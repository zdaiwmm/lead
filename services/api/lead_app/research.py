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

    def gate(self):
        with tx(self.actor.aid) as c:
            a = require_agent(c, self.actor.aid)
            run = one(c, "SELECT * FROM lead.browser_runs WHERE id=%s", (self.run_id,))
            if not run or run["state"] != "running":
                raise ResearchStopped("浏览任务已经结束")
            if (
                a["control_version"] != run["control_version"]
                or a["config_revision"] != run["config_revision"]
            ):
                raise ResearchStopped("控制权或运营配置已改变，浏览操作已停止")
            if run["mode"] == "autonomous" and a["desired_state"] != "running":
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
        self.gate()
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
                "SELECT output->>'url' AS url FROM lead.browser_steps WHERE run_id=%s AND state='completed' ORDER BY seq DESC LIMIT 1",
                (self.run_id,),
            )
        if last and last["url"]:
            url = public_url(last["url"])
            await self.actor.forward_proxy.validate_url(url)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Element handles are deliberately rebuilt by a fresh read. A click
            # from the old process may not target a changed document.

    async def read_only_route(self, route):
        # Even a malicious page or accidental submit cannot issue writes while
        # the Agent is researching. No bypass for the model or page instructions.
        if route.request.method not in ("GET", "HEAD", "OPTIONS"):
            return await route.abort()
        await self.actor.route(route)

    async def observe(self):
        data = await self.page.evaluate(OBSERVE)
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
        return {
            "url": self.page.url,
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
                    await self.page.goto(
                        url, wait_until="domcontentloaded", timeout=35000
                    )
                    # Wait for actual visible content, without requiring every
                    # analytics request on the website to become idle.
                    await self.page.locator("body").wait_for(state="visible")
                    try:
                        await self.page.wait_for_function(
                            "document.body.innerText.trim().length>30", timeout=4000
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
