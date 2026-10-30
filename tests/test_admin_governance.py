"""Published catalogs, scoped controls, pinned attribution and reviewed experience."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import psycopg
import pytest
from lead_app import admin_config as configs
from lead_app import admin_controls as controls
from lead_app import admin_experiences as experiences
from lead_app import admin_management as management
from lead_app import admin_operations, admin_settings, domain, metering, model
from lead_app.config import database_kwargs, resolve_model
from lead_app.db import one, tx, uid
from lead_app.scope import tenant_scope
from lead_app.security import token
from openai import AsyncOpenAI
from pydantic import BaseModel
from test_admin_phase3 import HEADERS, web


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'governance verification')",
            (tenant,),
        )
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(admin_operations, "emit", lambda *a, **k: None)
    with tenant_scope(
        tenant,
        {"tenant_id": tenant, "role": "owner", "issuer": "local", "subject": "local"},
    ):
        yield tenant
        with tx() as c:
            c.execute("UPDATE lead.agents SET desired_state='archived'")


def publish(c, key, body, action="publish"):
    return configs.write(c, key, body, configs.read(c, key)["version"], action=action)


def test_model_catalog_drafts_in_use_guard_versions_and_workspace_isolation(
    workspace, monkeypatch
):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-6-luna")
    monkeypatch.setenv("OPENAI_ALLOWED_MODELS", "")
    monkeypatch.setenv("LEAD_ALLOWED_MODELS", "")
    key = "model-catalog/default"
    candidates = {"openai_models": ["gpt-6-luna", "gpt-6-sol", "gpt-6-astra"]}
    aid = domain.create_agent()
    with tx() as c:
        publish(c, key, candidates, "draft")
        assert "openai:gpt-6-sol" not in admin_settings.model_catalog(c)
        publish(c, key, candidates)
        body = admin_settings.defaults("models")
        body["task_models"]["chat"] = "openai:gpt-6-sol"
        publish(c, "models/" + aid, body)
        assert "openai:gpt-6-sol" in admin_settings.model_catalog(c)
        publish(c, key, {"openai_models": ["gpt-6-luna"]}, "draft")
    with pytest.raises(ValueError, match="正在使用"):
        with tx() as c:
            publish(c, key, {"openai_models": ["gpt-6-luna"]})
    with tx() as c:
        body["task_models"]["chat"] = "openai:gpt-6-luna"
        publish(c, "models/" + aid, body)
        publish(c, key, {"openai_models": ["gpt-6-luna"]})
        restored = configs.write(
            c, key, {}, configs.read(c, key)["version"], "rollback", rollback_version=2
        )
        assert restored["body"] == candidates
        assert len(configs.history(c, key)) == 5
    other = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'separate catalog')", (other,)
        )
    with tenant_scope(other, None), tx() as c:
        assert "openai:gpt-6-sol" not in admin_settings.model_catalog(c)
        assert configs.read(c, key)["version"] == 0


class Answer(BaseModel):
    reply: str


@pytest.mark.asyncio
async def test_two_openai_task_models_use_one_key_and_record_actual_models(
    workspace, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "one-shared-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    aid = domain.create_agent()
    with tx() as c:
        publish(
            c, "model-catalog/default", {"openai_models": ["gpt-6-luna", "gpt-6-sol"]}
        )
        body = admin_settings.defaults("models")
        body["task_models"].update(
            chat="openai:gpt-6-luna", planning="openai:gpt-6-sol"
        )
        publish(c, "models/" + aid, body)
    calls = []

    def respond(request):
        payload = json.loads(request.content)
        calls.append((payload["model"], request.headers["authorization"]))
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "object": "response",
                "created_at": 1793318400,
                "status": "completed",
                "model": payload["model"],
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": '{"reply":"ok"}',
                                "annotations": [],
                            }
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "input_tokens_details": {"cached_tokens": 20},
                    "output_tokens": 20,
                    "output_tokens_details": {"reasoning_tokens": 0},
                    "total_tokens": 120,
                },
            },
        )

    monkeypatch.setattr(
        model,
        "AsyncOpenAI",
        lambda **kwargs: AsyncOpenAI(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
        ),
    )
    for purpose in ["chat", "planning"]:
        result = await model.run_model(
            Answer, "Return ok", agent_id=aid, purpose=purpose
        )
        assert result.reply == "ok"
    assert calls == [
        ("gpt-6-luna", "Bearer one-shared-test-key"),
        ("gpt-6-sol", "Bearer one-shared-test-key"),
    ]
    assert (
        resolve_model("openai:gpt-6-sol")["key"]
        == resolve_model("openai:gpt-6-luna")["key"]
    )
    rows = metering.summary(aid)["by_model"]
    assert {r["model"]: r["tokens"] for r in rows} == {
        "openai:gpt-6-luna": 120,
        "openai:gpt-6-sol": 120,
    }
    assert "one-shared-test-key" not in web().get("/api/admin/model-options").text
    with tx() as c:
        assert "one-shared-test-key" not in json.dumps(configs.history(c), default=str)


def test_directory_bulk_control_is_atomic_and_checks_versions(workspace):
    a, b = domain.create_agent(), domain.create_agent()
    with tx() as c:
        c.execute(
            "UPDATE lead.agents SET name='Batch A',desired_state='running' WHERE id=%s",
            (a,),
        )
        c.execute(
            "UPDATE lead.agents SET name='Batch B',desired_state='running' WHERE id=%s",
            (b,),
        )
        rows = management.directory(c, query="Batch")
    versions = {str(row["id"]): row["control_version"] for row in rows}
    client = web()
    rejected = client.post(
        "/api/admin/agents/control",
        headers=HEADERS,
        json={
            "agents": [a, b],
            "action": "pause",
            "expected_control_versions": {**versions, b: 999},
        },
    )
    assert rejected.status_code == 409
    with tx() as c:
        assert {r["desired_state"] for r in management.directory(c, query="Batch")} == {
            "running"
        }
    accepted = client.post(
        "/api/admin/agents/control",
        headers=HEADERS,
        json={
            "agents": [a, b],
            "action": "pause",
            "expected_control_versions": versions,
            "note": "pause review",
        },
    )
    assert accepted.status_code == 200, accepted.text
    with tx() as c:
        rows = management.directory(c, query="Batch")
        assert {r["desired_state"] for r in rows} == {"paused"}
        versions = {str(row["id"]): row["control_version"] for row in rows}
        management.control_agents(c, [a, b], "archive", versions, "test archive")
        assert not management.directory(c, query="Batch")
        assert len(management.directory(c, query="Batch", state="archived")) == 2
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.audit_events WHERE type='admin.agents.archive'",
            )["n"]
            == 1
        )


def channel_agent():
    aid = domain.create_agent()
    domain.connect_lab(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.social_accounts SET platform='facebook'")
        target = str(one(c, "SELECT id FROM lead.account_targets")["id"])
        business = one(
            c,
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url) VALUES(lead.ctx_tenant(),%s,1,'Private business','https://example.com') RETURNING *",
            (aid,),
        )
    return aid, target, business


def test_channel_rules_narrow_parent_and_are_enforced_before_dispatch(workspace):
    aid, target, _ = channel_agent()
    with tx() as c:
        parent = controls.ChannelRule(
            formats=["text"], daily_publish_limit=2
        ).model_dump()
        publish(c, "channel-rules/facebook", parent, "draft")
        assert (
            controls.channel_policy(c, aid, "facebook", target)[0][
                "daily_publish_limit"
            ]
            is None
        )
        publish(c, "channel-rules/facebook", parent)
        publish(
            c,
            "channel-rules/facebook/" + aid,
            controls.ChannelRule(daily_publish_limit=20).model_dump(),
        )
        policy, _ = controls.channel_policy(c, aid, "facebook", target)
        assert policy["daily_publish_limit"] == 2 and policy["formats"] == ["text"]
    with tx(aid) as c:
        controls.enforce_channel(c, aid, "facebook", target, content_format="text")
        with pytest.raises(ValueError, match="未允许"):
            controls.enforce_channel(c, aid, "facebook", target, content_format="image")
    with tx() as c:
        publish(
            c,
            "channel-rules/facebook/" + aid + "/" + target,
            controls.ChannelRule(enabled=False).model_dump(),
        )
    with tx(aid) as c:
        with pytest.raises(ValueError, match="停用"):
            controls.enforce_channel(c, aid, "facebook", target, operation="setup")
        with pytest.raises(ValueError, match="停用"):
            controls.enforce_channel(c, aid, "facebook", target, operation="community")


def test_visits_pin_engagement_and_conversion_window_across_config_changes(workspace):
    aid = domain.create_agent()
    cid, code = uid(), uid().replace("-", "")[:16]
    client = web()
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.analytics_connections(id,tenant_id,agent_id,provider,site_host,secret_ref,state) VALUES(%s,lead.ctx_tenant(),%s,'lead_site','https://business.example','hmac','pending')",
            (cid, aid),
        )
        c.execute(
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(lead.ctx_tenant(),%s,'direct',%s,'https://business.example/','business.example')",
            (aid, code),
        )
    with tx() as c:
        publish(
            c,
            "attribution/" + aid,
            controls.AttributionSettings(
                engagement_seconds=30, attribution_window_days=1
            ).model_dump(),
        )
    session = uid()
    body = {
        "key": token("analytics:" + cid),
        "session_id": session,
        "event": "pageview",
        "url": "https://business.example/",
        "ref": code,
    }
    result = client.post(
        "/collect/" + cid, json=body, headers={"Origin": "https://business.example"}
    )
    assert (
        result.status_code == 204
        and result.headers["x-lead-engagement-seconds"] == "30"
    )
    with tx() as c:
        publish(
            c,
            "attribution/" + aid,
            controls.AttributionSettings(
                engagement_seconds=10, attribution_window_days=30
            ).model_dump(),
        )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.visit_sessions SET started_at=now()-interval '15 seconds'"
        )
    body["event"] = "engagement"
    result = client.post(
        "/collect/" + cid, json=body, headers={"Origin": "https://business.example"}
    )
    assert result.headers["x-lead-engagement-seconds"] == "30"
    with tx(aid) as c:
        assert one(c, "SELECT qualified FROM lead.visit_sessions")["qualified"] is False
        c.execute("UPDATE lead.visit_sessions SET started_at=now()-interval '2 days'")
    server = client.post(
        f"/api/agents/{aid}/analytics/server-key",
        headers=HEADERS,
        json={"connection_id": cid},
    ).json()
    conversion = {
        "connection_id": cid,
        "event_id": "private-window",
        "type": "signup",
        "session_id": session,
        "ref": code,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    headers = {"Authorization": "Bearer " + server["key"]}
    assert (
        client.post(
            "/collect/server/conversion", json=conversion, headers=headers
        ).status_code
        == 422
    )
    conversion["occurred_at"] = (
        datetime.now(timezone.utc) - timedelta(days=1, hours=12)
    ).isoformat()
    accepted = client.post(
        "/collect/server/conversion", json=conversion, headers=headers
    )
    assert accepted.status_code == 200, accepted.text
    with tx(aid) as c:
        assert (
            one(c, "SELECT attribution_version FROM lead.conversion_events")[
                "attribution_version"
            ]
            == "attribution/" + aid + "@1"
        )


def test_experience_review_context_and_explicit_template_promotion(workspace):
    from lead_app import operating_playbooks

    aid, target, business = channel_agent()
    key = "experiences/" + aid + "/" + uid()
    body = controls.ExperienceSettings(
        platform="facebook",
        target_id=target,
        observation="Still insufficient outcome evidence.",
        applicable_conditions="This business and target only.",
        proposed_change="Compare mature samples before expanding.",
    ).model_dump(mode="json")
    with tx() as c:
        pending = publish(c, key, body, "draft")
        assert not experiences.approved_for(c, aid, "facebook", target, business["id"])
        body["review_status"] = "approved"
        publish(c, key, body)
        assert (
            len(experiences.approved_for(c, aid, "facebook", target, business["id"]))
            == 1
        )
        assert not experiences.approved_for(c, aid, "facebook", uid(), business["id"])
        assert not experiences.approved_for(c, aid, "facebook", target, uid())
        context = operating_playbooks.planning_context(
            c,
            [
                {
                    "platform": "facebook",
                    "target_id": target,
                    "available_formats": ["text"],
                }
            ],
            business,
            1,
        )[target]
        snapshot = operating_playbooks.plan_snapshot(context)
        assert (
            snapshot["strategy_context"]["approved_experiences"][0]["config_key"] == key
        )
        template = configs.read(c, "strategies/facebook")["body"]
        template["cadence"] = "Preserve human draft cadence"
        publish(c, "strategies/facebook", template, "draft")
        source = configs.read(c, key)
        result = experiences.promote(
            c,
            key,
            source["version"],
            "strategies/facebook",
            1,
            "Review comparable mature samples.",
            "Use comparable windows and the same objective.",
        )
        assert result["draft"]["cadence"] == "Preserve human draft cadence"
        assert (
            "Review comparable mature samples." in result["draft"]["experience_notes"]
        )
        assert "Private business" not in result["draft"]["experience_notes"]
        assert result["published_version"] == 0
        assert key + "@2" in result["draft"]["evidence_refs"]
        assert pending["published_version"] == 0
    with pytest.raises(ValueError, match="真实发布"):
        with tx() as c:
            publish(c, key, {**body, "evidence_action_ids": [uid()]})


@pytest.mark.parametrize("role", ["viewer", "operator"])
def test_new_controls_and_catalog_are_owner_only(workspace, monkeypatch, role):
    from lead_app import api as api_module
    from lead_app import identity_store

    monkeypatch.setattr(api_module, "auth_mode", lambda: "oidc")
    monkeypatch.setattr(
        identity_store,
        "lookup_session",
        lambda _: {
            "tenant_id": workspace,
            "role": role,
            "issuer": "test",
            "subject": "readonly",
        },
    )
    client = web()
    assert client.get("/api/admin/agent-directory").status_code == 200
    assert client.get("/api/admin/config?key=model-catalog/default").status_code == 200
    assert client.get("/api/admin/openai-models").status_code == 403
    assert (
        client.post(
            "/api/admin/config",
            headers=HEADERS,
            json={
                "key": "model-catalog/default",
                "body": {"openai_models": ["gpt-6-sol"]},
                "expected_version": 0,
            },
        ).status_code
        == 403
    )
    assert (
        client.post("/api/admin/agents/control", headers=HEADERS, json={}).status_code
        == 403
    )
    assert (
        client.post(
            "/api/admin/strategy-experiences/promote", headers=HEADERS, json={}
        ).status_code
        == 403
    )
