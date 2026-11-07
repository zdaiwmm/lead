"""Pre-creation guest session. Only bounded first-party reads reach the website."""

import asyncio
import os
import re
from contextlib import AsyncExitStack
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from . import network_namespace
from .config import STATE, env
from .fetch import pinned_get
from .forward_proxy import BrowserForwardProxy
from .research import OBSERVE

SLOTS = asyncio.Semaphore(1)
ENTRY = re.compile(
    r"演示|体验|试用|预览|功能|示例|筛选|帮助|了解更多|如何使用|使用说明|demo|try it|preview|filter|tour|features?|how it works|learn more|sample|说明|玩法",
    re.I,
)
MUTATION = re.compile(
    r"登录|注册|加入|发送|提交|删除|创建|购买|支付|授权|邀请|订阅|logout|sign.?up|log.?in|join|send|submit|delete|create|purchase|checkout|invite|subscribe",
    re.I,
)


def next_control(elements, visited, host):
    for el in elements:
        label = el["name"]
        href = el.get("href") or ""
        if (
            not label
            or label in visited
            or el.get("form")
            or el["tag"] in ("input", "textarea", "select")
            or MUTATION.search(label + " " + href)
        ):
            continue
        if href and urlsplit(href).hostname != host:
            continue
        if ENTRY.search(label) or el.get("role") == "tab" or el.get("expanded"):
            return el
    # Some apps expose their setup form only behind a "Create…" button.
    # Preview that local transition with *all* subsequent network reads blocked.
    for el in elements:
        label = el["name"]
        if (
            el["tag"] == "button"
            and not el.get("form")
            and not el.get("href")
            and label not in visited
            and re.fullmatch(r"(?:创建|新建|Create |New )[\w -]{1,35}", label, re.I)
            and not MUTATION.search(
                re.sub(r"^(创建|新建|Create |New )", "", label, flags=re.I)
            )
        ):
            return {**el, "offline": True}
    return None


async def explore(url):
    host = urlsplit(url).hostname
    observations = []
    visited = set()
    async with asyncio.timeout(58), SLOTS, AsyncExitStack() as stack:
        pw = await stack.enter_async_context(async_playwright())
        client = await stack.enter_async_context(
            httpx.AsyncClient(timeout=7, trust_env=False)
        )
        options = {
            "env": {
                k: v
                for k, v in os.environ.items()
                if k in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT", "WINDIR")
            },
            "chromium_sandbox": True,
            # All permitted requests are fulfilled by pinned_get. Even a redirect or
            # unhandled browser connection has no usable outbound proxy.
            "proxy": {"server": "http://127.0.0.1:1"},
            "args": [
                "--proxy-bypass-list=<-loopback>",
                "--disable-quic",
                "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            ],
        }
        if network_namespace.required():
            key = uuid4().hex
            profile = stack.enter_context(
                TemporaryDirectory(dir=STATE / "profiles", prefix="trial-")
            )
            assets = stack.enter_context(
                TemporaryDirectory(dir=STATE / "assets", prefix="trial-")
            )
            proxy = await BrowserForwardProxy(key).start()
            stack.push_async_callback(proxy.close)
            options["executable_path"] = (
                env().get("LEAD_TRIAL_CHROMIUM_PATH") or pw.chromium.executable_path
            )
            options = network_namespace.apply(options, key, proxy)
            options["env"].update(
                LEAD_BROWSER_PROFILE=profile, LEAD_BROWSER_ASSETS=assets
            )
            ctx = await pw.chromium.launch_persistent_context(
                profile,
                **options,
                service_workers="block",
                accept_downloads=False,
                locale="zh-CN",
            )
            stack.push_async_callback(ctx.close)
        else:
            browser = await pw.chromium.launch(**options)
            stack.push_async_callback(browser.close)
            ctx = await browser.new_context(
                service_workers="block", accept_downloads=False, locale="zh-CN"
            )
        offline = False
        request_count = 0

        async def read(route):
            nonlocal request_count
            req = route.request
            p = urlsplit(req.url)
            if (
                offline
                or req.method != "GET"
                or p.scheme not in ("https", "http")
                or p.hostname != host
                or req.resource_type
                not in ("document", "script", "stylesheet", "xhr", "fetch")
                or MUTATION.search(p.path)
                or request_count >= 60
            ):
                return await route.abort()
            request_count += 1
            try:
                response = await pinned_get(client, req.url)
                try:
                    if not response.is_success:
                        return await route.abort()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 4_000_000:
                            return await route.abort()
                    await route.fulfill(
                        status=response.status_code,
                        content_type=response.headers.get(
                            "content-type", "application/octet-stream"
                        ),
                        body=bytes(data),
                    )
                finally:
                    await response.aclose()
            except (ValueError, OSError, httpx.HTTPError):
                await route.abort()

        await ctx.route("**/*", read)
        await ctx.route_web_socket("**/*", lambda ws: ws.close())
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=50000)
        try:
            await page.wait_for_function(
                """() => !!document.querySelector('button, input, [role=button], a[href]') ||
                (document.body.innerText.trim().length > 12 && !/正在|加载中|loading/i.test(document.body.innerText.slice(0,100)))""",
                timeout=6000,
            )
        except BrowserError:
            pass
        action = "打开产品首页"
        for _ in range(4):
            if urlsplit(page.url).hostname != host:
                break
            state = await page.evaluate(OBSERVE)
            labels = [el["name"] for el in state["elements"] if el["name"]]
            observations.append(
                {
                    "url": page.url,
                    "title": state["title"],
                    "action": action,
                    "text": state["text"][:3000],
                    "controls": labels[:24],
                }
            )
            control = next_control(state["elements"], visited, host)
            if len(observations) == 4 or state["has_password"] or not control:
                break
            visited.add(control["name"])
            if control.get("offline"):
                offline = True
            try:
                await page.locator(control["selector"]).click(timeout=2000)
                try:
                    await page.wait_for_function(
                        "old => document.body.innerText.slice(0,12000) !== old",
                        arg=state["text"],
                        timeout=1400,
                    )
                except BrowserError:
                    pass
            except BrowserError:
                break
            action = (
                ("断网查看本地入口「" if offline else "点击「") + control["name"] + "」"
            )
    return observations
