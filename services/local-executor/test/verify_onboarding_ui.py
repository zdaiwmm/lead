"""Real-browser fixture of first-project Codex onboarding; no model or accounts."""
import asyncio
import json
import os
import socket
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = '''"use client";
import {useState,useEffect} from "react";
import WebsiteAgentDialog from "../website-agent-dialog";
import {api,setWorkspace} from "../api-client";
export default function Fixture(){const [open,setOpen]=useState(false),[created,setCreated]=useState("");
useEffect(()=>{setWorkspace("fixture-workspace");void api("/auth/me");},[]);
return <main style={{padding:24}}><h1>首次接入验证</h1><button onClick={()=>setOpen(true)}>新建项目</button>
<p data-created>{created}</p>{open&&<WebsiteAgentDialog platforms={[]} onClose={()=>setOpen(false)} onCreated={async id=>{setCreated(id);setOpen(false);}}/>}</main>}'''


async def verify():
    import httpx
    from playwright.async_api import async_playwright

    web = ROOT / "apps/web"
    fixture = web / "app/onboarding-check"
    if fixture.exists():
        raise RuntimeError("Refusing to replace existing verification route")
    fixture.mkdir()
    (fixture / "page.tsx").write_text(FIXTURE)
    original = {name: (web / name).read_bytes() for name in ("tsconfig.json", "next-env.d.ts")}
    report = Path(os.environ.get("LEAD_ONBOARDING_REPORT", "/private/tmp/lead-codex-onboarding-ui-20261114"))
    report.mkdir(parents=True, exist_ok=True)
    sock = socket.socket();sock.bind(("127.0.0.1", 0));port = sock.getsockname()[1];sock.close()
    base = f"http://127.0.0.1:{port}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LEAD_", "OPENAI_", "DEEPSEEK_", "ANTHROPIC_"))}
    env.update(LEAD_WEB_DIST_DIR=".next-onboarding-check", LEAD_API_ORIGIN="http://127.0.0.1:1")
    log = (report / "server.log").open("w")
    process = subprocess.Popen(["node", str(web / "node_modules/next/dist/bin/next"), "dev", "--webpack", "--hostname", "127.0.0.1", "--port", str(port)], cwd=web, env=env, stdout=log, stderr=subprocess.STDOUT)
    errors, drafts, analyses, confirms = [], [], [], []
    paired = False
    draft_id = "00000000-0000-4000-8000-000000000018"
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Verification frontend exited")
                try:
                    if (await client.get(base + "/onboarding-check")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.3)
            else:
                raise RuntimeError("Verification frontend did not start")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})
                async def route(route):
                    nonlocal paired
                    request = route.request
                    if not request.url.startswith(base + "/"):
                        return await route.abort()
                    if "/api/" not in request.url:
                        return await route.continue_()
                    path = request.url.split("/api/", 1)[1]
                    body = request.post_data_json if request.method == "POST" else None
                    if path == "auth/me":
                        return await route.fulfill(json={"role": "owner", "capabilities": ["agents.control", "agents.chat"]})
                    if path == "agents/setup/draft":
                        drafts.append(body)
                        return await route.fulfill(json={"id": draft_id})
                    if path.endswith("/executor/pairing"):
                        return await route.fulfill(json={"code": "fixture-once-only", "expires_at": "2099-01-01T00:00:00Z", "connect_command": "lead-executor pair --url https://fixture.example --code fixture-once-only"})
                    if path.endswith("/executor"):
                        return await route.fulfill(json={"agent_id": draft_id, "mode": "codex" if paired else "legacy", "device_id": "device" if paired else None, "devices": [{"id": "device", "name": "本机", "state": "active", "online": True}] if paired else [], "requests": [], "pending_inputs": []})
                    if path == "agents/setup/analyze":
                        analyses.append(body)
                        if len(analyses) <= 2:
                            return await route.fulfill(json={"executor_pending": True})
                        return await route.fulfill(json={"product_url": "https://example.test", "product_name": "示例产品", "introduction": "根据官网确认的产品介绍", "features": [], "customers": [], "scenarios": [], "advantages": []})
                    if path == "agents/setup/confirm":
                        confirms.append(body)
                        return await route.fulfill(json={"id": draft_id})
                    return await route.fulfill(json={})
                await context.route("**/*", route)
                page = await context.new_page();page.on("pageerror", lambda error: errors.append(str(error)))
                await page.goto(base + "/onboarding-check", wait_until="networkidle")
                await page.get_by_role("button", name="新建项目", exact=True).click()
                await page.get_by_label("官网网址", exact=True).fill("https://example.test")
                await page.get_by_role("button", name="连接本机 Codex", exact=True).click()
                await page.get_by_text("草稿只用于连接和整理资料，确认创建前不会开始运营。", exact=True).wait_for()
                assert len(drafts) == 1 and drafts[0]["client_id"]
                await page.get_by_text("本机执行尚未启用", exact=True).wait_for()
                await page.get_by_role("button", name="根据官网生成", exact=True).click()
                await page.get_by_role("alert").filter(has_text="先连接本机 Codex").wait_for()
                assert not analyses
                await page.get_by_role("button", name="设备与连接", exact=True).click()
                await page.get_by_role("button", name="生成连接指令", exact=True).click()
                await page.get_by_text("lead-executor pair --url https://fixture.example --code fixture-once-only", exact=True).wait_for()
                for width, label in ((1440, "desktop-pair"), (390, "narrow-pair"), (320, "small-pair")):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                    await page.screenshot(path=str(report / f"{label}.png"), full_page=True)
                await page.get_by_role("button", name="关闭新建 Agent", exact=True).click()
                assert not confirms
                await page.get_by_role("button", name="新建项目", exact=True).click()
                await page.get_by_text("草稿只用于连接和整理资料，确认创建前不会开始运营。", exact=True).wait_for()
                assert len(drafts) == 1
                paired = True
                await page.evaluate("window.dispatchEvent(new Event('lead:updated'))")
                await page.get_by_text("本机 已连接", exact=True).wait_for()
                for width, label in ((1440, "desktop-ready"), (390, "narrow-ready"), (320, "small-ready")):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), label
                    await page.screenshot(path=str(report / f"{label}.png"), full_page=True)
                await page.get_by_role("button", name="根据官网生成", exact=True).click()
                for _ in range(30):
                    if analyses: break
                    await asyncio.sleep(0.1)
                assert len(analyses) == 1
                await page.get_by_role("button", name="关闭新建 Agent", exact=True).click()
                await page.get_by_role("button", name="新建项目", exact=True).click()
                await page.get_by_role("button", name="根据官网生成", exact=True).click()
                await page.wait_for_function("document.querySelector('input[maxlength=\"80\"]')?.value === '示例产品'")
                assert await page.get_by_label("产品名称", exact=True).input_value() == "示例产品"
                assert len(analyses) == 3
                assert analyses[0]["agent_id"] == draft_id
                assert analyses[0]["request_id"] == analyses[1]["request_id"] == analyses[2]["request_id"]
                saved = await page.evaluate("JSON.parse(localStorage.getItem('lead.agent-setup.fixture-workspace.website.v1'))")
                assert saved["draftAgentId"] == draft_id
                await page.get_by_role("button", name="确认并创建 Agent", exact=True).click()
                await page.locator("[data-created]").filter(has_text=draft_id).wait_for()
                assert len(confirms) == 1 and confirms[0]["draft_agent_id"] == draft_id
                # Existing server-model users can still confirm an entirely blank form.
                await page.get_by_role("button", name="新建项目", exact=True).click()
                await page.get_by_role("button", name="确认并创建 Agent", exact=True).click()
                await page.get_by_role("dialog").wait_for(state="detached")
                assert len(confirms) == 2 and "draft_agent_id" not in confirms[1]
                assert confirms[1]["features"] == [] and confirms[1]["product_url"] == ""
                assert len(drafts) == 1
                assert not errors, errors
                (report / "result.json").write_text(json.dumps({"ok": True, "draft_calls": len(drafts), "analysis_calls": len(analyses), "confirm_calls": len(confirms), "widths": [1440, 390, 320], "page_errors": errors}, ensure_ascii=False, indent=2))
            finally:
                await browser.close()
    finally:
        process.terminate()
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()
        (fixture / "page.tsx").unlink(missing_ok=True);fixture.rmdir()
        for name, data in original.items():(web / name).write_bytes(data)


if __name__ == "__main__":
    asyncio.run(verify())
