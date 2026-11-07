"""Browser acceptance against a dedicated analytics test database and local app.

Run with LEAD_ANALYTICS_UI_VERIFY=1 and LEAD_UI_BASE_URL pointing at the isolated
frontend. Only Agents created here are archived; no model or social task runs.
"""

import asyncio
import json
import os
from unittest.mock import patch
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from lead_app import analytics, domain
from lead_app import analytics_ingest as ingest
from lead_app.analytics_reporting import refresh_scope
from lead_app.api import app
from lead_app.config import STATE, database_kwargs
from lead_app.db import all_rows, one, tx, uid
from lead_app.promotion import new_marker
from lead_app.scope import current_tenant
from lead_app.security import token
from playwright.async_api import async_playwright, expect


def seed():
    client = TestClient(app)
    client.cookies.set("lead_session", token("owner"))
    headers = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    records = []
    for label, clicks in (("A", 1), ("B", 2)):
        with patch.object(domain, "emit"), patch.object(analytics, "resolve_public"):
            aid = domain.create_agent()
            result = client.post(
                f"/api/agents/{aid}/analytics",
                headers=headers,
                json={"site_url": "https://site.example"},
            )
        assert result.status_code == 200
        cid = str(result.json()["connection"]["id"])
        marker = new_marker("x")
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.agents SET name=%s,desired_state='paused' WHERE id=%s",
                ("数分验收 " + label, aid),
            )
            c.execute(
                """INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host)
              VALUES(%s,%s,'x',%s,'https://site.example/','site.example')""",
                (current_tenant(), aid, marker),
            )
        for _ in range(clicks):
            assert client.get("/r/" + marker, follow_redirects=False).status_code == 302
        with ingest.scoped_tx(current_tenant(), aid, "live") as c:
            accepted = all_rows(c, "SELECT * FROM lead.analytics_ingest_events")
        for row in accepted:
            assert ingest.process_ingest(ingest.work_args(row))["state"] == "processed"
        if label == "A":
            body = {
                "key": token("analytics:" + cid),
                "event_id": uid(),
                "event_name": "PageView",
                "anonymous_id": uid(),
                "session_id": uid(),
                "url": "https://site.example/",
                "ref": marker,
            }
            for _ in range(3):
                body["event_id"] = uid()
                response = client.post(
                    "/collect/" + cid,
                    headers={"Origin": "https://site.example"},
                    json=body,
                )
                assert response.status_code == 200
            saved = response.json()["attribution"]
            key = client.post(
                f"/api/agents/{aid}/analytics/server-key",
                headers=headers,
                json={"connection_id": cid},
            ).json()["key"]
            for currency, value in (("USD", 12.5), ("EUR", 20)):
                purchase = {
                    "event_name": "Purchase",
                    "event_id": uid(),
                    "connection_id": cid,
                    "lead_attribution": saved,
                    "properties": {
                        "order_id": uid(),
                        "value": value,
                        "currency": currency,
                    },
                }
                assert (
                    client.post(
                        "/collect/server/events",
                        headers={"Authorization": "Bearer " + key},
                        json=purchase,
                    ).status_code
                    == 200
                )
            with tx(aid) as c:
                conn = one(
                    c, "SELECT * FROM lead.analytics_connections WHERE id=%s", (cid,)
                )
            retry = ingest.admit_browser(
                conn,
                ingest.BrowserEvent.model_validate({**body, "event_id": uid()}),
                {},
                inline=True,
            )
            with ingest.scoped_tx(current_tenant(), aid, "live") as c:
                c.execute(
                    "UPDATE lead.analytics_ingest_events SET state='failed',error_code='PROCESSING_UNAVAILABLE' WHERE id=%s",
                    (retry["id"],),
                )
                c.execute(
                    "UPDATE lead.outbox_events SET published_at=now() WHERE id=%s",
                    (retry["outbox_id"],),
                )
        refresh_scope(current_tenant(), aid)
        report = client.get(f"/api/agents/{aid}/analytics/summary").json()
        records.append({"aid": aid, "label": label, "clicks": clicks, "report": report})
    return client, records


async def main():
    base = os.environ.get("LEAD_UI_BASE_URL", "http://127.0.0.1:13100").rstrip("/")
    assert os.environ.get("LEAD_ANALYTICS_UI_VERIFY") == "1", (
        "Explicit test environment required"
    )
    assert database_kwargs()["dbname"].startswith("lead_analytics_"), (
        "Dedicated analytics verification database required"
    )
    assert urlsplit(base).hostname in ("127.0.0.1", "localhost")
    output = STATE / "analytics-ui"
    output.mkdir(exist_ok=True)
    client, records = seed()
    a, b = records
    checks = []
    failures = []
    resources = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1000}
            )
            # Start from the authenticated owner's analytics workflow. Login
            # behavior and identity-provider checks have separate coverage.
            await context.add_cookies(
                [
                    {
                        "name": "lead_session",
                        "value": token("owner"),
                        "url": base,
                        "httpOnly": True,
                        "sameSite": "Strict",
                    }
                ]
            )
            page = await context.new_page()
            page.on("pageerror", lambda error: failures.append(str(error)))
            page.on(
                "response",
                lambda response: (
                    resources.append(
                        {"path": urlsplit(response.url).path, "status": response.status}
                    )
                    if response.status >= 400
                    else None
                ),
            )
            page.on(
                "console",
                lambda message: (
                    failures.append(message.text) if message.type == "error" else None
                ),
            )
            await page.goto(base + "/?agent=" + a["aid"], wait_until="domcontentloaded")
            box = page.locator(f'[data-analytics-agent="{a["aid"]}"]')
            await expect(box.locator('[data-metric="clicks"] dd')).to_have_text("1")
            await expect(box.locator('[data-metric="pv"] dd')).to_have_text("3")
            await expect(box.get_by_text("USD 12.5", exact=True)).to_be_visible()
            await expect(box.get_by_text("EUR 20", exact=True)).to_be_visible()
            await page.screenshot(path=str(output / "desktop.png"))
            checks.append("desktop: real rollups and separate currencies")
            await expect(
                box.get_by_role("region", name="国家与地区统计，可横向滚动")
            ).to_contain_text("未知／未解析")
            await expect(
                box.get_by_role("link", name="IP Geolocation by DB-IP")
            ).to_have_attribute("href", "https://db-ip.com")
            checks.append(
                "geography: unknown remains visible and provider attribution is present"
            )
            await box.get_by_role("button", name="数据状态", exact=False).click()
            dialog = page.get_by_role("dialog", name="数据状态", exact=True)
            await expect(
                dialog.get_by_role("button", name="重试", exact=True)
            ).to_be_visible()
            await dialog.get_by_text("采集保护与保留期", exact=True).first.click()
            quota = dialog.get_by_label("每天接收尝试上限（0 为不限）", exact=True)
            await expect(quota).to_have_value("0")
            await quota.fill("1234")
            await dialog.get_by_role("button", name="保存采集设置", exact=True).click()
            await expect(
                dialog.get_by_role("status").filter(
                    has_text="当前 Agent 的采集设置已保存"
                )
            ).to_be_visible()
            with tx(a["aid"]) as c:
                assert (
                    one(c, "SELECT daily_events FROM lead.analytics_policies")[
                        "daily_events"
                    ]
                    == 1234
                )
            with tx(b["aid"]) as c:
                assert not one(c, "SELECT daily_events FROM lead.analytics_policies")
            checks.append("policy: saved A quota cannot change B settings")
            await page.screenshot(path=str(output / "policy-desktop.png"))
            async with page.expect_response(
                lambda r: r.url.endswith("/retry") and r.request.method == "POST"
            ) as retried:
                await dialog.get_by_role("button", name="重试", exact=True).click()
            assert (await retried.value).status == 200
            await page.keyboard.press("Escape")
            await expect(dialog).not_to_be_visible()
            checks.append("monitor: real failure retry and keyboard dismissal")

            release = asyncio.Event()
            caught = asyncio.Event()

            async def hold(route):
                caught.set()
                await release.wait()
                await route.continue_()

            await page.route(f"**/api/agents/{a['aid']}/analytics/summary?*", hold)
            await box.get_by_role("button", name="刷新获客统计").click()
            await asyncio.wait_for(caught.wait(), 10)
            await (
                page.locator("nav")
                .get_by_role("button", name="数分验收 B", exact=True)
                .click()
            )
            b_box = page.locator(f'[data-analytics-agent="{b["aid"]}"]')
            await expect(b_box.locator('[data-metric="clicks"] dd')).to_have_text("2")
            release.set()
            await expect(b_box.locator('[data-metric="pv"] dd')).to_have_text("未接入")
            await expect(b_box.locator('[data-metric="clicks"] dd')).to_have_text("2")
            await page.unroute(f"**/api/agents/{a['aid']}/analytics/summary?*", hold)
            checks.append("A/B switching: late A response cannot replace B")

            async def wrong_agent(route):
                await route.fulfill(json=a["report"])

            await page.route(
                f"**/api/agents/{b['aid']}/analytics/summary?*", wrong_agent
            )
            await b_box.get_by_role("button", name="刷新获客统计").click()
            await expect(b_box.get_by_role("alert")).to_contain_text("数据归属不匹配")
            await expect(b_box.locator('[data-metric="clicks"] dd')).to_have_text("2")
            await page.unroute(
                f"**/api/agents/{b['aid']}/analytics/summary?*", wrong_agent
            )
            checks.append("mismatched Agent response rejected")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(base + "/?agent=" + b["aid"], wait_until="domcontentloaded")
            await expect(
                page.locator(
                    f'[data-analytics-agent="{b["aid"]}"] [data-metric="clicks"] dd'
                )
            ).to_have_text("2")
            await page.locator(
                f'[data-analytics-agent="{b["aid"]}"] dl'
            ).first.scroll_into_view_if_needed()
            assert await page.evaluate(
                "document.documentElement.scrollWidth<=innerWidth+1"
            )
            canvas = page.locator(".agent-canvas")
            assert await canvas.evaluate("(el)=>el.scrollWidth<=el.clientWidth+1")
            await page.screenshot(path=str(output / "mobile.png"))
            checks.append("390px: clicks without pixel and no document overflow")
            mobile_box = page.locator(f'[data-analytics-agent="{b["aid"]}"]')
            await mobile_box.get_by_role("button", name="数据状态", exact=False).click()
            mobile_dialog = page.get_by_role("dialog", name="数据状态", exact=True)
            await mobile_dialog.get_by_text(
                "采集保护与保留期", exact=True
            ).first.click()
            await expect(
                mobile_dialog.get_by_label("每天接收尝试上限（0 为不限）", exact=True)
            ).to_have_value("0")
            assert await mobile_dialog.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
            await page.screenshot(path=str(output / "policy-mobile.png"))
            checks.append(
                "390px policy: B default quota preserved and form has no overflow"
            )
            await page.keyboard.press("Escape")
            assert not failures, {"console": failures, "resources": resources}
            await browser.close()
        report = {"ok": True, "checks": checks, "page_errors": failures}
        (output / "result.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2)
        )
        print(json.dumps(report, ensure_ascii=False))
    finally:
        for record in records:
            domain.set_control(record["aid"], "archive")


if __name__ == "__main__":
    asyncio.run(main())
