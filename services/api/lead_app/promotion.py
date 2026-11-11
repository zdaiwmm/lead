"""Compact, per-publication attribution shared by every promotion path."""

import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PLATFORM_CODES = {
    "telegram": "tg",
    "x": "x",
    "linkedin": "li",
    "facebook": "fb",
    "instagram": "ig",
    "youtube": "yt",
    "tiktok": "tt",
    "reddit": "rd",
    "discord": "dc",
    "pinterest": "pi",
    "threads": "th",
    "whatsapp": "wa",
    "direct": "d",
}
REF_KEY = "lr"
CODE_PATTERN = r"(?:[A-Za-z0-9_-]{16}|[a-z]{1,2}_[A-Za-z0-9_-]{11})"


PROMOTER_VOICE = (
    "发言人的身份是帮产品做推广的人。日常发言直接讲场景或产品，不自报家门，不写 Disclosure 或身份声明。"
    "不用‘我们团队’‘我们的产品’等口吻，不以员工、开发者、官方代表或用户身份发言。"
    "群友直接询问你和产品的关系时，才用对方的语言简短回答‘我帮这个产品做推广’。"
    "已核实的功能用产品名直接描述，例如“聊天页支持自动锁定”。具体说明使用条件。仅有网页自述、尚未核实的卖点略去。对外不用“官网称”“据说”“号称”或英文同类措辞。"
)


def promoter_voice(agent):
    return (agent.get("onboarding_state") or {}).get("public_voice") == "independent_promoter"


def promoter_copy_ok(text, brand, *, identity_question=False):
    attribution = (
        r"(?:官[网網方]|[网網]站|品牌方|[廠厂]商)(?:[声聲宣號号]?[称稱]|表示|[说說]是?)|"
        r"(?:[据據](?:官[网網方]|[网網]站)(?:[介說说聲声宣表]|所)|[据據][称稱说說]|[号號][称稱]|所[谓謂])|"
        r"\b(?:its stated features|(?:the |their |its )?(?:official )?(?:website|site|company) (?:says|claims|states)|"
        r"according to (?:the |its |their )?(?:official )?(?:website|site)|reportedly|supposedly|allegedly|claims to (?:offer|support))\b"
    )
    if re.search(attribution, text, re.I):
        return False
    brand = re.escape(brand or "Chamber")
    claim = (
        rf"(?:我们|我們|我是|我来自|我來自|我代表|这里是|這裡是|作为|作為).{{0,26}}(?:{brand}\s*(?:团队|團隊|官方|员工|員工|开发|開發)|官方(?:账号|團隊|团队|代表|人员|人員))|"
        rf"(?:我们|我們)(?:的)?(?:产品|產品|团队|團隊|开发了|開發了|推出了)|(?:^|[。！？.!?]\s*)\s*{brand}\s*(?:团队|團隊|官方)\s*(?:在此|分享|提醒|为您|為您)|"
        rf"\b(?:I(?:['’]m| am)|we(?:['’]re| are)|speaking|as (?:a |an )?|on behalf of|greetings from|hello from|this is)\s+(?:[^.!?\n]{{0,36}}\s)?(?:the\s+)?{brand}(?:['’]s)?\s*(?:team|staff|official|employee|developer)|"
        rf"\b(?:I(?:['’]m| am)|we(?:['’]re| are))\s+(?:with|from|at)\s+(?:the\s+)?{brand}\b|"
        rf"\b(?:the\s+)?{brand}\s+team\s+(?:here|speaking)|\bour (?:team|product|app|platform|service)\b|"
        rf"\b(?:I|we)\s+(?:represent|built|developed|founded)\s+{brand}\b|"
        rf"(?:somos|soy|sou|nous sommes|je suis|мы из|я из).{{0,28}}(?:equipo|equipe|équipe|команд).{{0,16}}{brand}"
    )
    claim += (
        rf"|(?:我是|我们是|我們是|这里是|這裡是|我来自|我來自|我代表).{{0,24}}"
        rf"(?:官方(?:的人|运营|運營|客服|人员|人員|账号|帳號|代表)|(?:产品|產品|{brand})\s*(?:的)?\s*(?:团队|團隊|员工|員工|开发者|開發者))"
        rf"|(?:^|[。！？.!?]\s*)\s*{brand}\s*(?:官方账号|官方帳號|官方运营|官方運營|团队在这|團隊在這)"
        rf"|\b(?:I work(?: for| at)|we work(?: for| at)|I am an employee of|we(?: are|['’]re))\s+(?:the )?{brand}(?![\w])"
        rf"|\b(?:we(?: are|['’]re)|I(?: am|['’]m)|this is)\s+(?:the |an? )?official (?:account|representative|team|support)"
        rf"|(?:soy|somos|sou|nous sommes|je suis|мы|я).{{0,15}}(?:representante oficial|cuenta oficial|compte officiel|официальн)"
    )
    if re.search(claim, text, re.I):
        return False
    introduction = r"(?:我(?:是|在|负责|幫|帮).{0,20}(?:推广|推廣)|\b(?:I (?:help (?:to )?promote|promote|do marketing for)|as (?:a|an) (?:product )?promoter|disclosure\s*:))"
    return identity_question or not re.search(introduction, text, re.I)


def asked_role(text):
    return bool(re.search(
        r"你(?:们|們)?(?:是(?:谁|誰|哪位|做什么的|做什麼的|.{0,12}(?:官方|团队|團隊|员工|員工|推广|推廣))|做什么的|做什麼的|和.{0,15}什么关系)|你.{0,10}(?:官方|推广|推廣|哪位)|"
        r"\b(?:who are you|what(?:['’]s| is) your (?:role|connection)|are you (?:the |a |an )?(?:official|from|with|affiliated|promot|part|paid)|do you (?:work for|promote))|"
        r"(?:quién eres|quem é você|кто вы|кто ты)", text or "", re.I))


def new_marker(platform):
    # 64 random bits, plus a readable channel code; at most 18 appended chars.
    return PLATFORM_CODES[platform] + "_" + secrets.token_urlsafe(8)


def attributed_url(destination, marker):
    parts = urlsplit(destination)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("请填写有效的公开网站链接")
    # Keep repeated query keys and fragments, replacing only our attribution.
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k not in (REF_KEY, "lead_ref")
    ]
    query.append((REF_KEY, marker))
    return urlunsplit(parts._replace(query=urlencode(query)))


def configuration(agent, business=None):
    saved = agent["onboarding_state"].get("promotion", {})
    return {
        "destination_url": saved.get("destination_url")
        or (business or {}).get("product_url")
        or "",
        "configured": bool(saved.get("destination_url")),
        "revision": saved.get("revision", 0),
    }


def tracking_required(agent):
    from .growth_experiments import chamber_campaign
    return bool(agent["onboarding_state"].get("promotion", {}).get("require_tracking")) or chamber_campaign(agent)


def telegram_link_line(body, link):
    """Give Telegram a bare URL line, keeping continuation text with its sentence."""
    if not link or body.count(link) != 1:
        return body
    lines = body.split("\n")
    for i, line in enumerate(lines):
        if link not in line:
            continue
        left, right = (part.strip() for part in line.split(link))
        if left.endswith(("打开", "点击", "访问", "前往")):
            sentence = left + "以下链接" + right
            lines[i] = sentence + "\n" + link
        else:
            lines[i] = "\n".join(part for part in (left, link, right) if part)
        break
    return "\n".join(lines)


def promotion_body(body, destination, marker, product_url=None, *, require_tracking=False):
    """Replace product URLs in model copy with one configured, attributed CTA."""
    attributed_url(destination, marker)  # Preserve destination validation.
    from .outbound_links import configured_problem, external_url_problem
    if external_url_problem(destination):
        raise ValueError("推广目标必须是公开网站")
    if configured_problem():
        if require_tracking:
            raise ValueError("public_shortlink_required")
        link = attributed_url(destination, marker)
    else:
        link = tracking_url(marker)
    hosts = {urlsplit(url).hostname for url in (destination, product_url) if url}
    used = False

    def replace(match):
        nonlocal used
        if urlsplit(match[0]).hostname not in hosts:
            return match[0]
        if used:
            return ""
        used = True
        return link

    body = re.sub(r"https?://[^\s<>，。；）)\]]+", replace, body).strip()
    if not used:
        body += "\n\n" + link
    if marker.startswith("tg_"):
        body = telegram_link_line(body, link)
    return body, link


def tracking_url(marker):
    from .analytics import public_origin

    if not re.fullmatch(CODE_PATTERN, marker):
        raise ValueError("推广链接编号无效")
    return public_origin() + "/r/" + marker
