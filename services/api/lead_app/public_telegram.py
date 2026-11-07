"""Read public directory entries and anonymous Telegram profile cards only."""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .community_links import telegram_link

DIRECTORY = "https://combot.org/api/chart/all?"


# Public, multi-topic community catalog. The publisher's listing supplies
# discovery leads only; every selected handle needs its own Telegram card.
CATALOG = "https://cdn.jsdelivr.net/gh/itgoyo/TelegramGroup@master/README.md"


def catalog_rows(text):
    rows = {}
    for line in text.splitlines():
        urls = re.findall(r"https://t\.me/[A-Za-z][A-Za-z0-9_]{3,31}(?![\w/?])", line)
        name = re.sub(r"https?://[^\s|)\]]+|[|*\[\]()]", " ", line)
        name = " ".join(name.split())[:100]
        if not name or re.search(r"频道|頻道|机器人|機器人|\bbot\b", name, re.I):
            continue
        for url in urls:
            if telegram_link(url) == url and not url.lower().endswith("bot"):
                rows.setdefault(url.lower(), {"href": url, "name": name})
    return sorted(
        rows.values(),
        key=lambda row: (
            not re.search(
                r"群|交流|讨论|討論|chat|group|community|support", row["name"], re.I
            )
        ),
    )


def directory_url(query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 60:
        raise ValueError("公开群目录查询无效")
    return DIRECTORY + urlencode({"limit": 10, "offset": 0, "q": query.strip()})


def linked_rule_post(description, url):
    handle = url.rsplit("/", 1)[-1]
    marker = r"(?:\brules?\b|群规|群規|群守则|規約)"
    message = (
        r"(?:https?://)?t\.me/(" + re.escape(handle) + r"/[1-9][0-9]{0,12})(?![\w/?#])"
    )
    match = re.search(
        marker + r"[^.:。；;\n]{0,65}[:：]?\s*" + message, description, re.I
    )
    return "https://t.me/" + match[1] if match else None


def rule_text(body, url):
    soup = BeautifulSoup(body, "html.parser")
    post = url.removeprefix("https://t.me/").casefold()
    for el in soup.select(".tgme_widget_message[data-post]"):
        if el["data-post"].casefold() == post:
            text = el.select_one(".tgme_widget_message_text")
            if text:
                return text.get_text(" ", strip=True)[:6000]
    return ""


def audience_counts(subtitle):
    """Read only the platform subtitle. Missing labels mean unknown, never zero."""
    counts = {}
    for key, label in (
        ("members", r"members?\b|subscribers?\b|成员|成員|订阅者|訂閱者|订阅人数"),
        ("online", r"online\b|在线|在線"),
    ):
        match = re.search(
            rf"(?<![\d.])([0-9]+(?:[, \u00a0\u202f][0-9]{{3}})*(?:\.\d+)?)\s*([km万萬]?)\s*(?:位|名|人)?\s*(?:{label})",
            subtitle or "", re.I,
        )
        if match:
            number = re.sub(r"[,\s]", "", match[1])
            scale = {"k": 1000, "m": 1000000, "万": 10000, "萬": 10000}
            counts[key] = int(float(number) * scale.get(match[2].lower(), 1))
    return counts


def discussion_entries(description, channel_url):
    """Only accept publicly labelled links to a channel's own chat."""
    entries = {}
    if description is not None:
        for anchor in description.select("a[href]"):
            href = telegram_link(anchor["href"])
            if not href or href.casefold() == channel_url.casefold():
                continue
            label = anchor.get_text(" ", strip=True)
            previous = anchor.previous_sibling
            prefix = str(previous).splitlines()[-1:] if isinstance(previous, str) else []
            context = " ".join(prefix + [label]).strip()
            if re.search(r"(?:讨论群|討論群|交流群|群聊|group chat|discussion group|chat group|our chat|join (?:the |our )?chat)\s*[:：→-]?\s*(?:https?://t\.me/|@|$)", context, re.I):
                entries[href.casefold()] = {"href": href, "name": context[:160]}
    return list(entries.values())[:2]


def group_card(body, url):
    soup = BeautifulSoup(body, "html.parser")

    def field(name):
        el = soup.select_one(".tgme_page_" + name)
        return el.get_text(" ", strip=True)[:2200] if el else ""

    name, subtitle, description = field("title"), field("extra"), field("description")
    group = bool(name and re.search(r"\bmembers?\b", subtitle, re.I))
    return {
        "title": name or "Telegram 公开资料",
        "subtitle": subtitle,
        "description": description,
        "is_public_group": group,
        "discussion_entries": discussion_entries(soup.select_one(".tgme_page_description"), url)
            if re.search(r"subscribers?\b|订阅者|訂閱者", subtitle, re.I) else [],
        "text": "\n".join((name, subtitle, description)),
        "community_links": [{"href": url, "name": name}] if group else [],
    }


TOPIC_DIRECTORY = "https://tdirectory.me"


def topic_directory_url(term):
    return TOPIC_DIRECTORY + "/search.html?" + urlencode({"query": term})


async def read_topic_directory(context, url):
    """Read public listings and resolve only the directory's explicit group redirect."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "tdirectory.me" or parsed.fragment:
        raise ValueError("不支持的目录入口")
    detail = re.fullmatch(r"/group/([a-z0-9_]+)\.dhtml", parsed.path, re.I)
    terms = parse_qs(parsed.query).get("query", [])
    if not (detail and not parsed.query) and not (len(terms) == 1 and 1 <= len(terms[0]) <= 60 and url == topic_directory_url(terms[0])):
        raise ValueError("不支持的公开群目录路径")
    reply = await context.request.get(url, max_redirects=0, timeout=14000)
    try:
        if not reply.ok:
            raise ValueError("群目录页面暂不可用")
        soup = BeautifulSoup(await reply.text(), "html.parser")
    finally:
        await reply.dispose()
    links, entries = [], {}
    if detail:
        redirect = "/gogroup/" + detail[1] + ".dhtml"
        if any(a.get_text(" ", strip=True).casefold() == "join" for a in soup.find_all("a", href=redirect)):
            response = await context.request.get(urljoin(url, redirect), max_redirects=0, timeout=10000)
            try:
                observed = telegram_link(response.headers.get("location"))
                if response.status in (301,302,303,307,308) and observed:
                    links.append({"href": observed, "name": soup.title.get_text(strip=True)[:120] if soup.title else "公开群"})
            finally:
                await response.dispose()
    else:
        for el in soup.select('a[href^="/group/"]'):
            path = el["href"]
            label = el.get_text(" ",strip=True)
            if label and re.fullmatch(r"/group/[a-z0-9_]+\.dhtml", path, re.I):
                entries.setdefault(path, {"href": urljoin(url,path), "name": label[:120]})
    return {"url":url, "title":soup.title.get_text(strip=True)[:160] if soup.title else "公开群目录",
            "text":"公开目录提供的群条目，仍需核实群资料。", "elements":[], "community_links":links,
            "community_entries":list(entries.values())[:8],"read_only":True}


async def read_metadata(actor, context, operation, args):
    if operation == "community_directory" and args.get("topic_url"):
        url = args["topic_url"]
        if urlsplit(url).netloc != "tdirectory.me":
            raise ValueError("不支持的群目录")
        await actor.forward_proxy.validate_url(url)
        return await read_topic_directory(context, url)
    if operation == "community_directory":
        url = directory_url(args.get("query"))
    elif operation == "community_catalog":
        url = CATALOG
    else:
        url = args.get("url")
        if not url or telegram_link(url) != url:
            raise ValueError("只读取公开账号资料页")
    await actor.forward_proxy.validate_url(url)
    from playwright.async_api import Error as BrowserError
    from playwright.async_api import TimeoutError as BrowserTimeout

    def unavailable(reason):
        return {"url": url, "title": "公开资料暂未核实", "text": "", "elements": [],
                "community_links": [], "error": reason, "read_only": True}

    try:
        response = await context.request.get(url, max_redirects=0, timeout=14000)
    except BrowserError as error:
        if operation != "community_preview":
            raise
        return unavailable("preview_timeout" if isinstance(error, BrowserTimeout) or "timeout" in str(error).lower()
                           else "preview_transport_failed")
    try:
        if operation == "community_preview" and not response.ok:
            return unavailable(f"preview_http_{response.status}")
        if not response.ok or response.url != url:
            raise ValueError("公开资料源暂不可用")
        if operation == "community_directory":
            entries = []
            for row in (await response.json())[:10]:
                handle = row.get("u") or ""
                if re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{3,31}", handle):
                    entries.append(
                        {
                            "href": "https://t.me/" + handle,
                            "name": str(row.get("t") or handle)[:120],
                        }
                    )
            data = {
                "title": "Combot 公开群目录",
                "text": "\n".join(e["name"] for e in entries),
                "community_links": entries,
            }
        elif operation == "community_catalog":
            data = {
                "title": "公开 Telegram 社群索引",
                "text": "公开社群条目，仅作为入口线索。",
                "community_links": catalog_rows(await response.text())[:1400],
            }
        else:
            data = group_card(await response.text(), url)
            rule_url = (
                linked_rule_post(data["description"], url)
                if data["is_public_group"]
                else None
            )
            if rule_url:
                data["rules_link"] = rule_url
                # Only the rule document explicitly named in this group's bio.
                # No history, neighboring posts, comments, or author metadata.
                from playwright.async_api import Error as BrowserError

                try:
                    reply = await context.request.get(
                        rule_url + "?embed=1", max_redirects=0, timeout=9000
                    )
                    try:
                        if reply.ok and reply.url == rule_url + "?embed=1":
                            data["rules_text"] = rule_text(await reply.text(), rule_url)
                    finally:
                        await reply.dispose()
                except BrowserError:
                    pass
        return {**data, "url": url, "elements": [], "read_only": True}
    finally:
        await response.dispose()
