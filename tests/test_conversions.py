import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from lead_app import domain
from lead_app.api import app
from lead_app.config import TENANT
from lead_app.db import one, tx, uid
from lead_app.security import token


@pytest.fixture
def site(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    cid = uid()
    code = uid().replace("-", "")[:16]
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.analytics_connections(id,tenant_id,agent_id,provider,site_host,secret_ref,state) VALUES(%s,%s,%s,'lead_site','https://site.example','client-write','pending')",
            (cid, TENANT, aid),
        )
        c.execute(
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,'x',%s,'https://site.example/','site.example')",
            (TENANT, aid, code),
        )
    client = TestClient(app)
    client.cookies.set("lead_session", token("owner"))
    owner = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    key = client.post(
        f"/api/agents/{aid}/analytics/server-key",
        headers=owner,
        json={"connection_id": cid},
    ).json()["key"]
    yield aid, cid, code, client, owner, key
    domain.set_control(aid, "archive")


def pageview(site):
    aid, cid, code, client, _, _ = site
    sid = uid()
    response = client.post(
        "/collect/" + cid,
        headers={"Origin": "https://site.example"},
        json={
            "key": token("analytics:" + cid),
            "session_id": sid,
            "event": "pageview",
            "url": "https://site.example/",
            "ref": code,
        },
    )
    assert response.status_code == 204
    return {
        "connection_id": cid,
        "event_id": "signup:" + uid(),
        "type": "signup",
        "session_id": sid,
        "ref": code,
    }


def test_server_conversion_requires_distinct_key_real_attribution_and_dedupes(site):
    aid, cid, code, client, _, key = site
    assert domain.agent_detail(aid)["metrics"]["conversions"] is None
    assert (
        client.post(
            "/collect/server/ping",
            headers={"Authorization": "Bearer " + key},
            json={"connection_id": cid},
        ).status_code
        == 200
    )
    assert domain.agent_detail(aid)["metrics"]["conversions"] == 0
    body = pageview(site)
    assert (
        client.post(
            "/collect/server/conversion",
            headers={"Authorization": "Bearer " + token("analytics:" + cid)},
            json=body,
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/collect/server/conversion",
            headers={
                "Authorization": "Bearer " + key,
                "Origin": "https://site.example",
            },
            json=body,
        ).status_code
        == 403
    )
    auth = {"Authorization": "Bearer " + key}
    assert (
        client.post(
            "/collect/server/conversion",
            headers=auth,
            json={**body, "session_id": uid()},
        ).status_code
        == 409
    )
    first = client.post("/collect/server/conversion", headers=auth, json=body)
    assert first.status_code == 200 and not first.json()["duplicate"]
    repeated = client.post("/collect/server/conversion", headers=auth, json=body)
    assert repeated.json()["duplicate"] and repeated.json()["id"] == first.json()["id"]
    assert (
        client.post(
            "/collect/server/conversion",
            headers=auth,
            json={**body, "type": "purchase"},
        ).status_code
        == 409
    )
    assert domain.agent_detail(aid)["metrics"]["conversions"] == 1
    with tx(aid) as c:
        stored = one(c, "SELECT secret_sha256 FROM lead.analytics_credentials")
        assert stored["secret_sha256"] == hashlib.sha256(key.encode()).hexdigest()
        assert (
            one(c, "SELECT trust_level FROM lead.conversion_events")["trust_level"]
            == "server_verified"
        )


def test_rotation_scope_freshness_and_large_requests(site):
    aid, cid, _, client, owner, key = site
    other = domain.create_agent()
    try:
        assert (
            client.post(
                f"/api/agents/{other}/analytics/server-key",
                headers=owner,
                json={"connection_id": cid},
            ).status_code
            == 404
        )
        new = client.post(
            f"/api/agents/{aid}/analytics/server-key",
            headers=owner,
            json={"connection_id": cid},
        ).json()["key"]
        assert (
            client.post(
                "/collect/server/ping",
                headers={"Authorization": "Bearer " + key},
                json={"connection_id": cid},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/collect/server/ping",
                headers={"Authorization": "Bearer " + new},
                json={"connection_id": cid},
            ).status_code
            == 200
        )
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.analytics_credentials SET last_received_at=now()-interval '2 days' WHERE state='active'"
            )
        metric = domain.agent_detail(aid)["metrics"]
        assert (
            metric["conversions"] is None and metric["conversion_coverage"] == "stale"
        )
        auth = {"Authorization": "Bearer " + new}
        body = pageview(site)
        assert (
            client.post(
                "/collect/server/conversion",
                headers=auth,
                json={
                    **body,
                    "occurred_at": (
                        datetime.now(timezone.utc) + timedelta(days=1)
                    ).isoformat(),
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/collect/server/conversion",
                headers=auth,
                json={**body, "connection_id": uid()},
            ).status_code
            == 401
        )
        large = client.post(
            "/collect/server/ping",
            headers={"Transfer-Encoding": "chunked"},
            content=iter([b"x" * 600000, b"x" * 600000]),
        )
        assert large.status_code == 413
    finally:
        domain.set_control(other, "archive")
