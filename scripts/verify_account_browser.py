"""Exercise the real account browser component with isolated, mocked HTTP endpoints.

Run: .venv/bin/python scripts/verify_account_browser.py
No database, social account, or running application is changed.
"""

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web"
AID = "11111111-1111-4111-8111-111111111111"
TAKEOVER = "22222222-2222-4222-8222-222222222222"
ACCOUNT = "33333333-3333-4333-8333-333333333333"
SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="900"><rect width="1280" height="900" fill="white"/></svg>'


async def scenarios(base):
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        try:
            for case in (
                "queued",
                "screenshot",
                "input_error",
                "timeout",
                "expired",
                "verify_error",
                "close",
                "debounce",
                "unmount",
                "managed",
            ):
                page = await browser.new_page()
                calls, errors = [], []
                entered, release, frame_entered = (
                    asyncio.Event(),
                    asyncio.Event(),
                    asyncio.Event(),
                )
                hold_frame = False
                page.on("pageerror", lambda e: errors.append(str(e)))

                async def handle(route):
                    nonlocal hold_frame
                    request = route.request
                    path = request.url.split("?", 1)[0]
                    if path.endswith("/connect"):
                        await route.fulfill(
                            json={
                                "takeover_id": TAKEOVER,
                                "account_id": ACCOUNT,
                                "managed_target_start": case == "managed",
                            }
                        )
                    elif path.endswith("/browser/input"):
                        calls.append(request.post_data_json["kind"])
                        entered.set()
                        if case == "screenshot":
                            hold_frame = True
                        elif len(calls) == 1:
                            await release.wait()
                        if case in ("input_error", "expired"):
                            await route.fulfill(
                                status=409,
                                json={
                                    "detail": "接管已过期"
                                    if case == "expired"
                                    else "输入服务暂时不可用"
                                },
                            )
                        else:
                            await route.fulfill(json={"ok": True})
                    elif path.endswith("/accounts/verify-login"):
                        assert request.post_data_json == {
                            "platform": "facebook",
                            "account_id": ACCOUNT,
                            "start_managed_targets": case == "managed",
                        }
                        calls.append("verify")
                        if case == "verify_error" and calls.count("verify") == 1:
                            await route.fulfill(
                                status=409, json={"detail": "尚未检测到登录，请重试"}
                            )
                        else:
                            await route.fulfill(json={"ok": True})
                    elif path.endswith("/takeover/end"):
                        assert request.url.endswith("?takeover=" + TAKEOVER)
                        calls.append("end")
                        await route.fulfill(json={"ok": True})
                    elif "/browser/screenshot" in path or "/browser-control/" in path:
                        # The former combined input + screenshot request reproduces the original bug.
                        if request.method == "POST":
                            calls.append(request.post_data_json["kind"])
                            entered.set()
                            await release.wait()
                        if hold_frame:
                            frame_entered.set()
                            await release.wait()
                        await route.fulfill(
                            body=SVG,
                            content_type="image/svg+xml",
                            headers={
                                "x-lead-frame-width": "1280",
                                "x-lead-frame-height": "900",
                            },
                        )
                    else:
                        raise AssertionError(f"Unexpected API request: {path}")

                await page.route("**/api/**", handle)
                await page.route("**/browser-control/**", handle)
                try:
                    await page.goto(base)
                    picture = page.get_by_role(
                        "img", name="Facebook 登录页面", exact=False
                    )
                    verify = page.get_by_role(
                        "button",
                        name="绑定并启动获客" if case == "managed" else "验证并完成",
                        exact=True,
                    )
                    close = page.get_by_role("button", name="关闭账号浏览器")
                    await expect(picture).to_be_visible()
                    if case == "timeout":
                        await page.clock.install()
                    if case == "managed":
                        await verify.click()
                        await expect(
                            page.get_by_text("已关闭", exact=True)
                        ).to_be_visible()
                        await expect(
                            page.get_by_text("started:true", exact=True)
                        ).to_be_visible()
                        assert calls == ["verify", "end"], calls
                    elif case == "debounce":
                        # Submit text and a debounced wheel immediately before verification.
                        await page.get_by_role("textbox").fill("登录")
                        await page.evaluate("""() => {
                          document.querySelector('.account-browser-screen').dispatchEvent(new WheelEvent('wheel', {deltaY: 100, bubbles: true}));
                          document.querySelector('button.primary').click();
                        }""")
                        await entered.wait()
                        release.set()
                        await expect(
                            page.get_by_text("已关闭", exact=True)
                        ).to_be_visible()
                        assert calls == ["text", "scroll", "verify", "end"], calls
                    elif case == "verify_error":
                        await verify.click()
                        await expect(
                            page.get_by_role("dialog").get_by_role("alert")
                        ).to_contain_text("尚未检测到登录")
                        assert calls == ["verify"], calls
                        await verify.click()
                        await expect(
                            page.get_by_text("已关闭", exact=True)
                        ).to_be_visible()
                        assert calls == ["verify", "verify", "end"], calls
                    else:
                        await picture.click(position={"x": 20, "y": 20})
                        await asyncio.wait_for(entered.wait(), 5)
                        if case == "screenshot":
                            await asyncio.wait_for(frame_entered.wait(), 5)
                        if case == "queued":
                            await page.get_by_role("textbox").fill("abc")
                            await page.get_by_role("textbox").press("Enter")
                        if case == "close":
                            await close.click()
                            await expect(close).to_be_disabled()
                        else:
                            # Two synchronous clicks must not create concurrent verification calls.
                            await verify.evaluate("el => { el.click(); el.click(); }")
                            if case != "screenshot":
                                await expect(
                                    page.get_by_role("button", name="正在完成验证…")
                                ).to_be_disabled()
                        if case == "screenshot":
                            await expect(
                                page.get_by_text("已关闭", exact=True)
                            ).to_be_visible()
                            assert calls == ["click", "verify", "end"], calls
                        elif case == "unmount":
                            await page.get_by_role(
                                "button", name="移除测试组件"
                            ).click()
                            release.set()
                            await expect(
                                page.get_by_text("已关闭", exact=True)
                            ).to_be_visible()
                            assert calls == ["click"], calls
                        elif case == "timeout":
                            await page.clock.fast_forward(45_001)
                            await expect(
                                page.get_by_role("dialog").get_by_role("alert")
                            ).to_contain_text("输入响应超时")
                            await expect(verify).to_be_enabled()
                            assert calls == ["click"], calls
                            release.set()
                            await verify.click()
                            await expect(
                                page.get_by_text("已关闭", exact=True)
                            ).to_be_visible()
                            assert calls == ["click", "verify", "end"], calls
                        else:
                            assert "verify" not in calls and "end" not in calls, calls
                            release.set()
                            if case in ("input_error", "expired"):
                                await expect(
                                    page.get_by_role("dialog").get_by_role("alert")
                                ).to_contain_text(
                                    "接管已结束"
                                    if case == "expired"
                                    else "输入服务暂时不可用"
                                )
                                assert calls == ["click"], calls
                                if case == "expired":
                                    await expect(verify).to_be_disabled()
                                    await close.click()
                                else:
                                    await verify.click()
                                await expect(
                                    page.get_by_text("已关闭", exact=True)
                                ).to_be_visible()
                                assert calls == (
                                    ["click", "end"]
                                    if case == "expired"
                                    else ["click", "verify", "end"]
                                ), calls
                            else:
                                await expect(
                                    page.get_by_text("已关闭", exact=True)
                                ).to_be_visible()
                                assert calls == (
                                    ["click", "end"]
                                    if case == "close"
                                    else ["click", "text", "key", "verify", "end"]
                                ), calls
                    assert not errors, errors
                    print(f"PASS {case}: {calls}", flush=True)
                finally:
                    release.set()
                    await page.close()
        finally:
            await browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--component", type=Path, default=WEB / "app/account-browser.tsx"
    )
    args = parser.parse_args()
    (ROOT / ".state").mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(
        prefix="account-browser-", dir=ROOT / ".state"
    ) as directory:
        project = Path(directory)
        app = project / "app"
        app.mkdir()
        (project / "node_modules").symlink_to(
            WEB / "node_modules", target_is_directory=True
        )
        (project / "package.json").write_text(
            json.dumps(
                {
                    "private": True,
                    "dependencies": json.loads((WEB / "package.json").read_text())[
                        "dependencies"
                    ],
                }
            )
        )
        shutil.copy(args.component, app / "account-browser.tsx")
        for name in ("api-client.ts", "social-icon.tsx"):
            shutil.copy(WEB / "app" / name, app / name)
        (app / "layout.tsx").write_text(
            "export default function Layout({children}) {return <html><body>{children}</body></html>}"
        )
        (app / "page.tsx").write_text(
            '''"use client";
import {useState} from "react";
import AccountBrowser from "./account-browser";
export default function Page() {
  const [open, setOpen] = useState(true);
  const [started, setStarted] = useState(false);
  return <><button onClick={() => setOpen(false)}>移除测试组件</button>
    {open ? <AccountBrowser aid="'''
            + AID
            + """" platform="facebook" name="Facebook" onClose={() => setOpen(false)} onVerified={async value => {setStarted(Boolean(value)); setOpen(false)}}/> : <><p>已关闭</p><p>started:{String(started)}</p></>}</>;
}"""
        )
        (project / "next.config.js").write_text(
            "module.exports = {reactStrictMode: true, devIndicators: false};"
        )
        base = f"http://127.0.0.1:{port}"
        with (project / "server.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    str(WEB / "node_modules/.bin/next"),
                    "dev",
                    "--webpack",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=project,
                stdout=log,
                stderr=log,
                env={**os.environ, "NEXT_TELEMETRY_DISABLED": "1"},
            )
            try:

                async def run():
                    for _ in range(120):
                        if process.poll() is not None:
                            raise RuntimeError("Isolated frontend exited")
                        try:
                            await asyncio.to_thread(
                                urllib.request.urlopen, base, timeout=1
                            )
                            break
                        except (OSError, TimeoutError):
                            await asyncio.sleep(0.25)
                    else:
                        raise RuntimeError("Isolated frontend did not start")
                    await scenarios(base)

                asyncio.run(run())
            except Exception:
                log.seek(0)
                print(log.read()[-4000:])
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
