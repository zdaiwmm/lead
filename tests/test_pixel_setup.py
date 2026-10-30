"""Reusable installation codes and local-only website registration."""

import re
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from lead_app import analytics, domain, security
from lead_app.api import app
from lead_app.db import one, tx, uid

OWNER = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}


@pytest.fixture
def setup_client(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(analytics, "env", lambda: {})
    aid = domain.create_agent()
    client = TestClient(app)
    client.cookies.set("lead_session", security.token("owner"))
    return aid, client


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://127.0.0.1:8787/path/", "http://127.0.0.1:8787"),
        (" http://localhost:8787/ ", "http://localhost:8787"),
        ("http://[::1]:8787/", "http://[::1]:8787"),
        ("http://LOCALHOST:80/", "http://localhost"),
    ],
)
def test_local_setup_and_receipt_without_public_fetch(
    setup_client, monkeypatch, url, origin
):
    aid, client = setup_client

    def no_fetch(value):
        pytest.fail("Local registration must not resolve or fetch a website")

    monkeypatch.setattr(analytics, "resolve_public", no_fetch)
    response = client.post(
        f"/api/agents/{aid}/analytics", headers=OWNER, json={"site_url": url}
    )
    assert response.status_code == 200, response.text
    conn = response.json()["connection"]
    assert conn["site_host"] == origin
    key = re.search(r'data-key="([^"]+)"', conn["snippet"])[1]
    body = {
        "key": key,
        "event_name": "PageView",
        "event_id": uid(),
        "anonymous_id": uid(),
        "session_id": uid(),
        "url": origin + "/",
    }
    assert (
        client.post(
            "/collect/" + conn["id"], headers={"Origin": origin}, json=body
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/collect/" + conn["id"],
            headers={"Origin": "http://localhost:9999"},
            json=body,
        ).status_code
        == 403
    )
    body["url"] = "https://other.example/"
    assert (
        client.post(
            "/collect/" + conn["id"], headers={"Origin": origin}, json=body
        ).status_code
        == 403
    )


def test_saved_code_survives_time_reads_repeated_setup_and_site_switch(
    setup_client, monkeypatch
):
    aid, client = setup_client
    path = f"/api/agents/{aid}/analytics"

    def save(url):
        response = client.post(path, headers=OWNER, json={"site_url": url})
        assert response.status_code == 200, response.text
        return response.json()["connection"]

    first = save("http://127.0.0.1:8787/")
    # Reads a year later still return the exact installation code.
    with monkeypatch.context() as patch:
        now = security.time.time()
        patch.setattr(security.time, "time", lambda: now + 2 * 365 * 86400)
        assert (
            analytics.settings(UUID(aid))["connection"]["snippet"] == first["snippet"]
        )
    assert client.get(path).json()["connection"]["snippet"] == first["snippet"]
    assert save("http://127.0.0.1:8787/checkout") == first
    other = save("http://localhost:8787")
    assert other["id"] != first["id"] and other["snippet"] != first["snippet"]
    assert save("http://127.0.0.1:8787")["snippet"] == first["snippet"]
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.analytics_connections")["n"] == 2
        c.execute(
            "UPDATE lead.analytics_connections SET state='disconnected' WHERE id=%s",
            (first["id"],),
        )
    assert (
        client.post(
            "/collect/" + first["id"],
            headers={"Origin": first["site_host"]},
            json={
                "key": security.analytics_key(first["id"]),
                "event": "pageview",
                "session_id": uid(),
                "url": first["site_host"] + "/",
            },
        ).status_code
        == 403
    )


def test_public_install_key_is_scoped_and_legacy_tokens_remain_compatible(monkeypatch):
    cid, other = uid(), uid()
    key = security.analytics_key(cid)
    assert security.valid_analytics_key(key, cid)
    assert not security.valid_analytics_key(key, other)
    assert not security.valid(key, "owner")
    assert not security.valid(key, "analytics:" + cid)
    assert security.valid_analytics_key(security.token("analytics:" + cid), cid)
    assert not security.valid_analytics_key(security.token("owner"), cid)
    assert not security.valid_analytics_key(security.token("analytics:" + cid, -1), cid)
    for invalid in (None, "", "错误", key[:-1]):
        assert not security.valid_analytics_key(invalid, cid)
    monkeypatch.setitem(
        security.CONFIG, "session_secret", "isolated-replacement-secret"
    )
    assert not security.valid_analytics_key(key, cid)


@pytest.mark.parametrize(
    "url", ["http://127.0.0.1:8787", "http://localhost", "http://[::1]"]
)
def test_local_origins_remain_blocked_in_production(monkeypatch, url):
    monkeypatch.setattr(
        analytics, "env", lambda: {"LEAD_DEPLOYMENT_MODE": "production"}
    )
    with pytest.raises(ValueError, match="公开网站"):
        analytics.site_origin(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:secret@127.0.0.1:8787/",
        "http://@localhost:8787/",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/",
        "http://localhost.evil.example:8787/",
    ],
)
def test_registration_does_not_relax_other_address_restrictions(monkeypatch, url):
    monkeypatch.setattr(analytics, "env", lambda: {})
    with pytest.raises(ValueError):
        analytics.site_origin(url)
