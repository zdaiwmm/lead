import re

import httpx
from pydantic_ai import Tool

from .db import uid
from .research import begin_run, finish_run, public_url
from .security import internal_token

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

    async def command(self, operation, args=None):
        if not self.run_id:
            self.run_id = begin_run(
                self.aid, self.goal, self.message_id, self.task_id, self.mode
            )
        self.steps += 1
        if self.steps > 12:
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
                    headers={"Authorization": "Bearer " + internal_token()},
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
        async def prepare_browser_tool(ctx, definition):
            return definition if self.steps < 7 else None

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
        ]

    async def finish(self, state="succeeded"):
        if not self.run_id:
            return []
        self.sources = finish_run(self.aid, self.run_id, state)
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            try:
                await client.post(
                    f"http://127.0.0.1:8089/agents/{self.aid}/research/{self.run_id}/close",
                    headers={"Authorization": "Bearer " + internal_token()},
                )
            except httpx.HTTPError:
                pass
        return self.sources
