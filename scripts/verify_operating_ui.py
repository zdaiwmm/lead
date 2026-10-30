"""Isolated desktop/mobile UI check using real locally rendered media.

The temporary page and mocked operations API are QA fixtures only. No browser
account, workspace credential, or external service is accessed.
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))

import httpx
from camoufox.async_api import AsyncCamoufox
from lead_app.config import BROWSER_BUILD, ROOT, STATE
from lead_app.operating_playbooks import catalog


async def verify():
    web = ROOT / "apps/web"
    page_dir = web / "app" / "creative-check"
    output = STATE / "verification" / "creative-autonomy"
    next_env = web / "next-env.d.ts"
    original_next_env = next_env.read_text()
    if page_dir.exists():
        raise ValueError("本地检查页面已存在，请先核对")
    if not (output / "report.json").exists():
        raise ValueError("请先执行 verify_creative_runtime.py")
    page_dir.mkdir()
    page_file = page_dir / "page.tsx"
    page_file.write_text("""import OperatingPanel from "../operating-panel";
export default function Check(){return <main style={{maxWidth:760,margin:"40px auto",padding:20}}><p>本地素材与页面验证</p><OperatingPanel aid="creative-qa" /></main>}""")
    log = (output / "ui-server.log").open("w")
    process = subprocess.Popen(
        [
            "node",
            str(web / "node_modules/next/dist/bin/next"),
            "dev",
            "--hostname",
            "127.0.0.1",
            "--port",
            "3102",
        ],
        cwd=web,
        env={**os.environ, "LEAD_WEB_DIST_DIR": ".next-creative-qa"},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    data = {
        "channels": [
            {
                "platform": c["platform"],
                "preparation": {"result": {"plans": c["plans"]}},
            }
            for c in catalog()["channels"]
        ],
        "reviews": [],
        "media_jobs": [
            {
                "id": "local-image",
                "platform": "instagram",
                "headline": "Keep key terms consistent",
                "kind": "image",
                "purpose": "account_free_preview",
                "state": "succeeded",
                "assets": [{"id": "poster", "role": "publication", "kind": "image"}],
            },
            {
                "id": "local-video",
                "platform": "tiktok",
                "headline": "Prepare, configure and review",
                "kind": "video",
                "purpose": "account_free_preview",
                "state": "succeeded",
                "assets": [
                    {"id": "video", "role": "publication", "kind": "video"},
                    {"id": "preview", "role": "preview", "kind": "image"},
                ],
            },
        ],
    }
    files = {
        "poster": output / "instagram-image/poster.png",
        "preview": output / "tiktok-video/preview.png",
        "video": output / "tiktok-video/explainer.mp4",
    }
    report = {"ok": False, "fixture": "local-only", "accounts_used": 0, "checks": []}
    try:
        deadline = time.monotonic() + 50
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("检查页面服务未启动")
                try:
                    if (
                        await client.get("http://127.0.0.1:3102/creative-check")
                    ).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.4)
            else:
                raise RuntimeError("检查页面启动超时")
        async with AsyncCamoufox(browser=BROWSER_BUILD, headless=True) as browser:
            page = await browser.new_page(viewport={"width": 1100, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))

            async def route(request):
                url = request.request.url
                if url.endswith("/operations"):
                    await request.fulfill(json=data)
                    return
                asset = url.rsplit("/", 1)[-1]
                if asset in files:
                    await request.fulfill(
                        path=str(files[asset]),
                        content_type="video/mp4" if asset == "video" else "image/png",
                    )
                    return
                await request.abort()

            await page.route("**/api/agents/creative-qa/**", route)
            await page.goto(
                "http://127.0.0.1:3102/creative-check", wait_until="networkidle"
            )
            await page.get_by_text("10 个渠道已备齐方案").wait_for()
            button = page.get_by_role("button", name="查看", exact=True)
            await button.focus()
            await page.keyboard.press("Enter")
            await page.get_by_role("heading", name="最近制作", exact=True).wait_for()
            assert await page.get_by_role("link", name="查看素材").count() == 2
            await page.get_by_role("combobox", name="筛选素材渠道").select_option(
                "tiktok"
            )
            assert await page.get_by_role("link", name="查看素材").count() == 1
            await page.get_by_role("combobox", name="筛选素材渠道").select_option("")
            report["checks"].extend(
                ["keyboard_expand", "real_media_previews", "channel_filter"]
            )
            for width, name in [(1100, "desktop"), (390, "mobile")]:
                await page.set_viewport_size({"width": width, "height": 1000})
                await page.screenshot(
                    path=str(output / f"ui-{name}.png"), full_page=True
                )
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), name
                assert await page.locator("img").evaluate_all(
                    "images=>images.every(i=>i.complete&&i.naturalWidth>0)"
                )
                report["checks"].append(name + "_no_overflow")
            assert not errors, errors
            report["ok"] = True
            (output / "ui-report.json").write_text(json.dumps(report, indent=2))
            print(json.dumps(report))
    finally:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        log.close()
        # Remove exactly the page created by this verifier, never user source.
        page_file.unlink(missing_ok=True)
        if page_dir.is_dir() and not any(page_dir.iterdir()):
            page_dir.rmdir()
        tsconfig = web / "tsconfig.json"
        config = json.loads(tsconfig.read_text())
        config["include"] = [
            p for p in config["include"] if not p.startswith(".next-creative-qa/")
        ]
        tsconfig.write_text(json.dumps(config, indent=2) + "\n")
        if ".next-creative-qa/" in next_env.read_text():
            next_env.write_text(original_next_env)


if __name__ == "__main__":
    asyncio.run(verify())
