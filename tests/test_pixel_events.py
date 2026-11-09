"""Real DB receipts: source snapshots, business dedup and isolated event tests."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from lead_app import analytics, domain
from lead_app.api import app
from lead_app.db import one, tx, uid
from lead_app.promotion import new_marker
from lead_app.scope import current_tenant
from lead_app.security import token


@pytest.fixture
def pixel(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(analytics, "resolve_public", lambda url: None)
    aid = domain.create_agent()
    client = TestClient(app)
    client.cookies.set("lead_session", token("owner"))
    headers = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    settings = client.post(
        f"/api/agents/{aid}/analytics",
        headers=headers,
        json={"site_url": "https://site.example"},
    ).json()
    cid = str(settings["connection"]["id"])
    markers = [new_marker("telegram"), new_marker("x")]
    with tx(aid) as c:
        for marker in markers:
            c.execute(
                """INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host)
                VALUES(%s,%s,'x',%s,'https://site.example/','site.example')""",
                (current_tenant(), aid, marker),
            )
    server = client.post(
        f"/api/agents/{aid}/analytics/server-key",
        headers=headers,
        json={"connection_id": cid},
    ).json()["key"]
    return aid, cid, client, headers, markers, server


def send(pixel, name="PageView", **extra):
    _, cid, client, _, _, _ = pixel
    body = dict(
        key=token("analytics:" + cid),
        event_name=name,
        event_id=uid(),
        anonymous_id=uid(),
        session_id=uid(),
        url="https://site.example/",
    )
    body.update(extra)
    return client.post(
        "/collect/" + cid, headers={"Origin": "https://site.example"}, json=body
    )


def settings(pixel):
    aid, _, client, _, _, _ = pixel
    return client.get(f"/api/agents/{aid}/analytics").json()


def test_install_requires_pageview_and_completion_requires_real_test(pixel):
    aid, cid, client, headers, _, _ = pixel
    assert not settings(pixel)["pixel"]["installed"]
    client.post(
        f"/api/agents/{aid}/analytics/published",
        headers=headers,
        json={"connection_id": cid},
    )
    assert (
        settings(pixel)["pixel"]["embedded"]
        and not settings(pixel)["pixel"]["installed"]
    )
    assert send(pixel).status_code == 200
    assert settings(pixel)["pixel"]["installed"]
    path = f"/api/agents/{aid}/analytics/complete"
    assert (
        client.post(path, headers=headers, json={"connection_id": cid}).status_code
        == 409
    )
    assert (
        client.post(
            path, headers=headers, json={"connection_id": cid, "visits_only": True}
        ).status_code
        == 200
    )


def test_business_dedup_identity_and_unattributed_events_are_retained(pixel):
    aid, _, _, _, markers, _ = pixel
    did, sid = uid(), uid()
    assert send(pixel, anonymous_id=did, session_id=sid).status_code == 200
    body = dict(anonymous_id=did, session_id=sid, user_id="business-user")
    first = send(pixel, "CompleteRegistration", **body)
    assert first.status_code == 200 and not first.json()["duplicate"]
    assert send(pixel, "CompleteRegistration", **body).json()["duplicate"]
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT touchpoint_id FROM lead.pixel_receipts WHERE event_name='CompleteRegistration' AND counted",
            )["touchpoint_id"]
            is None
        )
    assert settings(pixel)["pixel"]["report"]["event_count"] == 1
    assert settings(pixel)["pixel"]["report"]["events"][0]["users"] == 1
    assert (
        send(
            pixel,
            "Purchase",
            properties={"order_id": "o", "value": 1, "currency": "CNY"},
        ).status_code
        == 422
    )


def test_source_switch_keeps_order_snapshot_and_payment_dedup(pixel):
    aid, cid, client, _, markers, key = pixel
    did, sid = uid(), uid()
    first = send(pixel, anonymous_id=did, session_id=sid, ref=markers[0]).json()[
        "attribution"
    ]
    second = send(pixel, anonymous_id=did, session_id=sid, ref=markers[1]).json()[
        "attribution"
    ]
    assert first["touchpoint_id"] != second["touchpoint_id"]
    body = {
        "connection_id": cid,
        "event_name": "Purchase",
        "event_id": uid(),
        "anonymous_id": did,
        "user_id": "buyer",
        "touchpoint_id": first["touchpoint_id"],
        "properties": {"order_id": "order-1", "value": 99, "currency": "CNY"},
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    auth = {"Authorization": "Bearer " + key}
    result = client.post("/collect/server/events", headers=auth, json=body)
    assert result.status_code == 200, result.text
    assert client.post(
        "/collect/server/events", headers=auth, json={**body, "event_id": uid()}
    ).json()["duplicate"]
    assert (
        client.post(
            "/collect/server/events",
            headers=auth,
            json={**body, "properties": {**body["properties"], "value": 100}},
        ).status_code
        == 409
    )
    changed_order = {
        **body,
        "event_id": uid(),
        "properties": {**body["properties"], "value": 101},
    }
    assert (
        client.post(
            "/collect/server/events", headers=auth, json=changed_order
        ).status_code
        == 409
    )
    with tx(aid) as c:
        assert (
            str(
                one(
                    c,
                    "SELECT touchpoint_id FROM lead.pixel_receipts WHERE event_name='Purchase' AND counted",
                )["touchpoint_id"]
            )
            == first["touchpoint_id"]
        )
        assert one(c, "SELECT count(*) AS n FROM lead.pixel_touchpoints")["n"] == 2


def test_real_test_is_revision_scoped_and_never_counts_as_live(pixel):
    aid, cid, client, headers, _, _ = pixel
    send(pixel)
    definition = settings(pixel)["pixel"]["events"][0]
    path = f"/api/agents/{aid}/analytics/events"
    test = client.post(
        path + "/test",
        headers=headers,
        json={
            "definition_id": definition["id"],
            "expected_revision": definition["revision"],
        },
    ).json()
    assert test.get("test_code"), test
    assert client.get(path + "/test/" + test["id"]).json()["received_at"] is None
    rejected = send(pixel, "CompleteRegistration", test_code=test["test_code"])
    assert rejected.status_code == 422
    assert (
        client.get(path + "/test/" + test["id"]).json()["error"] == "此事件需要用户编号"
    )
    event_id = uid()
    assert (
        send(
            pixel,
            "CompleteRegistration",
            user_id="test-user",
            event_id=event_id,
            test_code=test["test_code"],
        ).status_code
        == 200
    )
    assert client.get(path + "/test/" + test["id"]).json()["received_at"]
    assert settings(pixel)["pixel"]["report"]["event_count"] is None
    assert (
        send(
            pixel, "CompleteRegistration", user_id="test-user", event_id=event_id
        ).status_code
        == 200
    )
    assert settings(pixel)["pixel"]["report"]["event_count"] == 1
    changed = client.post(
        path + "/configure",
        headers=headers,
        json={
            "connection_id": cid,
            "name": "CompleteRegistration",
            "label": "注册完成",
            "method": "browser",
            "expected_revision": definition["revision"],
        },
    )
    assert changed.status_code == 200
    assert (
        send(
            pixel,
            "CompleteRegistration",
            user_id="test-user",
            test_code=test["test_code"],
        ).status_code
        == 422
    )


def test_full_session_payment_snapshot_end_and_live_isolation(pixel):
    aid, cid, client, headers, _, key = pixel
    assert send(pixel).status_code == 200
    root = f"/api/agents/{aid}/analytics/events"
    for name in ("ViewContent", "AddToCart", "InitiateCheckout"):
        response = client.post(
            root + "/configure",
            headers=headers,
            json={
                "connection_id": cid,
                "name": name,
                "label": name,
                "expected_revision": 0,
            },
        )
        assert response.status_code == 200, response.text
    test = client.post(
        root + "/test", headers=headers, json={"connection_id": cid}
    ).json()
    did, sid = uid(), uid()
    common = {"anonymous_id": did, "session_id": sid, "test_code": test["test_code"]}
    snapshot = send(pixel, **common).json()["attribution"]
    assert snapshot["test_code"] == test["test_code"]
    for name in (
        "ViewContent",
        "CompleteRegistration",
        "AddToCart",
        "InitiateCheckout",
    ):
        response = send(pixel, name, user_id="test-buyer", **common)
        assert response.status_code == 200, response.text
    payment = {
        "connection_id": cid,
        "event_name": "Purchase",
        "event_id": "purchase:test-order",
        "lead_attribution": snapshot,
        "user_id": "test-buyer",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "properties": {"order_id": "test-order", "value": 198, "currency": "CNY"},
    }
    auth = {"Authorization": "Bearer " + key}
    response = client.post("/collect/server/events", headers=auth, json=payment)
    assert response.status_code == 200, response.text
    assert client.post("/collect/server/events", headers=auth, json=payment).json()[
        "duplicate"
    ]
    result = client.get(root + "/test/" + test["id"]).json()
    assert result["active"]
    assert {e["event_name"] for e in result["events"]} == {
        "PageView",
        "ViewContent",
        "CompleteRegistration",
        "AddToCart",
        "InitiateCheckout",
        "Purchase",
    }
    assert any(
        e["event_name"] == "Purchase" and e["trust_level"] == "server_verified"
        for e in result["events"]
    )
    assert settings(pixel)["pixel"]["report"]["event_count"] is None
    assert all(
        e["tested"]
        for e in settings(pixel)["pixel"]["events"]
        if e["name"] != "Activation"
    )
    assert (
        client.post(
            root + "/test/" + test["id"] + "/end", headers=headers, json={}
        ).status_code
        == 200
    )
    assert send(pixel, "AddToCart", **common).status_code == 422
    assert (
        client.post("/collect/server/events", headers=auth, json=payment).status_code
        == 422
    )
    assert settings(pixel)["pixel"]["report"]["event_count"] is None
    # The same business order can subsequently exist in live scope, without the saved test snapshot.
    live = {**payment, "lead_attribution": None}
    assert (
        client.post("/collect/server/events", headers=auth, json=live).status_code
        == 200
    )
    assert settings(pixel)["pixel"]["report"]["event_count"] == 1


def test_public_session_control_checks_origin_and_expiration(pixel):
    aid, cid, client, headers, _, _ = pixel
    send(pixel)
    root = f"/api/agents/{aid}/analytics/events"
    test = client.post(
        root + "/test", headers=headers, json={"connection_id": cid}
    ).json()
    control = {
        "key": token("analytics:" + cid),
        "url": "https://site.example/",
        "test_code": test["test_code"],
    }
    path = "/collect/" + cid + "/test"
    assert (
        client.post(
            path, headers={"Origin": "https://foreign.example"}, json=control
        ).status_code
        == 403
    )
    assert (
        client.options(path, headers={"Origin": "https://site.example"}).status_code
        == 204
    )
    result = client.post(path, headers={"Origin": "https://site.example"}, json=control)
    assert (
        result.json()["active"]
        and result.headers["access-control-allow-origin"] == "https://site.example"
    )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.pixel_tests SET expires_at=now()-interval '1 second' WHERE id=%s",
            (test["id"],),
        )
    assert (
        client.post(
            path, headers={"Origin": "https://site.example"}, json=control
        ).json()["state"]
        == "expired"
    )
    assert (
        send(
            pixel,
            "CompleteRegistration",
            user_id="expired-test",
            test_code=test["test_code"],
        ).status_code
        == 422
    )
    assert settings(pixel)["pixel"]["report"]["event_count"] is None
    result = client.post(
        path,
        headers={"Origin": "https://site.example"},
        json={**control, "action": "end"},
    )
    assert result.json()["state"] == "ended"


def test_server_snapshot_conflicts_are_rejected(pixel):
    _, cid, client, _, _, key = pixel
    body = {
        "connection_id": cid,
        "event_name": "Purchase",
        "event_id": "p:test",
        "properties": {"order_id": "order", "value": 99, "currency": "CNY"},
        "lead_attribution": {"connection_id": cid, "test_code": "lt_saved"},
        "test_code": "lt_different",
    }
    assert (
        client.post(
            "/collect/server/events",
            headers={"Authorization": "Bearer " + key},
            json=body,
        ).status_code
        == 422
    )
    body.pop("test_code")
    body["lead_attribution"]["connection_id"] = uid()
    assert (
        client.post(
            "/collect/server/events",
            headers={"Authorization": "Bearer " + key},
            json=body,
        ).status_code
        == 422
    )


def test_custom_events_and_cross_agent_access(pixel):
    aid, cid, client, headers, _, _ = pixel
    path = f"/api/agents/{aid}/analytics/events/configure"
    config = {
        "connection_id": cid,
        "name": "ReportExported",
        "label": "导出报告",
        "definition": "导出成功",
        "method": "browser",
    }
    assert client.post(path, headers=headers, json=config).status_code == 200
    assert (
        send(pixel, "ReportExported", properties={"action": "export"}).status_code
        == 200
    )
    assert (
        client.post(
            path, headers=headers, json={**config, "name": "reportexported"}
        ).status_code
        == 409
    )
    other = domain.create_agent()
    assert (
        client.post(
            f"/api/agents/{other}/analytics/events/configure",
            headers=headers,
            json=config,
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/agents/{other}/analytics/report").json()["pixel"]["report"][
            "event_count"
        ]
        is None
    )


def test_public_collection_fences_origins_keys_and_foreign_sources(pixel):
    aid, cid, client, headers, markers, key = pixel
    other = domain.create_agent()
    foreign = new_marker("x")
    with tx(other) as c:
        c.execute(
            """INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host)
            VALUES(%s,%s,'x',%s,'https://site.example/','site.example')""",
            (current_tenant(), other, foreign),
        )
    did, sid = uid(), uid()
    assert send(pixel, anonymous_id=did, session_id=sid, ref=markers[0]).json()[
        "attribution"
    ]["touchpoint_id"]
    assert (
        send(pixel, anonymous_id=did, session_id=sid, ref=foreign).json()[
            "attribution"
        ]["touchpoint_id"]
        is None
    )
    body = dict(
        event_id=uid(),
        event_name="PageView",
        key=token("analytics:" + cid),
        anonymous_id=uid(),
        session_id=uid(),
        url="https://site.example/",
    )
    assert (
        client.post(
            "/collect/" + cid, headers={"Origin": "https://evil.example"}, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/collect/" + cid,
            headers={"Origin": "https://site.example"},
            json={**body, "url": "https://evil.example/"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/collect/server/events",
            headers={"Authorization": "Bearer " + body["key"]},
            json={"connection_id": cid, "event_id": uid(), "event_name": "Purchase"},
        ).status_code
        == 401
    )
    assert TestClient(app).get(f"/api/agents/{aid}/analytics/report").status_code == 401
    report = client.get(f"/api/agents/{aid}/analytics/report").json()
    assert "snippet" not in report["connection"] and "server" not in report


def test_agent_report_recovery_and_freshness(pixel):
    from scripts.recover_agent_analytics import recover_scope

    aid, _, client, _, _, _ = pixel
    path = f"/api/agents/{aid}/analytics/overview?traffic=site"
    tenant = current_tenant()
    assert send(pixel).is_success
    today = client.get(path).json()["series"][-1]
    assert today["report_state"] == "pending" and today["pv"] is None
    assert recover_scope(tenant, aid, "live") > 0
    today = client.get(path).json()["series"][-1]
    assert today["report_state"] == "current" and today["updated_at"]
    assert today["pv"] == today["site_devices"] == 1 and today["clicks"] == 0
    assert send(pixel).is_success
    stale = client.get(path).json()["series"][-1]
    assert stale["report_state"] == "pending" and stale["pv"] == 1
    recover_scope(tenant, aid, "live")
    assert recover_scope(tenant, aid, "live") == 0
    assert client.get(path).json()["series"][-1]["pv"] == 2


@pytest.mark.skipif(__import__("os").getenv("LEAD_WORKFLOW_TESTS") != "1", reason="isolated Temporal server required")
def test_target_temporal_worker_processes_queued_event(pixel):
    import os
    import subprocess
    import sys
    import time
    from pathlib import Path

    from lead_app.analytics_ingest import admit_browser
    from lead_app.pixel_events import BrowserEvent

    aid, cid, client, _, _, _ = pixel
    with tx(aid) as c:
        conn = one(c, "SELECT * FROM lead.analytics_connections WHERE id=%s", (cid,))
    body = BrowserEvent(key=token("analytics:"+cid), event_name="PageView", event_id=uid(),
                        anonymous_id=uid(), session_id=uid(), url="https://site.example/")
    event = admit_browser(conn, body, {"client_ip":None,"user_agent":"Fixture Browser"})
    root = Path(__file__).resolve().parents[1]
    import_paths = [str(root / "services/api")]
    if existing := os.environ.get("PYTHONPATH"):
        import_paths.append(existing)
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(import_paths), LEAD_ANALYTICS_TASK_QUEUE="lead-analytics-"+aid)
    worker = subprocess.Popen([sys.executable, str(root / "scripts/agent_analytics_temporal.py"),
                               "--agent", aid], env=env)
    try:
        for _ in range(45):
            assert worker.poll() is None
            today = client.get(f"/api/agents/{aid}/analytics/overview?traffic=site").json()["series"][-1]
            if today.get("pv") == 1 and today["report_state"] == "current":
                break
            time.sleep(1)
        else:
            pytest.fail("dedicated queue did not produce the expected snapshot")
        from lead_app.analytics_ingest import scoped_tx

        with scoped_tx(current_tenant(), aid, "live") as c:
            assert one(c, "SELECT state FROM lead.analytics_ingest_events WHERE id=%s", (event["id"],))["state"] == "processed"
    finally:
        worker.terminate()
        worker.wait(timeout=30)


def test_two_clicks_one_device_and_one_business_event_in_isolated_agent(pixel):
    from scripts.recover_agent_analytics import recover_scope
    aid, cid, client, headers, markers, _ = pixel
    for marker in markers:
        response=client.get("/r/"+marker, follow_redirects=False, headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X) Chrome/132.0.0.0 Safari/537.36","Accept":"text/html"})
        assert response.headers["X-Lead-Measurement"] == "accepted"
    did, sid = uid(), uid()
    assert send(pixel,anonymous_id=did,session_id=sid).is_success
    event = {"event_id":uid(),"user_id":"fixture-member","anonymous_id":did,"session_id":sid}
    assert send(pixel,"CompleteRegistration",**event).is_success
    assert send(pixel,"CompleteRegistration",**event).is_success
    for _ in range(6):
        if not recover_scope(current_tenant(), aid, "live"):
            break
    day=client.get(f"/api/agents/{aid}/analytics/overview?traffic=site").json()["series"][-1]
    assert (day["clicks"],day["devices"],day["pv"],day["site_devices"])==(2,1,1,1)
    with tx(aid) as c:
        counted=one(c,"SELECT count(*) AS n FROM lead.pixel_receipts WHERE connection_id=%s AND event_name='CompleteRegistration' AND counted AND scope_key='live'",(cid,))
        assert counted["n"]==1


def test_acquisition_scope_separates_page_entries_from_window_conversions(pixel):
    from urllib.parse import parse_qs, urlsplit

    from lead_app.analytics_ingest import scoped_tx
    from lead_app.analytics_reporting import refresh_scope

    from scripts.recover_agent_analytics import recover_scope

    aid, cid, client, _, markers, server = pixel
    anonymous = uid()
    tenant = current_tenant()
    redirect = client.get("/r/" + markers[0], follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"})
    query = parse_qs(urlsplit(redirect.headers["location"]).query)
    arrivals = [({}, uid()), ({"ref": markers[0], "click_token": query["lc"][0]}, uid()), ({}, uid())]
    for entry, session in arrivals:
        page = send(pixel, anonymous_id=anonymous, session_id=session, **entry)
        assert page.status_code == 200, page.text
        if entry:
            internal = send(pixel, anonymous_id=anonymous, session_id=session)
            assert internal.status_code == 200
    # A direct return keeps an eligible order attribution snapshot. Its PV
    # belongs to the current (direct) visit even with a historical touch.
    context = page.json()["attribution"]
    assert context["touchpoint_id"]
    payment = {"connection_id": cid, "event_name": "Purchase", "event_id": uid(),
               "lead_attribution": context, "properties": {"order_id": "delayed", "value": 8, "currency": "CNY"}}
    for i in range(2):
        result = client.post("/collect/server/events", headers={"Authorization": "Bearer " + server}, json=payment)
        assert result.status_code == 200, result.text
    organic = {**payment, "lead_attribution": None, "anonymous_id": uid(), "event_id": uid(),
               "properties": {"order_id": "organic", "value": 12, "currency": "CNY"}}
    result = client.post("/collect/server/events", headers={"Authorization": "Bearer " + server}, json=organic)
    assert result.status_code == 200, result.text
    details = client.get(f"/api/agents/{aid}/analytics/report").json()
    assert details["report_traffic"] == "promotion"
    assert details["pixel"]["report"]["event_count"] == 1
    assert details["pixel"]["report"]["sessions"] == 1
    assert recover_scope(tenant, aid, "live")
    base = f"/api/agents/{aid}/analytics/"
    for traffic, pages, revenue in [("promotion", 2, 8), ("site", 4, 20)]:
        report = client.get(base+"summary", params={"traffic": traffic, "event_name": "Purchase"}).json()
        assert report["summary"]["pv"] == pages and report["summary"]["visitors"] == 1
        assert report["summary"]["conversions"] == (1 if traffic == "promotion" else 2)
        assert sum(r["pv"] for r in report["countries"]) == pages
        assert float(report["amounts"][0]["amount"]) == revenue
        overview = client.get(base+"overview", params={"traffic": traffic}).json()["series"][-1]
        assert (overview["clicks"], overview["pv"], overview["site_devices"]) == (1, pages, 1)
    assert client.get(base+"overview").json()["series"][-1]["pv"] == 2
    assert client.get(base+"overview?channel=unattributed").json()["series"][-1]["pv"] == 2
    with scoped_tx(tenant, aid, "live") as c:
        c.execute("UPDATE lead.analytics_report_days SET version='analytics-report-v1'")
    assert client.get(base+"overview").json()["series"][-1]["pv"] is None
    assert client.get(base+"summary?traffic=promotion").json()["summary"]["pv"] is None
    assert len(refresh_scope(tenant, aid)) == 1
    assert not refresh_scope(tenant, aid)
    assert client.get(base+"overview").json()["series"][-1]["pv"] == 2
