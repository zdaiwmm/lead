"""Real local website -> shipped SDK -> queue -> server callback, isolated DB only."""

import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app import domain
from lead_app.config import STATE, database_kwargs
from lead_app.db import all_rows, one, tx
from lead_app.promotion import new_marker
from lead_app.scope import current_tenant
from lead_app.security import token


async def run():
    if not database_kwargs()["dbname"].startswith("lead_analytics_"):
        raise ValueError("Dedicated verification database required")
    base = os.environ.get("LEAD_BROWSER_COLLECT_ORIGIN", "http://127.0.0.1:18188")
    if base != "http://127.0.0.1:18188":
        raise ValueError("Dedicated loopback API required")
    origin = "http://127.0.0.1:18787"
    records = {}
    payments = {}
    checks = []
    errors = []
    admin = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    async with httpx.AsyncClient(
        base_url=base, timeout=30, cookies={"lead_session": token("owner")}
    ) as api:
        with patch.object(domain, "emit"):
            for label in ("A", "B"):
                aid = domain.create_agent()
                with tx(aid) as c:
                    c.execute(
                        "UPDATE lead.agents SET name=%s,desired_state='paused' WHERE id=%s",
                        ("数分网站验收 " + label, aid),
                    )
                response = await api.post(
                    f"/api/agents/{aid}/analytics",
                    headers=admin,
                    json={"site_url": origin},
                )
                response.raise_for_status()
                conn = response.json()["connection"]
                cid = str(conn["id"])
                key = (
                    await api.post(
                        f"/api/agents/{aid}/analytics/server-key",
                        headers=admin,
                        json={"connection_id": cid},
                    )
                ).json()["key"]
                marker = new_marker("x")
                with tx(aid) as c:
                    c.execute(
                        "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,'x',%s,%s,'127.0.0.1')",
                        (
                            current_tenant(),
                            aid,
                            marker,
                            origin + "/" + label + "?email=private-fixture#secret",
                        ),
                    )
                records[label] = {
                    "collect_origin": response.json()["public_url"],
                    "aid": aid,
                    "cid": cid,
                    "key": key,
                    "snippet": conn["snippet"],
                    "marker": marker,
                }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                label = urlsplit(self.path).path.strip("/")
                if label not in records:
                    self.send_error(404)
                    return
                script = records[label]["snippet"]
                content = (
                    '<!doctype html><meta charset="utf-8"><title>Analytics verification '
                    + label
                    + "</title>"
                    + script
                    + "<h1>本机数分验收 "
                    + label
                    + "</h1>"
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html;charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            def do_POST(self):
                label = self.path.strip("/")
                if label not in records or self.headers.get("Origin") != origin:
                    self.send_error(403)
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size < 16000:
                    self.send_error(413)
                    return
                data = json.loads(self.rfile.read(size))
                record = records[label]
                if data.get("connection_id") != record["cid"]:
                    self.send_error(422)
                    return
                # The test order retains its original source and event ID.
                payload = payments.setdefault(
                    label,
                    {
                        "connection_id": record["cid"],
                        "event_id": "purchase:shared-test-order",
                        "event_name": "Purchase",
                        "lead_attribution": data,
                        "properties": {
                            "order_id": "shared-test-order",
                            "value": 12.5 if label == "A" else 88,
                            "currency": "USD",
                        },
                    },
                )
                with httpx.Client(timeout=30) as backend:
                    response = backend.post(
                        base + "/collect/server/events",
                        headers={"Authorization": "Bearer " + record["key"]},
                        json=payload,
                    )
                self.send_response(response.status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response.content)))
                self.end_headers()
                self.wfile.write(response.content)

        server = ThreadingHTTPServer(("127.0.0.1", 18787), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(channel="chrome", headless=True)
                context = await browser.new_context(
                    user_agent=f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{browser.version} Safari/537.36"
                )
                page = await context.new_page()
                page.on("pageerror", lambda err: errors.append(str(err)))
                first = True

                async def delayed_receipt(route):
                    nonlocal first
                    if route.request.method != "POST":
                        await route.continue_()
                        return
                    if not first:
                        await route.continue_()
                        return
                    first = False
                    response = await route.fetch()
                    response_json = await response.json()
                    body = route.request.post_data_json
                    assert response.status in (200, 202), response_json
                    await route.fulfill(
                        status=202,
                        headers={
                            **{
                                k: v
                                for k, v in response.headers.items()
                                if k.lower()
                                not in ("content-length", "content-encoding")
                            },
                            "content-type": "application/json",
                        },
                        json={
                            "event_id": body["event_id"],
                            "status": "pending",
                            "agent_id": records["A"]["aid"],
                            "capabilities": {"batch": True, "receipts": True},
                        },
                    )

                await page.route(
                    records["A"]["collect_origin"] + "/collect/" + records["A"]["cid"],
                    delayed_receipt,
                )
                batch_attempts = []

                async def transient_limit(route):
                    if route.request.method != "POST":
                        await route.continue_()
                        return
                    batch_attempts.append(route.request.post_data_json)
                    if len(batch_attempts) == 1:
                        await route.fulfill(
                            status=429,
                            headers={
                                "Access-Control-Allow-Origin": origin,
                                "Access-Control-Expose-Headers": "Retry-After",
                                "Retry-After": "1",
                            },
                            json={"detail": {"code": "ANALYTICS_RATE_LIMITED"}},
                        )
                    else:
                        await route.continue_()

                await page.route(
                    records["A"]["collect_origin"]
                    + "/collect/"
                    + records["A"]["cid"]
                    + "/batch",
                    transient_limit,
                )
                sources = {}
                for label, record in records.items():
                    await page.goto(base + "/r/" + record["marker"], wait_until="load")
                    source = await page.evaluate(
                        "async()=>{await leadq('attribution');await leadq('identify','shared-browser-user');await leadq('track','CompleteRegistration',{}, {event_id:'registration:shared-browser-user'});return await leadq('attribution')}"
                    )
                    assert (
                        source["connection_id"] == record["cid"]
                        and source["click_id"]
                        and source["click_token"]
                        and source["touchpoint_id"]
                    ), {
                        "label": label,
                        "fields": {k: bool(v) for k, v in source.items()},
                    }
                    sources[label] = source
                    for duplicate in (False, True):
                        result = await page.evaluate(
                            "async ({source,label})=>{const response=await fetch('/'+label,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(source)});return {status:response.status,body:await response.json()}}",
                            {"source": source, "label": label},
                        )
                        assert result["status"] == 200, result
                        assert result["body"]["duplicate"] == duplicate, {
                            "label": label,
                            "duplicate_expected": duplicate,
                            "result": result,
                        }
                assert (
                    len(batch_attempts) >= 2
                    and batch_attempts[0]["events"][0]["event_id"]
                    == batch_attempts[1]["events"][0]["event_id"]
                ), {
                    "batch_attempts": [
                        [e["event_name"], e["event_id"]]
                        for b in batch_attempts
                        for e in b["events"]
                    ]
                }
                assert (
                    sources["A"]["click_id"] != sources["B"]["click_id"]
                    and sources["A"]["anonymous_id"] != sources["B"]["anonymous_id"]
                )
                checks += [
                    "actual short redirect and signed click context",
                    "202 initialization waits for real receipt",
                    "429 retry preserves event ID",
                    "A/B website identities stay separate",
                    "server payment retries count once per Agent",
                ]
                await browser.close()
            for label, record in records.items():
                with tx(record["aid"]) as c:
                    counted = one(
                        c,
                        "SELECT count(*) n,max(value) amount FROM lead.pixel_receipts WHERE scope_key='live' AND event_name='Purchase' AND counted",
                    )
                    assert counted["n"] == 1 and float(counted["amount"]) == (
                        12.5 if label == "A" else 88
                    )
                    c.execute("SELECT set_config('app.analytics_scope','live',true)")
                    contexts = all_rows(
                        c,
                        "SELECT context FROM lead.analytics_clean_events WHERE event_name='PageView'",
                    )
                    assert contexts and all(
                        "email=" not in json.dumps(r) and "#secret" not in json.dumps(r)
                        for r in contexts
                    )
            checks.append("stored page context strips query and fragment")
            assert not errors, errors
            result = {"ok": True, "checks": checks, "page_errors": errors}
            folder = STATE / "analytics-verification"
            folder.mkdir(exist_ok=True)
            (folder / "browser.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2) + "\n"
            )
            print(json.dumps(result, ensure_ascii=False))
        finally:
            server.shutdown()
            server.server_close()
            for record in records.values():
                with tx(record["aid"]) as c:
                    c.execute(
                        "UPDATE lead.agents SET desired_state='archived' WHERE id=%s",
                        (record["aid"],),
                    )


if __name__ == "__main__":
    asyncio.run(run())
