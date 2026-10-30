"""Real browser observation/action loop, bound to an Agent and durable run.

No arbitrary selectors, JavaScript, browser connection, files or database tools
are exposed to the model. Public research is enforced as read-only at the network
and UI layers. Authorized publishing uses the separate action ledger.
"""

import hashlib
import json
import re
from urllib.parse import urljoin, urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

from .config import STATE
from .db import all_rows, j, one, record, require_agent, tx, uid
from .scope import current_tenant

OBSERVE = r"""() => {
 const visible = el => {const r=el.getBoundingClientRect();const s=getComputedStyle(el);return !!(r.width&&r.height)&&s.visibility!=='hidden'&&s.display!=='none'};
 const selector = el => {let p=[];while(el&&el.nodeType===1){let s=el.tagName.toLowerCase();if(el.id){p.unshift('#'+CSS.escape(el.id));break}let n=1,sib=el;while((sib=sib.previousElementSibling))if(sib.tagName===el.tagName)n++;p.unshift(s+':nth-of-type('+n+')');el=el.parentElement}return p.join(' > ')};
 const items=Array.from(document.querySelectorAll('a[href],button,[role=button],[role=tab],input,textarea,select')).filter(visible).slice(0,100).map(el=>({selector:selector(el),tag:el.tagName.toLowerCase(),type:el.type||'',target:el.getAttribute('target')||'',role:el.getAttribute('role')||'',name:(el.getAttribute('aria-label')||el.innerText||el.getAttribute('placeholder')||el.getAttribute('title')||'').trim().slice(0,200),href:el.tagName==='A'?el.href:null,expanded:el.hasAttribute('aria-expanded'),editable:el.isContentEditable,form:el.form?{method:el.form.method,action:el.form.action,role:el.form.getAttribute('role')}:null}));
 return {title:document.title,text:(document.body?.innerText||'').slice(0,12000),elements:items,has_password:Array.from(document.querySelectorAll('input[type=password]')).some(visible)};
}"""


class ResearchStopped(ValueError):
    pass


def community_entry(link):
    """Follow community evidence, excluding search UI and site-account actions."""
    name = link.get("name", "")
    parsed = urlsplit(link.get("href", ""))
    return bool(
        name
        and parsed.scheme == "https"
        and parsed.hostname not in ("t.me", "telegram.me")
        and not (
            parsed.hostname in ("www.google.com", "google.com")
            and parsed.path == "/search"
        )
        and not re.search(
            r"/contact/report-content|/(?:login|logout|register|signup|report)(?:[/?#]|$)",
            parsed.path,
            re.I,
        )
        and not re.search(r"https?://translations\.telegram\.org\b", name, re.I)
        and re.search(
            r"community|contact|telegram|discussion|forum|社群|讨论|交流|联系我们",
            name + " " + parsed.path,
            re.I,
        )
    )


def public_url(value, base=None):
    value = value.strip()
    if not re.match(r"^https?://", value, re.I) and re.match(
        r"^[\w.-]+\.[a-z]{2,}(?:[/:?#]|$)", value, re.I
    ):
        value = "https://" + value
    value = urljoin(base or "", value)
    p = urlsplit(value)
    if (
        p.scheme.lower() not in ("https", "http")
        or not p.hostname
        or p.username
        or p.password
    ):
        raise ValueError("只允许公开网站网址")
    return p._replace(scheme=p.scheme.lower()).geturl()


class ResearchPage:
    def __init__(self, actor, run_id):
        self.actor = actor
        self.run_id = run_id
        self.page = None
        self.context = None
        self.refs = {}
        self.snapshot = 0
        self.last_search = None
        self.community_redirects = []
        self.redirect_source_url = None
        self.redirect_metadata_only = False
        self.redirect_observation = {}

    def gate(self):
        from .admin_settings import effective

        with tx(self.actor.aid) as c:
            a = require_agent(c, self.actor.aid)
            run = one(c, "SELECT * FROM lead.browser_runs WHERE id=%s", (self.run_id,))
            if not run or run["state"] != "running":
                raise ResearchStopped("浏览任务已经结束")
            tools, _ = effective(c, "models", self.actor.aid)
            if not tools["tools"]["public_web"] or (
                run["mode"] == "community_discovery"
                and not tools["tools"]["community_research"]
            ):
                raise ResearchStopped("此研究工具已在后台关闭")
            if (
                a["control_version"] != run["control_version"]
                or a["config_revision"] != run["config_revision"]
            ):
                raise ResearchStopped("控制权或运营配置已改变，浏览操作已停止")
            if (
                run["mode"] in ("autonomous", "community_discovery")
                and a["desired_state"] != "running"
            ):
                raise ResearchStopped("Agent 已暂停")
            if one(
                c,
                "SELECT 1 FROM lead.takeover_sessions WHERE state IN ('active','requested','ending') AND expires_at>now()",
            ):
                raise ResearchStopped("用户正在接管浏览器")
            if not one(
                c,
                "SELECT 1 FROM lead.profile_leases WHERE state='held' AND epoch=%s AND expires_at>now()",
                (self.actor.epoch,),
            ):
                raise ResearchStopped("浏览器租约已失效")
            return run

    async def ensure(self):
        self.community_discovery = self.gate()["mode"] == "community_discovery"
        if self.page and not self.page.is_closed():
            return
        browser = self.actor.context.browser
        if browser is None:
            raise ValueError("此浏览器版本不能提供受限的研究上下文")
        self.context = await browser.new_context(
            accept_downloads=False,
            viewport={"width": 1280, "height": 800},
            proxy=self.actor.forward_proxy.browser_proxy,
        )
        self.page = await self.context.new_page()
        await self.context.route("**/*", self.read_only_route)

        async def deny_websocket(ws):
            await ws.close(code=1008, reason="Research is read-only")

        await self.context.route_web_socket("**/*", deny_websocket)
        self.page.set_default_timeout(10000)
        with tx(self.actor.aid) as c:
            last = one(
                c,
                "SELECT output->>'url' AS url,output->>'redirect_metadata_only' AS metadata_only,output->'community_links' AS links,output->'redirect_observation' AS redirect_observation FROM lead.browser_steps WHERE run_id=%s AND state='completed' ORDER BY seq DESC LIMIT 1",
                (self.run_id,),
            )
        if last and last["url"]:
            if last["metadata_only"] == "true":
                self.redirect_source_url = last["url"]
                self.community_redirects = last["links"] or []
                self.redirect_metadata_only = True
                self.redirect_observation = last["redirect_observation"] or {}
                return
            url = public_url(last["url"])
            await self.actor.forward_proxy.validate_url(url)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Element handles are deliberately rebuilt by a fresh read. A click
            # from the old process may not target a changed document.

    async def read_only_route(self, route):
        # Even a malicious page or accidental submit cannot issue writes while
        # the Agent is researching. No bypass for the model or page instructions.
        host = (urlsplit(route.request.url).hostname or "").lower()
        if getattr(self, "community_discovery", False) and any(
            host == d or host.endswith("." + d)
            for d in (
                "t.me",
                "telegram.me",
                "telegram.org",
                "telegram.dog",
                "tgstat.com",
                "telemetr.io",
                "reddit.com",
                "facebook.com",
                "x.com",
                "twitter.com",
                "discord.com",
            )
        ):
            # Capture a search engine's actual redirect destination as link
            # evidence, then block the destination before loading any content.
            # Subresources, message URLs and direct Telegram browsing do not
            # create this evidence.
            from .community_links import telegram_link

            link = telegram_link(route.request.url)
            if (
                link
                and self.redirect_source_url
                and self.page
                and route.request.method == "GET"
                and route.request.is_navigation_request()
                and route.request.frame == self.page.main_frame
            ):
                item = {"href": link, "name": "Telegram 公开入口（搜索跳转核对）"}
                if item not in self.community_redirects:
                    self.community_redirects.append(item)
                self.redirect_metadata_only = True
            return await route.abort()
        if route.request.method not in ("GET", "HEAD", "OPTIONS"):
            if host in ("www.google.com", "www.bing.com", "html.duckduckgo.com"):
                requests = getattr(self, "blocked_search_requests", [])
                item = {
                    "method": route.request.method,
                    "host": host,
                    "path": urlsplit(route.request.url).path,
                }
                if item not in requests and len(requests) < 8:
                    requests.append(item)
                self.blocked_search_requests = requests
            return await route.abort()
        if (
            getattr(self, "community_discovery", False)
            and self.redirect_source_url == route.request.url
            and route.request.method == "GET"
            and route.request.is_navigation_request()
        ):
            # Browser route handlers are not re-run for every HTTP redirect.
            # Fetch only the search redirect response, never its Telegram body.
            response = await route.fetch(max_redirects=0, timeout=15000)
            if response.status in (301, 302, 303, 307, 308):
                from .community_links import telegram_link

                destination = urljoin(
                    route.request.url, response.headers.get("location", "")
                )
                link = telegram_link(destination)
                target = urlsplit(destination)
                self.redirect_observation = {
                    "status": response.status,
                    "destination_host": target.hostname,
                    "kind": "public_handle"
                    if link
                    else "invite"
                    if target.hostname in ("t.me", "telegram.me")
                    and target.path.startswith(("/+", "/joinchat/"))
                    else "interface_translation_project"
                    if target.hostname == "translations.telegram.org"
                    else "unsupported_destination",
                    "destination_content_loaded": False,
                }
                self.community_redirects = (
                    [{"href": link, "name": "Telegram 公开入口（搜索跳转核对）"}]
                    if link
                    else []
                )
                self.redirect_metadata_only = True
                return await route.fulfill(
                    status=200,
                    content_type="text/html",
                    body="<!doctype html><title>Public link observed</title><p>Redirect metadata recorded.</p>",
                )
            return await route.fulfill(response=response)
        await self.actor.route(route)

    async def observe(self):
        data = (
            {
                "title": "已核对搜索跳转目的地址",
                "text": "",
                "elements": [],
                "has_password": False,
            }
            if self.redirect_metadata_only
            else await self.page.evaluate(OBSERVE)
        )
        self.snapshot += 1
        self.refs = {}
        visible = []
        for idx, item in enumerate(data["elements"]):
            ref = f"s{self.snapshot}:e{idx}"
            self.refs[ref] = item
            visible.append(
                {
                    k: v
                    for k, v in {**item, "ref": ref}.items()
                    if k not in ("selector", "form")
                }
            )
        community_links = list(self.community_redirects)
        community_entries = []
        if (
            getattr(self, "community_discovery", False)
            and not self.redirect_metadata_only
        ):
            # Footer/community links can be after the generic 100-control cap.
            # This fixed extraction exports links only, never arbitrary scripts.
            links = await self.page.evaluate(
                """() => [...document.querySelectorAll('a[href]')].map(a=>({href:a.href,name:(a.innerText||a.getAttribute('aria-label')||'').trim().slice(0,120),context:(a.closest('li,p,td,tr')||a.parentElement)?.innerText?.slice(0,600)||''}))"""
            )
            from .community_links import telegram_link

            community_links = [link for link in links if telegram_link(link["href"])][
                :30
            ]
            community_entries = [link for link in links if community_entry(link)]
            community_entries.sort(
                key=lambda link: (
                    not bool(
                        re.search(
                            r"https?://(?:t\.me|telegram\.me)\b", link["name"], re.I
                        )
                    )
                )
            )
            community_entries = community_entries[:4]
        return {
            "community_links": community_links,
            "redirect_metadata_only": self.redirect_metadata_only,
            "redirect_observation": self.redirect_observation,
            "community_entries": community_entries,
            "blocked_search_requests": getattr(self, "blocked_search_requests", []),
            "url": self.redirect_source_url
            if self.redirect_metadata_only
            else self.page.url,
            "title": data["title"],
            "text": data["text"],
            "elements": visible,
            "requires_login": data["has_password"],
            "read_only": True,
            "snapshot": self.snapshot,
        }

    async def command(self, operation, args, command_id):
        async with self.actor.lock:
            self.actor.touch()
            await self.ensure()
            self.gate()
            with tx(self.actor.aid) as c:
                old = one(
                    c,
                    "SELECT * FROM lead.browser_steps WHERE command_id=%s",
                    (command_id,),
                )
                if old:
                    if (
                        str(old["run_id"]) != self.run_id
                        or old["input"] != args
                        or old["operation"] != operation
                    ):
                        raise ValueError("命令标识与内容不一致")
                    if old["state"] == "completed":
                        return old["output"]
                    raise ValueError("该浏览命令需要重新观察后恢复")
                seq = one(
                    c,
                    "SELECT coalesce(max(seq),0)+1 AS n FROM lead.browser_steps WHERE run_id=%s",
                    (self.run_id,),
                )["n"]
                step = uid()
                c.execute(
                    "INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'started')",
                    (
                        step,
                        current_tenant(),
                        self.actor.aid,
                        self.run_id,
                        seq,
                        command_id,
                        operation,
                        j(args),
                    ),
                )
            try:
                if operation == "open":
                    url = public_url(args["url"])
                    await self.actor.forward_proxy.validate_url(url)
                    self.community_redirects = []
                    self.redirect_metadata_only = False
                    self.redirect_observation = {}
                    parsed = urlsplit(url)
                    self.redirect_source_url = (
                        url
                        if self.community_discovery
                        and parsed.hostname in ("www.google.com", "google.com")
                        and parsed.path in ("/goto", "/url")
                        else None
                    )
                    try:
                        await self.page.goto(
                            url, wait_until="domcontentloaded", timeout=35000
                        )
                    except Exception:
                        if not self.community_redirects:
                            raise
                        self.redirect_metadata_only = True
                    if self.community_redirects:
                        self.redirect_metadata_only = True
                    # Wait for actual visible content, without requiring every
                    # analytics request on the website to become idle.
                    if not self.redirect_metadata_only:
                        await self.page.locator("body").wait_for(state="visible")
                    try:
                        if not self.redirect_metadata_only:
                            await self.page.wait_for_function(
                                "document.body.innerText.trim().length>30", timeout=4000
                            )
                    except BrowserTimeout:
                        pass
                    host = (urlsplit(self.page.url).hostname or "").lower()
                    search_selector = (
                        "#search h3"
                        if host in ("www.google.com", "google.com")
                        else "#b_results .b_algo"
                        if host in ("www.bing.com", "bing.com")
                        else ".result__a"
                        if host == "html.duckduckgo.com"
                        else None
                    )
                    if search_selector and not self.redirect_metadata_only:
                        try:
                            await self.page.locator(search_selector).first.wait_for(
                                timeout=8000
                            )
                        except BrowserTimeout:
                            pass
                    self.refs = {}
                elif operation == "read":
                    if self.page.url == "about:blank":
                        raise ValueError("先用 browser_open 打开网址")
                elif operation == "click":
                    item = self.refs.get(args["ref"])
                    if not item:
                        raise ValueError("页面元素引用已过期，请重新读取页面")
                    if item["tag"] == "a":
                        url = public_url(item["href"], self.page.url)
                        if re.search(
                            r"/(logout|log-out|delete|unsubscribe)(?:[/?#]|$)",
                            url,
                            re.I,
                        ):
                            raise ValueError("研究任务不执行账号操作")
                        await self.actor.forward_proxy.validate_url(url)
                    elif not (item["expanded"] or item["role"] == "tab"):
                        raise ValueError(
                            "此按钮需要操作授权；研究任务仅支持链接、展开及选项卡"
                        )
                    before = set(self.context.pages)
                    if item.get("target") == "_blank":
                        async with self.page.expect_popup(timeout=15000) as popup:
                            await self.page.locator(item["selector"]).click(
                                timeout=10000
                            )
                        self.page = await popup.value
                    else:
                        await self.page.locator(item["selector"]).click(timeout=10000)
                        created = [p for p in self.context.pages if p not in before]
                        if created:
                            self.page = created[-1]
                    await self.page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                    self.refs = {}
                elif operation == "fill":
                    item = self.refs.get(args["ref"])
                    if not item:
                        raise ValueError("页面元素引用已过期，请重新读取页面")
                    form = item["form"]
                    if (
                        item["tag"] != "input"
                        or item["type"] not in ("search", "text")
                        or (form and form["method"].lower() != "get")
                        or (
                            item["type"] != "search"
                            and (not form or form["role"] != "search")
                        )
                    ):
                        raise ValueError("研究模式只允许填写公开的 GET 搜索框")
                    target = public_url(
                        form["action"] if form else self.page.url, self.page.url
                    )
                    if urlsplit(target).netloc != urlsplit(self.page.url).netloc:
                        raise ValueError("搜索表单不在当前网站")
                    await self.page.locator(item["selector"]).fill(args["text"])
                    self.last_search = item
                elif operation == "search_submit":
                    if not self.last_search:
                        raise ValueError("请先填写公开搜索框")
                    await self.page.locator(self.last_search["selector"]).press("Enter")
                    await self.page.wait_for_load_state(
                        "domcontentloaded", timeout=10000
                    )
                    self.last_search = None
                    self.refs = {}
                elif operation == "scroll":
                    await self.page.mouse.wheel(
                        0, max(-1500, min(args.get("pixels", 700), 1500))
                    )
                elif operation == "back":
                    await self.page.go_back(wait_until="domcontentloaded")
                    self.refs = {}
                else:
                    raise ValueError("未知浏览操作")
                self.gate()
                result = await self.observe()
                result["evidence_id"] = step
                result["run_id"] = self.run_id
                folder = STATE / "evidence" / self.actor.aid
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                screenshot = folder / (step + ".png")
                await self.page.screenshot(path=str(screenshot))
                bodyfile = folder / (step + ".json")
                bodyfile.write_text(json.dumps(result, ensure_ascii=False))
                with tx(self.actor.aid) as c:
                    c.execute(
                        "UPDATE lead.browser_steps SET state='completed',output=%s,screenshot_ref=%s,ended_at=now() WHERE id=%s",
                        (j(result), str(screenshot), step),
                    )
                    if result["text"] and result["url"].startswith(
                        ("http://", "https://")
                    ):
                        doc = uid()
                        c.execute(
                            "INSERT INTO lead.knowledge_documents(id,tenant_id,agent_id,revision,title,source_type,source_ref,body_ref,sha256,trust_state) VALUES(%s,%s,%s,1,%s,'browser',%s,%s,%s,'untrusted_source')",
                            (
                                doc,
                                current_tenant(),
                                self.actor.aid,
                                result["title"],
                                result["url"],
                                str(bodyfile),
                                hashlib.sha256(result["text"].encode()).hexdigest(),
                            ),
                        )
                        c.execute(
                            "INSERT INTO lead.knowledge_chunks(tenant_id,agent_id,document_id,ordinal,body,search_vector) VALUES(%s,%s,%s,0,%s,to_tsvector('simple',%s))",
                            (
                                current_tenant(),
                                self.actor.aid,
                                doc,
                                result["text"],
                                result["text"],
                            ),
                        )
                return result
            except Exception as e:
                code = (
                    "BROWSER_CANCELED"
                    if isinstance(e, ResearchStopped)
                    else "BROWSER_COMMAND_FAILED"
                )
                with tx(self.actor.aid) as c:
                    c.execute(
                        "UPDATE lead.browser_steps SET state='failed',error_code=%s,ended_at=now() WHERE id=%s",
                        (code, step),
                    )
                if isinstance(e, ValueError):
                    raise
                raise ValueError(
                    "网页操作未完成，请重新读取页面或尝试另一个公开网址"
                ) from None


def begin_run(aid, goal, message_id=None, task_id=None, mode="research"):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        if one(
            c,
            "SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()",
        ):
            raise ResearchStopped("环境正在备份，浏览任务稍后重试")
        rid = uid()
        c.execute(
            "INSERT INTO lead.browser_runs(id,tenant_id,agent_id,message_id,task_id,goal,mode,control_version,config_revision) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                rid,
                current_tenant(),
                aid,
                message_id,
                task_id,
                goal[:6000],
                mode,
                a["control_version"],
                a["config_revision"],
            ),
        )
        return rid


def finish_run(aid, rid, state="succeeded"):
    with tx(aid) as c:
        steps = all_rows(
            c,
            "SELECT id,output FROM lead.browser_steps WHERE run_id=%s AND state='completed' ORDER BY seq",
            (rid,),
        )
        updated = one(
            c,
            "UPDATE lead.browser_runs SET state=%s,ended_at=now() WHERE id=%s AND state='running' RETURNING id",
            (state, rid),
        )
        if steps and updated:
            record(
                c,
                aid,
                "已浏览业务网页",
                "、".join(dict.fromkeys(s["output"]["title"] for s in steps))[:500],
            )
        return [
            {
                "url": s["output"]["url"],
                "title": s["output"]["title"],
                "evidence_id": str(s["id"]),
                "run_id": rid,
            }
            for s in steps
        ]
