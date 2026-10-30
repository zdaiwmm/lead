"""Real persistence and attribution for compact promotion links and channel rows."""

from urllib.parse import parse_qsl, urlsplit
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import analytics, domain
from lead_app.api import app
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx, uid
from lead_app.promotion import (
    PLATFORM_CODES,
    attributed_url,
    new_marker,
    promotion_body,
)
from lead_app.scope import current_tenant
from lead_app.security import token
from psycopg import sql

from scripts.migrate import upgrade


def make_agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(analytics, "emit", lambda *a, **k: None)
    monkeypatch.setattr(analytics, "resolve_public", lambda url: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url) VALUES(%s,%s,1,'Fixture product','https://site.example/')",
            (current_tenant(), aid),
        )
        profile = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        for platform, state in [
            ("telegram", "connected"),
            ("x", "connected"),
            ("x", "connected"),
            ("facebook", "pending"),
            ("lab", "connected"),
        ]:
            c.execute(
                "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id) VALUES(%s,%s,%s,%s,%s,%s)",
                (current_tenant(), aid, profile, platform, state, uid()),
            )
    return aid


@pytest.fixture
def agent(monkeypatch):
    return make_agent(monkeypatch)


@pytest.fixture
def publishing_agent(monkeypatch):
    """Verified capabilities belong only to this disposable database."""
    admin = database_kwargs(True)
    name = "lead_promotion_test_" + uid().replace("-", "")
    with psycopg.connect(**admin, autocommit=True) as c:
        c.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(
                sql.Identifier(name)
            )
        )
    try:
        upgrade({**admin, "dbname": name})
        with monkeypatch.context() as patch:
            patch.setenv("LEAD_DB_NAME", name)
            with psycopg.connect(**{**admin, "dbname": name}) as c:
                c.execute(
                    "UPDATE lead.platform_capabilities SET technical_state='verified',permission_state='approved' WHERE platform='telegram' AND action_type='publish'"
                )
            yield make_agent(patch)
    finally:
        with psycopg.connect(**admin, autocommit=True) as c:
            c.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


def owner_client():
    client = TestClient(app)
    client.cookies.set("lead_session", token("owner"))
    return client


OWNER = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}


def test_compact_source_parameter_preserves_queries_and_hash():
    destination = (
        "https://site.example/start?tag=a&tag=b&empty=&lead_ref=old&lr=old#pricing"
    )
    for platform, prefix in PLATFORM_CODES.items():
        marker = new_marker(platform)
        assert marker.startswith(prefix + "_")
        base = "https://site.example/start"
        assert len(attributed_url(base, marker)) - len(base) <= 18
        parts = urlsplit(attributed_url(destination, marker))
        assert parse_qsl(parts.query, keep_blank_values=True) == [
            ("tag", "a"),
            ("tag", "b"),
            ("empty", ""),
            ("lr", marker),
        ]
        assert parts.fragment == "pricing"
        body, link = promotion_body(
            "Try https://site.example/old then https://site.example/other",
            destination,
            marker,
        )
        assert body.count(link) == 1 and "/old" not in body and "/other" not in body


def test_promotion_configuration_versions_and_fences_old_queue(agent):
    client = owner_client()
    path = f"/api/agents/{agent}/analytics/promotion"
    original = client.get(path).json()
    assert original == {
        "destination_url": "https://site.example/",
        "revision": 0,
        "configured": False,
    }
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({"custom_name": True}), agent),
        )
        for state in ("queued", "running"):
            c.execute(
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'fixture.publish',%s,%s,'{}',now())",
                (current_tenant(), agent, state, uid()),
            )
    destination = "https://landing.example/start?offer=1&lead_ref=old#pricing"
    response = client.post(
        path,
        headers=OWNER,
        json={"destination_url": destination, "expected_revision": 0},
    )
    assert response.status_code == 200
    saved = response.json()
    assert saved["destination_url"] == "https://landing.example/start?offer=1#pricing"
    assert saved["revision"] == 1 and saved["configured"]
    assert client.get(path).json() == saved
    assert (
        client.post(
            path,
            headers=OWNER,
            json={"destination_url": "https://other.example", "expected_revision": 0},
        ).status_code
        == 409
    )
    with tx(agent) as c:
        assert one(c, "SELECT onboarding_state FROM lead.agents")["onboarding_state"][
            "custom_name"
        ]
        assert (
            one(c, "SELECT count(*) AS n FROM lead.tasks WHERE state='canceled'")["n"]
            == 1
        )
        assert (
            one(c, "SELECT count(*) AS n FROM lead.tasks WHERE state='running'")["n"]
            == 1
        )
        assert (
            one(c, "SELECT product_url FROM lead.business_profiles")["product_url"]
            == "https://site.example/"
        )
    other = domain.create_agent()
    assert (
        client.get(f"/api/agents/{other}/analytics/promotion").json()["revision"] == 0
    )


def test_channel_rows_are_connected_scoped_and_measure_real_events(agent):
    client = owner_client()
    cid = uid()
    marker = new_marker("telegram")
    with tx(agent) as c:
        c.execute(
            "INSERT INTO lead.analytics_connections(id,tenant_id,agent_id,provider,site_host,secret_ref,state) VALUES(%s,%s,%s,'lead_site','https://site.example','fixture','pending')",
            (cid, current_tenant(), agent),
        )
        c.execute(
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,'telegram',%s,'https://site.example/','site.example')",
            (current_tenant(), agent, marker),
        )
    rows = {r["platform"]: r for r in analytics.trend(UUID(agent))["channels"]}
    assert set(rows) == {"telegram", "x"}
    assert rows["telegram"]["qualified_visits"] is None
    assert rows["x"]["conversions"] is None
    response = client.get("/r/" + marker, follow_redirects=False)
    assert (
        response.status_code == 302 and "lr=" + marker in response.headers["location"]
    )
    client.get(
        "/r/" + marker, headers={"User-Agent": "previewbot"}, follow_redirects=False
    )
    sid = uid()
    event = {
        "key": token("analytics:" + cid),
        "session_id": sid,
        "event": "pageview",
        "url": attributed_url("https://site.example/", marker),
        "ref": marker,
    }
    assert (
        client.post(
            "/collect/" + cid, headers={"Origin": "https://site.example"}, json=event
        ).status_code
        == 204
    )
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.visit_sessions SET started_at=now()-interval '15 seconds' WHERE source_session_id=%s",
            (sid,),
        )
    assert (
        client.post(
            "/collect/" + cid,
            headers={"Origin": "https://site.example"},
            json={**event, "event": "engagement"},
        ).status_code
        == 204
    )
    key = client.post(
        f"/api/agents/{agent}/analytics/server-key",
        headers=OWNER,
        json={"connection_id": cid},
    ).json()["key"]
    assert (
        client.post(
            "/collect/server/conversion",
            headers={"Authorization": "Bearer " + key},
            json={
                "connection_id": cid,
                "event_id": uid(),
                "type": "signup",
                "session_id": sid,
                "ref": marker,
            },
        ).status_code
        == 200
    )
    rows = {r["platform"]: r for r in analytics.trend(UUID(agent))["channels"]}
    assert rows["telegram"] == {
        "platform": "telegram",
        "visits": 1,
        "qualified_visits": 1,
        "clicks": 1,
        "published": 0,
        "conversions": 1,
    }
    assert rows["x"]["visits"] == rows["x"]["conversions"] == 0
    other = domain.create_agent()
    assert analytics.trend(UUID(other))["channels"] == []
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.social_accounts SET state='disconnected' WHERE platform='telegram'"
        )
        c.execute(
            "UPDATE lead.analytics_connections SET last_received_at=now()-interval '3 days'"
        )
    rows = analytics.trend(UUID(agent))["channels"]
    assert len(rows) == 1 and rows[0]["platform"] == "x" and rows[0]["visits"] is None


@pytest.mark.asyncio
async def test_autonomous_promotion_uses_configured_destination_and_short_source(
    publishing_agent, monkeypatch
):
    from lead_app import activities
    from lead_app.adapters.catalog import SPECS
    from lead_app.model import ContentDecision

    agent = publishing_agent
    monkeypatch.setattr(activities, "emit", lambda *a, **k: None)
    destination = "https://landing.example/start?offer=1#pricing"
    analytics.save_promotion(
        UUID(agent),
        analytics.PromotionSetup(destination_url=destination, expected_revision=0),
    )
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.agents SET desired_state='running' WHERE id=%s", (agent,)
        )
        target = one(
            c,
            "INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,display_name,verified_permissions,state) VALUES(%s,%s,(SELECT id FROM lead.social_accounts WHERE platform='telegram'),'channel',%s,'Fixture channel',%s,'active') RETURNING id",
            (
                current_tenant(),
                agent,
                uid(),
                j(
                    {
                        "adapter_version": SPECS["telegram"].version,
                        "publishing_verified_formats": ["text"],
                    }
                ),
            ),
        )["id"]
        grant = one(
            c,
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,1,%s,'fixture') RETURNING id",
            (current_tenant(), agent, target, j(["publish"])),
        )["id"]

    async def author(schema, payload, **kwargs):
        assert payload["promotion"]["destination_url"] == destination
        return ContentDecision(
            action="publish",
            target_id=str(target),
            body="Explore the current options: https://site.example/",
            rationale="Introduce the known product.",
        )

    monkeypatch.setattr(activities, "run_model", author)
    result = await activities.autonomous_cycle({"agent_id": agent, "cycle_id": uid()})
    assert "reason" not in result, result
    with tx(agent) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE type='telegram.publish'")
        link = one(c, "SELECT * FROM lead.tracking_links")
        assert link["destination_url"] == destination
        assert link["platform"] == "telegram" and link["public_code"].startswith("tg_")
        assert task["input"]["link"] == attributed_url(destination, link["public_code"])
        assert task["input"]["body"].count(task["input"]["link"]) == 1
        assert "site.example" not in task["input"]["body"]
        for probe in (False, True):
            recorded_task = one(
                c,
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'telegram.publish','succeeded',%s,%s,now()) RETURNING id",
                (current_tenant(), agent, uid(), j({"bootstrap_verification": probe})),
            )["id"]
            c.execute(
                "INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,expected_control_version,expected_config_revision,state,external_id,confirmed_at) VALUES(%s,%s,%s,%s,%s,%s,'publish','{}',1,1,'confirmed','fixture',now())",
                (current_tenant(), agent, recorded_task, target, grant, uid()),
            )
    rows = {r["platform"]: r for r in analytics.trend(UUID(agent))["channels"]}
    assert rows["telegram"]["published"] == 1
