"""Local synthetic UI only: no database, account, credential or social-service access."""

import ast
import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from camoufox.async_api import AsyncCamoufox

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "apps/web"


def browser_build():
    tree = ast.parse((ROOT / "services/api/lead_app/config.py").read_text())
    return next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "BROWSER_BUILD"
            for target in node.targets
        )
    )


def fixture():
    now = datetime.now(timezone.utc).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    base = {
        "evidence_source": "用户设置",
        "scope_label": "当前账号",
        "needs_user": False,
        "policy_version": 1,
        "compatible": True,
    }
    views = {
        "time": {
            **base,
            "state": "waiting_time",
            "reason_label": "等待用户设置的工作时段",
            "next_at": future,
            "resume_event_label": "到期后重新核验",
        },
        "event": {
            **base,
            "state": "waiting_event",
            "reason_label": "来源访问尚未获准",
            "evidence_source": "执行条件",
            "next_at": future,
            "resume_event_label": "条件变化后重查",
        },
        "legacy": {
            **base,
            "state": "unverified",
            "reason_label": "等待依据待核实",
            "evidence_source": "待核实",
            "next_at": future,
            "resume_event_label": "条件变化后重查",
            "policy_version": None,
            "compatible": None,
        },
    }
    accounts = [
        {
            "id": name,
            "platform": "facebook",
            "display_name": label,
            "external_account_id": name,
            "state": "connected",
            "automation_enabled": False,
            "binding_version": version,
            "managed_scope_available": True,
            "managed_scope_authorized": False,
        }
        for name, label, version in [
            ("account-a", "合成账号 A", 3),
            ("account-b", "合成账号 B", 8),
        ]
    ]
    accounts.append(
        {
            "id": "reddit-account",
            "platform": "reddit",
            "display_name": "合成社区账号",
            "external_account_id": "reddit-fixture",
            "state": "connected",
            "automation_enabled": True,
            "binding_version": 1,
            "managed_scope_available": True,
            "managed_scope_authorized": False,
            "member_scope_authorized": True,
            "channel_waits": [
                {
                    "lane": "research",
                    "execution_cadence": {**views["event"], "next_at": None},
                }
            ],
        }
    )
    board = {
        "metrics": {
            key: {"count": 1 if key == "joined" else 0, "records": []}
            for key in [
                "candidate",
                "joined",
                "pending",
                "join_unknown",
                "exit",
                "interaction",
                "inbox_reply",
                "promotion",
            ]
        },
        "current": "本轮核验未找到新的合格机会",
        "next": "等待新的合格机会或规则变化",
        "constraint": "近期没有可由群资料回答的新问题",
        "owner": None,
        "at": now,
        "participation": {
            "scope": ["答疑仅匹配群内原问题"],
            "policy_reported": True,
            "next_eligible_at": None,
            "reason": None,
        },
        "execution": {
            "totals": {},
            "facts": {
                "membership_confirmed": 1,
                "message_confirmed": 0,
                "pending_receipt": 0,
                "skipped": 1,
            },
            "outcomes": {"skipped": 1},
            "waiting": [],
            "gates": [],
            "timings": [],
            "execution_cadence": views["legacy"],
        },
    }
    return {
        "agent": {
            "id": "cadence-qa",
            "name": "合成测试",
            "accounts": accounts,
            "control_version": 7,
            "desired_state": "running",
            "health_state": "queued",
            "onboarding_state": {"operating_timezone": "Asia/Shanghai"},
        },
        "board": board,
        "views": views,
    }


async def verify():
    data = fixture()
    run = uuid4().hex[:10]
    page_dir = WEB / "app" / ("cadence-check-" + run)
    dist_name = ".next-cadence-check-" + run
    output = ROOT / ".state/verification/evidence-cadence-ui" / run
    output.mkdir(parents=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    backups = {
        p: p.read_bytes() if p.exists() else None
        for p in [WEB / "next-env.d.ts", WEB / "tsconfig.json"]
    }
    generated = {}
    page_dir.mkdir()
    source = """"use client";
import {useState} from "react";
import CadenceStatus from "../execution-cadence";
import TelegramGroupCard from "../telegram-group-card";
import SocialAccounts from "../social-accounts";
import {api} from "../api-client";
const fixtures=FIXTURES;
export default function Check(){
 const [agent,setAgent]=useState<any>(fixtures.agent);
 const [connect,setConnect]=useState("");
 return <main style={{maxWidth:760,margin:"24px auto",padding:16,containerType:"inline-size",containerName:"workspace"}}>
  <h1>本地合成数据验证</h1>
  <SocialAccounts agent={agent} platforms={[{id:"facebook",name:"Facebook",automation:false}]} readOnly={false} onConnect={(p)=>setConnect(p.account_id)} onComplete={async()=>setAgent(await api("/agents/cadence-qa"))}/>
  {([['time','时间等待'],['event','条件等待'],['legacy','兼容待核实']] as const).map(([key,label])=><section key={key} aria-label={label}><h2>{label}</h2><CadenceStatus value={fixtures.views[key] as any} zone="Asia/Shanghai"/></section>)}
  <p data-testid="connect-choice">{connect}</p>
  <TelegramGroupCard board={fixtures.board as any} zone="Asia/Shanghai" history={()=>{}}/>
 </main>;
}
""".replace("FIXTURES", json.dumps(data, ensure_ascii=False))
    (page_dir / "page.tsx").write_text(source)
    log = (output / "server.log").open("w")
    process = subprocess.Popen(
        [
            "node",
            str(WEB / "node_modules/next/dist/bin/next"),
            "dev",
            "--hostname",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=WEB,
        env={
            **os.environ,
            "LEAD_WEB_DIST_DIR": dist_name,
            "LEAD_API_ORIGIN": "http://127.0.0.1:1",
            "NEXT_TELEMETRY_DISABLED": "1",
        },
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    report = {
        "ok": False,
        "fixture": "local-only",
        "accounts_used": 0,
        "browser": browser_build(),
        "checks": [],
        "external_requests": [],
        "api_requests": [],
        "screenshots": [],
    }
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Local UI server exited")
                try:
                    if (
                        await client.get(base + "/" + page_dir.name)
                    ).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.3)
            else:
                raise RuntimeError("Local UI startup timed out")
        generated = {p: p.read_bytes() if p.exists() else None for p in backups}
        async with AsyncCamoufox(browser=browser_build(), headless=True) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1050})
            page.set_default_timeout(12000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def route(request):
                parsed = urlsplit(request.request.url)
                if (
                    parsed.hostname not in {"127.0.0.1", "localhost"}
                    or parsed.port != port
                ):
                    report["external_requests"].append(request.request.url)
                    await request.abort()
                    return
                if parsed.path.startswith("/api/"):
                    report["api_requests"].append(
                        {"method": request.request.method, "path": parsed.path}
                    )
                    if (
                        parsed.path == "/api/agents/cadence-qa"
                        and request.request.method == "GET"
                    ):
                        await request.fulfill(json=data["agent"])
                        return
                    if parsed.path.endswith("/managed-scope"):
                        selected = parsed.path.split("/")[-2]
                        body = request.request.post_data_json
                        account = next(
                            a for a in data["agent"]["accounts"] if a["id"] == selected
                        )
                        assert body == {
                            "binding_version": account["binding_version"],
                            "expected_control_version": 7,
                        }
                        if selected == "account-b":
                            await request.fulfill(
                                status=409,
                                json={"detail": "账号连接已变化，请刷新后重新确认"},
                            )
                            return
                        account.update(
                            automation_enabled=True,
                            managed_scope_authorized=True,
                            channel_waits=[
                                {
                                    "lane": "research",
                                    "execution_cadence": {
                                        **data["views"]["event"],
                                        "next_at": None,
                                    },
                                }
                            ],
                        )
                        await request.fulfill(json={"ok": True, "account_id": selected})
                        return
                    await request.fulfill(
                        status=501, json={"detail": "Unknown synthetic API route"}
                    )
                    return
                await request.continue_()

            await page.route("**/*", route)
            await page.goto(base + "/" + page_dir.name, wait_until="networkidle")
            event = page.get_by_role("region", name="条件等待", exact=True)
            assert "条件变化后重查" in await event.inner_text()
            assert "后重新核验" not in await event.inner_text()
            assert (
                "后重新核验"
                in await page.get_by_role(
                    "region", name="时间等待", exact=True
                ).inner_text()
            )
            assert (
                "执行条件与版本尚待核实"
                in await page.get_by_role(
                    "region", name="兼容待核实", exact=True
                ).inner_text()
            )
            await page.get_by_text("执行进度与等待原因", exact=True).click()
            assert (
                "已确认入群 1 次，已确认回复 0 条"
                in await page.locator("main").inner_text()
            )
            assert (
                "本轮核验未找到新的合格机会" in await page.locator("main").inner_text()
            )
            report["checks"] += [
                "time_deadline",
                "event_has_no_countdown",
                "legacy_unverified",
                "zero_opportunity",
                "join_survives_skipped_reply",
            ]
            await page.screenshot(path=str(output / "desktop.png"), full_page=True)
            report["screenshots"].append("desktop.png")
            await page.locator('button[data-platform="facebook"]').click()
            await page.get_by_role("combobox", name="选择 Facebook 账号").select_option(
                "account-a"
            )
            button = page.get_by_role(
                "button", name="授权此账号运营管理目标", exact=True
            )
            await button.focus()
            await page.keyboard.press("Enter")
            await page.get_by_text(
                "此账号已获管理目标的文字运营授权。来源、目标和发布条件仍会分别核验。",
                exact=True,
            ).wait_for()
            assert '运营已启用，外部操作等待条件变化' in await page.get_by_role('dialog').inner_text()
            assert data["agent"]["accounts"][1]["automation_enabled"] is False
            report['checks'].append('waiting_is_not_presented_as_active_work')
            report["checks"] += [
                "keyboard_authorization",
                "exact_account_and_binding",
                "sibling_unchanged",
                "permission_gap_visible",
            ]
            await page.screenshot(
                path=str(output / "account-desktop.png"), full_page=True
            )
            report["screenshots"].append("account-desktop.png")
            await page.get_by_role("combobox", name="选择 Facebook 账号").select_option(
                "account-b"
            )
            await page.get_by_role(
                "button", name="授权此账号运营管理目标", exact=True
            ).click()
            await page.get_by_role("alert").filter(has_text="账号连接已变化").wait_for()
            assert data["agent"]["accounts"][1]["automation_enabled"] is False
            for width in (390, 320):
                await page.set_viewport_size({"width": width, "height": 950})
                assert await page.evaluate(
                    "document.documentElement.scrollWidth <= innerWidth"
                ), width
                await page.screenshot(
                    path=str(output / f"account-{width}.png"), full_page=True
                )
                report["screenshots"].append(f"account-{width}.png")
            report["checks"] += [
                "binding_conflict_keeps_selection",
                "mobile_390",
                "mobile_320",
            ]
            await page.get_by_role("button", name="完成", exact=True).click()
            await page.screenshot(path=str(output / "mobile-320.png"), full_page=True)
            report["screenshots"].append("mobile-320.png")
            await page.locator('button[data-platform="facebook"]').click()
            await page.get_by_role("button", name="重新登录", exact=True).click()
            assert (
                await page.get_by_test_id("connect-choice").inner_text() == "account-b"
            )
            report["checks"].append("selected_account_relogin")
            await page.locator('button[data-platform="reddit"]').click()
            await page.get_by_text(
                "仅按已授权的社区参与范围投稿，发前仍核验社区规则和平台条件。",
                exact=True,
            ).wait_for()
            assert "来源访问尚未获准" in await page.get_by_role("dialog").inner_text()
            report["checks"].append("reddit_member_source_wait_visible")
            assert not errors, errors
            assert not report["external_requests"], report["external_requests"]
            report["ok"] = True
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        log.close()
        shutil.rmtree(page_dir)
        shutil.rmtree(WEB / dist_name, ignore_errors=True)
        for path, before in backups.items():
            if path.exists() and generated.get(path) == path.read_bytes():
                if before is None:
                    path.unlink()
                else:
                    path.write_bytes(before)
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(
            json.dumps(
                {
                    "ok": report["ok"],
                    "checks": len(report["checks"]),
                    "report": str(output / "report.json"),
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    asyncio.run(verify())
