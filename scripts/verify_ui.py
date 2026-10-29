"""Actual desktop/mobile UI, local data and DeepSeek; only own new Agent archived."""

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from camoufox.async_api import AsyncCamoufox
from lead_app.config import BROWSER_BUILD, STATE
from lead_app.ops import atomic_json


@asynccontextmanager
async def engine():
    if os.environ.get("LEAD_UI_BROWSER") == "chrome":
        from playwright.async_api import async_playwright

        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            try:
                yield browser
            finally:
                await browser.close()
    else:
        async with AsyncCamoufox(
            browser=BROWSER_BUILD, headless=True, window=(1440, 1000)
        ) as browser:
            yield browser


async def main():
    report = {
        "ok": False,
        "checks": [],
        "browser_build": "installed-chrome"
        if os.environ.get("LEAD_UI_BROWSER") == "chrome"
        else BROWSER_BUILD,
    }
    aid = None
    async with engine() as browser:
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        page.set_default_timeout(15000)
        page.set_default_navigation_timeout(20000)
        failures = []
        page.on("pageerror", lambda e: failures.append(type(e).__name__))
        try:
            report["stage"] = "navigation"
            atomic_json(STATE / "ui-local-progress.json", report)
            await page.goto("http://127.0.0.1:3100/", wait_until="domcontentloaded")
            report["stage"] = "create"
            atomic_json(STATE / "ui-local-progress.json", report)
            await page.get_by_role("button", name="雇用 Agent", exact=True).wait_for()
            async with page.expect_response(
                lambda r: r.url.endswith("/api/agents") and r.request.method == "POST"
            ) as created:
                await page.get_by_role("button", name="雇用 Agent", exact=True).click()
            aid = (await (await created.value).json())["id"]
            box = page.get_by_role("textbox", name="发消息给 Agent", exact=True)
            await box.fill(
                "这是一次本地验收。我提供中文图书翻译工具，面向独立出版社，不要连接任何账号。请先问一个必要问题。"
            )
            await page.get_by_role("button", name="发送", exact=True).click()

            async def detail():
                response = await page.request.get(
                    "http://127.0.0.1:3100/api/agents/" + aid
                )
                assert response.ok
                return await response.json()

            report["stage"] = "chat"
            atomic_json(STATE / "ui-local-progress.json", report)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                data = await detail()
                if (
                    any(m["role"] == "user" for m in data["messages"])
                    and data["messages"][-1]["role"] == "assistant"
                    and data["messages"][-1]["status"] == "complete"
                ):
                    break
                await asyncio.sleep(0.5)
            else:
                raise AssertionError("live_chat_deadline")
            report["stage"] = "advanced"
            atomic_json(STATE / "ui-local-progress.json", report)
            await page.get_by_role("tab", name="更多", exact=True).click()
            await page.get_by_text("高级设置", exact=True).click()
            await page.get_by_text("用量与数据", exact=True).click()
            await page.get_by_text("近 30 天模型用量", exact=True).wait_for()
            async with page.expect_download() as downloaded:
                await page.get_by_role(
                    "button", name="导出我的数据", exact=True
                ).click()
            download = await downloaded.value
            assert download.suggested_filename == "lead-" + aid + ".zip"
            report["checks"] += [
                "ui_creates_agent",
                "real_deepseek_reply_renders",
                "collapsed_advanced_usage",
                "scoped_data_download",
            ]
            await page.screenshot(
                path=str(STATE / "ui-local-desktop.png"), full_page=True
            )
            for width in (1440, 390):
                await page.set_viewport_size({"width": width, "height": 900})
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                ), "horizontal_overflow"
            await page.screenshot(
                path=str(STATE / "ui-local-mobile.png"), full_page=True
            )
            assert not failures, failures
            report["checks"] += [
                "desktop_mobile_no_horizontal_overflow",
                "no_browser_page_errors",
            ]
            report["ok"] = True
        finally:
            await page.screenshot(
                path=str(STATE / "ui-local-latest.png"), full_page=True
            )
            if aid:
                response = await page.request.post(
                    "http://127.0.0.1:3100/api/agents/" + aid + "/control",
                    headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"},
                    data={"action": "archive"},
                )
                assert response.ok
            atomic_json(STATE / "ui-local-verification.json", report)
    print(json.dumps(report))


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(main())
