import re

import httpx
from pydantic_ai import Tool

from .db import uid
from .research import begin_run, finish_run, public_url
from .security import internal_token
from .telemetry import trace_headers

URL_PATTERN = re.compile(
    r'https?://[^\s<>"，。；）)]+|(?<![\w@])(?:www\.)?[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.(?:com|org|net|io|ai|one|app|dev|co|xyz|me|tech|site|cn)(?:/[^\s<>"，。；）)]*)?',
    re.I,
)


def website_for_message(text, history, business=None):
    current = URL_PATTERN.search(text)
    if current:
        return public_url(current.group(0))
    if not re.search(
        r"网页|网站|网址|抓|浏览|访问|打开|链接|官网|browser|website|browse|fetch|read.*site",
        text,
        re.I,
    ):
        return None
    for message in reversed(history):
        if message["role"] == "user":
            found = URL_PATTERN.search(message["text"])
            if found:
                return public_url(found.group(0))
    return (
        public_url(business["product_url"])
        if business and business.get("product_url")
        else None
    )


class BrowserTools:
    def __init__(self, aid, goal, message_id=None, task_id=None, mode="research"):
        self.aid = aid
        self.goal = goal
        self.message_id = message_id
        self.task_id = task_id
        self.mode = mode
        self.run_id = None
        self.sources = []
        self.steps = 0
        self.last = None
        self.extra_sources = []

    async def command(self, operation, args=None):
        from .admin_settings import for_agent

        settings, _ = for_agent(self.aid, "models")
        if not settings["tools"]["public_web"]:
            return {"error": "TOOL_DISABLED", "message": "公开网页读取已在后台关闭，请根据已有资料作答。"}
        if not self.run_id:
            self.run_id = begin_run(
                self.aid, self.goal, self.message_id, self.task_id, self.mode
            )
        self.steps += 1
        if self.steps > settings["max_tool_calls"]:
            return {
                "error": "BROWSER_STEP_LIMIT",
                "message": "本轮已达到浏览步数上限，请根据已获取的网页作答",
            }
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            try:
                response = await client.post(
                    "http://127.0.0.1:8089/agents/" + self.aid + "/research/command",
                    json={
                        "run_id": self.run_id,
                        "command_id": uid(),
                        "operation": operation,
                        "args": args or {},
                    },
                    headers={
                        "Authorization": "Bearer " + internal_token(),
                        **trace_headers(),
                    },
                )
                if response.status_code >= 400:
                    return {
                        "error": "BROWSER_OPERATION_FAILED",
                        "message": response.text[:300]
                        if response.status_code == 409
                        else "浏览器暂时未能打开页面，请重试",
                    }
                self.last = response.json()
                return self.last
            except httpx.HTTPError:
                return {
                    "error": "BROWSER_SERVICE_UNAVAILABLE",
                    "message": "浏览器服务暂不可用，不能声称已经读取网页",
                }

    def tools(self):
        from .admin_settings import for_agent

        settings, _ = for_agent(self.aid, "models")
        if not settings["tools"]["public_web"] or not settings["max_tool_calls"]:
            return []
        async def prepare_browser_tool(ctx, definition):
            return definition if self.steps < min(7, settings["max_tool_calls"]) else None

        async def browser_open(url: str) -> dict:
            """用真实浏览器打开公开网址，返回渲染后的正文和当前可操作元素。支持 JavaScript 网站。"""
            return await self.command("open", {"url": url})

        async def browser_read() -> dict:
            """重新观察当前浏览器页面，取得最新正文和元素引用；用于动态内容或过期元素。"""
            return await self.command("read")

        async def browser_click(ref: str) -> dict:
            """点击最新观察中的元素引用。研究模式可跟随链接、展开内容或切换选项卡。"""
            return await self.command("click", {"ref": ref})

        async def browser_fill_search(ref: str, text: str) -> dict:
            """在已观察到的公开 GET 搜索框中输入文本。不能填写密码、登录或发布表单。"""
            return await self.command("fill", {"ref": ref, "text": text})

        async def browser_submit_search() -> dict:
            """实际按回车提交刚填写的公开 GET 搜索框，取得搜索后的页面。"""
            return await self.command("search_submit")

        async def browser_scroll(pixels: int = 700) -> dict:
            """在真实浏览器中滚动并重新观察页面。正数向下，负数向上。"""
            return await self.command("scroll", {"pixels": pixels})

        async def browser_back() -> dict:
            """在真实浏览器中返回上一页，并取得新观察。"""
            return await self.command("back")

        async def browser_explore(url: str, goal: str) -> dict:
            """使用 browser-use 自主探索复杂公开页面。已有简单浏览不足时使用；只读，不登录或发布，保留来源与证据。"""
            from .hybrid_research import explore

            self.steps += 1
            try:
                result = await explore(
                    self.aid, url, goal, mode=self.mode, message_id=self.message_id
                )
                self.extra_sources.extend(result.get("sources", []))
                return result
            except Exception:
                return {
                    "error": "BROWSER_USE_EXPLORATION_FAILED",
                    "message": "自主探索未完成，使用已有真实观察，不能声称已完成此步骤。",
                }

        from .hybrid_research import available

        return [
            Tool(
                fn,
                takes_ctx=False,
                sequential=True,
                max_retries=1,
                prepare=prepare_browser_tool,
            )
            for fn in (
                browser_open,
                browser_read,
                browser_click,
                browser_fill_search,
                browser_submit_search,
                browser_scroll,
                browser_back,
            )
            + ((browser_explore,) if available() and settings["tools"]["advanced_research"] else ())
        ]

    async def finish(self, state="succeeded"):
        if not self.run_id:
            return self.extra_sources
        self.sources = finish_run(self.aid, self.run_id, state)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            try:
                await client.post(
                    f"http://127.0.0.1:8089/agents/{self.aid}/research/{self.run_id}/close",
                    headers={
                        "Authorization": "Bearer " + internal_token(),
                        **trace_headers(),
                    },
                )
            except httpx.HTTPError:
                pass
        return self.sources + self.extra_sources
