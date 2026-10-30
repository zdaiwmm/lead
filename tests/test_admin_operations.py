"""Phase-two settings are real controls, scoped and versioned like phase one."""

import copy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import admin_config as config
from lead_app import admin_notifications as notices
from lead_app import admin_operations as operations
from lead_app import admin_settings as settings
from lead_app import api as api_module
from lead_app import budget, domain
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import tenant_scope
from lead_app.security import token

HEADERS = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'phase-two verification')",
            (tenant,),
        )
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(budget, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(operations, "emit", lambda *a, **kw: None)
    with tenant_scope(
        tenant,
        {"tenant_id": tenant, "role": "owner", "issuer": "local", "subject": "local"},
    ):
        yield tenant
        with tx() as c:
            c.execute(
                "UPDATE lead.agents SET desired_state='archived' WHERE desired_state<>'archived'"
            )


def web():
    result = TestClient(api_module.app)
    result.cookies.set("lead_session", token("owner"))
    return result


def publish(key, body, action="publish"):
    with tx() as c:
        return config.write(c, key, body, config.read(c, key)["version"], action=action)


def running_agent():
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
    return aid


@pytest.mark.parametrize("kind", ["models", "brand", "runtime", "notifications"])
def test_settings_default_draft_override_inheritance_and_immutable_versions(
    workspace, kind
):
    aid = domain.create_agent()
    client = web()
    value = client.get("/api/admin/config", params={"key": kind + "/default"})
    assert value.status_code == 200
    body = value.json()["body"]
    assert config.validate(kind + "/default", body)
    if kind == "models":
        body["max_output_tokens"] = 1200
    elif kind == "brand":
        body["voice"] = "Workspace voice"
    elif kind == "runtime":
        body["max_concurrent_agents"] = 2
    else:
        body["budget_warning_percent"] = 75
    publish(kind + "/default", body, "draft")
    with tx(aid) as c:
        assert settings.effective(c, kind, aid)[1].endswith("builtin@0")
    publish(kind + "/default", body)
    with tx(aid) as c:
        assert settings.effective(c, kind, aid)[0] == body
    override = copy.deepcopy(body)
    if kind == "models":
        override["max_output_tokens"] = 1500
    elif kind == "brand":
        override["voice"] = "Agent voice"
    elif kind == "runtime":
        override["max_concurrent_agents"] = 1
    else:
        override["budget_warning_percent"] = 85
    own = publish(kind + "/" + aid, override)
    with tx(aid) as c:
        assert settings.effective(c, kind, aid)[0] == override
        config.write(c, kind + "/" + aid, {}, own["version"], reset_to_default=True)
        assert settings.effective(c, kind, aid)[0] == body
    with tx() as c:
        assert config.history(c, kind + "/default")[0]["action"] == "publish"


def test_invalid_settings_and_credentials_never_reach_configuration_history(workspace):
    client = web()
    candidates = []
    body = settings.defaults("models")
    body["default_model"] = "not-permitted-model"
    candidates.append(("models", body))
    body = settings.defaults("models")
    body["api_key"] = "MUST_NOT_BE_SAVED"
    candidates.append(("models", body))
    body = settings.defaults("runtime")
    body["work_hours"]["timezone"] = "Invalid/Unknown"
    candidates.append(("runtime", body))
    body = settings.defaults("runtime")
    body["work_hours"]["weekdays"] = []
    candidates.append(("runtime", body))
    body = settings.defaults("runtime")
    body["work_hours"].update(enabled=True, start="09:00", end="09:00")
    candidates.append(("runtime", body))
    body = settings.defaults("notifications")
    body["recipient_roles"] = []
    candidates.append(("notifications", body))
    body = settings.defaults("notifications")
    body["recipient_subjects"] = ["unknown-member"]
    candidates.append(("notifications", body))
    for kind, body in candidates:
        response = client.post(
            "/api/admin/config",
            headers=HEADERS,
            json={"key": kind + "/default", "expected_version": 0, "body": body},
        )
        assert response.status_code == 409, response.text
    assert client.get("/api/admin/history").json() == []
    assert "MUST_NOT_BE_SAVED" not in client.get("/api/admin/model-options").text


def test_brand_knowledge_approval_expiry_fences_old_content_and_hard_blocks_phrases(
    workspace,
):
    from lead_app.activities import prepare_action

    aid = domain.create_agent()
    domain.connect_lab(aid)
    queued = domain.queue_lab_publish(aid, "old direction", uid())
    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    body = settings.defaults("brand")
    body["forbidden_phrases"] = ["Guaranteed profit"]
    body["facts"] = ["Owner supplied product fact"]
    body["documents"] = [
        {
            "id": uid(),
            "title": "Current",
            "content": "Approved current knowledge",
            "source": "Owner document",
            "status": "approved",
            "valid_until": "2026-10-30",
            "tags": [],
        },
        {
            "id": uid(),
            "title": "Expired",
            "content": "Old pricing",
            "source": "Old source",
            "status": "approved",
            "valid_until": "2026-10-29",
            "tags": [],
        },
        {
            "id": uid(),
            "title": "Pending",
            "content": "Unreviewed assertion",
            "source": "User",
            "status": "pending",
            "valid_until": None,
            "tags": [],
        },
    ]
    before = domain.agent_detail(aid)["config_revision"]
    publish("brand/" + aid, body)
    with tx(aid) as c:
        context = settings.brand_context(c, aid, now)
        assert [doc["title"] for doc in context["documents"]] == ["Current"]
        assert context["trust"] == "owner_provided"
        assert (
            one(c, "SELECT state FROM lead.tasks WHERE id=%s", (queued["id"],))["state"]
            == "canceled"
        )
        assert (
            one(c, "SELECT config_revision FROM lead.agents WHERE id=%s", (aid,))[
                "config_revision"
            ]
            > before
        )
    unsafe = domain.queue_lab_publish(aid, "This is GUARANTEED PROFIT", uid())
    with pytest.raises(ValueError, match="品牌禁用表达"):
        prepare_action(aid, str(unsafe["id"]))
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.actions")["n"] == 0


@pytest.mark.asyncio
async def test_model_routing_limits_brand_context_and_usage_snapshot(
    workspace, monkeypatch
):
    from lead_app import model

    aid = domain.create_agent()
    allowed = settings.model_catalog()
    selected = "verification-model"
    monkeypatch.setattr(settings, "model_catalog", lambda *a: [*allowed, selected])
    body = settings.defaults("models")
    body.update(
        max_output_tokens=800,
        timeout_seconds=20,
        max_model_requests=2,
        max_tool_calls=0,
    )
    body["task_models"]["chat"] = selected
    publish("models/" + aid, body)
    captured = {}
    monkeypatch.setattr(
        model,
        "model_config",
        lambda: {
            "model": "environment-model",
            "base_url": "https://models.invalid",
            "key": "secret-stays-on-server",
        },
    )

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)

        async def run(self, encoded, **kwargs):
            captured["prompt"] = encoded
            captured["limits"] = kwargs["usage_limits"]
            return SimpleNamespace(
                output=model.Decision(reply="A response"),
                usage=SimpleNamespace(total_tokens=5, details={}, requests=1),
            )

    monkeypatch.setattr(model, "Agent", FakeAgent)
    monkeypatch.setattr(
        model,
        "DeepSeekChatModel",
        lambda name, **kwargs: captured.update(selected_model=name),
    )
    monkeypatch.setattr(model, "reserve", lambda *args: "reservation")
    monkeypatch.setattr(
        model, "settle", lambda *args, **kwargs: captured.update(settlement=kwargs)
    )
    result = await model.run_model(model.Decision, {"message": "Test"}, agent_id=aid)
    assert result.reply == "A response"
    assert captured["selected_model"] == selected
    assert captured["model_settings"]["max_tokens"] == 800
    assert captured["model_settings"]["timeout"] == 20
    assert (
        captured["limits"].request_limit == 2
        and captured["limits"].tool_calls_limit == 0
    )
    assert "managed_brand_knowledge" in captured["prompt"]
    assert (
        captured["settlement"]["details"]["configuration_version"]
        == "models/" + aid + "@1"
    )


@pytest.mark.asyncio
async def test_disabled_tools_stop_before_browser_model_or_media_work(
    workspace, monkeypatch
):
    from lead_app import (
        browser_tools,
        community_growth,
        creative_pipeline,
        hybrid_research,
    )

    aid = domain.create_agent()
    body = settings.defaults("models")
    body["tools"] = {key: False for key in body["tools"]}
    publish("models/" + aid, body)
    monkeypatch.setattr(
        browser_tools,
        "begin_run",
        lambda *a, **kw: pytest.fail("Disabled browser must not open"),
    )
    browser = browser_tools.BrowserTools(aid, "Disabled test")
    assert browser.tools() == []
    assert (await browser.command("open", {"url": "https://example.com"}))[
        "error"
    ] == "TOOL_DISABLED"
    with pytest.raises(ValueError, match="后台关闭"):
        await hybrid_research.explore(aid, "https://example.com", "Test")
    with pytest.raises(ValueError, match="后台关闭"):
        await creative_pipeline.produce(aid, "facebook", {})
    assert await community_growth.discover_communities(aid) is None


def test_work_hours_include_overnight_start_day_and_next_weekday():
    hours = {
        "enabled": True,
        "timezone": "Asia/Shanghai",
        "weekdays": [4],
        "start": "22:00",
        "end": "02:00",
    }
    assert operations.work_window(
        hours, datetime(2026, 10, 30, 15, tzinfo=timezone.utc)
    )["allowed"]
    assert operations.work_window(
        hours, datetime(2026, 10, 30, 17, tzinfo=timezone.utc)
    )["allowed"]
    outside = operations.work_window(
        hours, datetime(2026, 10, 30, 19, tzinfo=timezone.utc)
    )
    assert not outside["allowed"] and outside["next_at"] == "2026-11-06T14:00:00+00:00"


def test_workspace_parallel_limit_and_failure_stop_are_durable(workspace, monkeypatch):
    first, second = running_agent(), running_agent()
    body = settings.defaults("runtime")
    body.update(max_concurrent_agents=1, pause_after_failures=2)
    publish("runtime/default", body)
    lease = operations.admission(first, "autonomous")
    assert lease["allowed"]
    assert operations.admission(second, "autonomous")["reason"] == "concurrency_limit"
    with tx(first) as c:
        c.execute(
            "UPDATE lead.admin_runtime_leases SET released_at=now() WHERE id=%s",
            (lease["lease_id"],),
        )
    assert operations.admission(second, "autonomous")["allowed"]
    assert not operations.failure(first, "planning", "TRANSIENT")["paused"]
    assert operations.admission(first, "autonomous")["reason"] == "failure_backoff"
    assert operations.failure(first, "planning", "TRANSIENT")["paused"]
    with tx(first) as c:
        assert one(c, "SELECT desired_state,health_state FROM lead.agents") == {
            "desired_state": "paused",
            "health_state": "needs_attention",
        }
    domain.connect_lab(first)
    monkeypatch.setattr(domain, "model_config", lambda: {"key": "verification-only"})
    domain.set_control(first, "resume")
    with tx(first) as c:
        assert one(c, "SELECT failures FROM lead.admin_runtime_state")["failures"] == 0
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "running"
        )


@pytest.mark.asyncio
async def test_managed_execution_defers_new_writes_but_reconciles_unknown_operations(
    workspace,
    monkeypatch,
):
    aid = running_agent()
    body = settings.defaults("runtime")
    body["work_hours"].update(enabled=True, weekdays=[0], start="01:00", end="01:01")
    publish("runtime/" + aid, body)
    monkeypatch.setattr(
        operations, "work_window", lambda *a, **kw: {"allowed": False, "delay": 60}
    )
    called = []

    @operations.managed_execution("publish")
    async def execute(args):
        called.append(args)
        return {"state": "confirmed"}

    domain.connect_lab(aid)
    task = domain.queue_lab_publish(aid, "Test", uid())
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.tasks SET type='facebook.publish' WHERE id=%s", (task["id"],)
        )
    assert (await execute({"agent_id": aid, "task_id": str(task["id"])}))[
        "state"
    ] == "deferred"
    assert called == []
    # Existing uncertain actions bypass admission; they still use the same ledger.
    from lead_app.activities import prepare_action

    action = prepare_action(aid, str(task["id"]))
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.actions SET state='dispatching' WHERE id=%s", (action["id"],)
        )
        c.execute(
            "UPDATE lead.actions SET state='unknown' WHERE id=%s", (action["id"],)
        )
    assert (await execute({"agent_id": aid, "task_id": str(task["id"])}))[
        "state"
    ] == "confirmed"
    assert len(called) == 1


def test_notifications_derive_from_usage_deduplicate_resolve_and_keep_personal_receipts(
    workspace,
):
    aid = domain.create_agent()
    budget.set_daily_limit(aid, 100)
    reserved = budget.reserve(aid, uid(), 85)
    notices.collect()
    notices.collect()
    client = web()
    rows = client.get("/api/admin/notifications").json()
    assert len(rows) == 1 and rows[0]["category"] == "budget_warning"
    assert (
        client.post(
            f"/api/admin/notifications/{rows[0]['id']}/read", headers=HEADERS, json={}
        ).status_code
        == 200
    )
    assert client.get("/api/admin/notifications").json()[0]["read_at"]
    budget.settle(aid, reserved, known_unbilled=True)
    notices.collect()
    assert client.get("/api/admin/notifications").json()[0]["resolved_at"]
    budget.set_daily_limit(aid, 0)
    notices.collect()
    assert (
        client.get("/api/admin/notifications").json()[0]["category"]
        == "budget_exhausted"
    )


def test_quiet_hours_delay_ordinary_notifications_and_allow_urgent_exceptions():
    body = settings.defaults("notifications")
    body["quiet_hours"].update(
        enabled=True, timezone="Asia/Shanghai", start="23:00", end="08:00"
    )
    now = datetime(2026, 10, 30, 16, 30, tzinfo=timezone.utc)
    assert notices.available_at(body, "warning", now) == datetime(
        2026, 10, 31, 0, tzinfo=timezone.utc
    )
    assert notices.available_at(body, "error", now) == now
    body["urgent_during_quiet"] = False
    assert notices.available_at(body, "error", now) > now


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_roles_cannot_publish_settings_but_can_read_only_their_notifications(
    workspace, monkeypatch, role
):
    from lead_app import identity_store

    other = {
        "tenant_id": workspace,
        "role": role,
        "subject": role,
        "issuer": "verification",
    }
    with tx() as c:
        body = settings.defaults("notifications")
        body["recipient_roles"] = ["owner", role]
        notices.condition(
            c,
            "manual-test",
            "login_expired",
            True,
            title="Verified test alert",
            settings=body,
            now=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        notification = notices.visible(c)[0]
    monkeypatch.setattr(api_module, "auth_mode", lambda: "oidc")
    monkeypatch.setattr(identity_store, "lookup_session", lambda _: other)
    client = web()
    for kind in ("models", "brand", "runtime", "notifications"):
        assert (
            client.get(
                "/api/admin/config", params={"key": kind + "/default"}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/admin/config",
                headers=HEADERS,
                json={
                    "key": kind + "/default",
                    "expected_version": 0,
                    "body": settings.defaults(kind),
                },
            ).status_code
            == 403
        )
    assert (
        client.post("/api/admin/model-check", headers=HEADERS, json={}).status_code
        == 403
    )
    assert client.get("/api/admin/notification-recipients").status_code == 403
    assert (
        client.post(
            f"/api/admin/notifications/{notification['id']}/read",
            headers=HEADERS,
            json={},
        ).status_code
        == 200
    )
    with tx() as c:
        assert (
            notices.visible(c)[0]["read_at"] is None
        )  # Owner's receipt is independent.
    hidden = uid()
    assert (
        client.post(
            f"/api/admin/notifications/{hidden}/read", headers=HEADERS, json={}
        ).status_code
        == 404
    )


def test_cross_workspace_runtime_config_and_notification_isolation(workspace):
    aid = running_agent()
    publish("models/" + aid, settings.defaults("models"))
    operations.failure(aid, "planning", "TRANSIENT")
    with tx() as c:
        notices.condition(
            c,
            "isolation",
            "login_expired",
            True,
            aid=aid,
            title="Private alert",
            now=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
        notification = notices.visible(c)[0]
    foreign = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'foreign operational settings')",
            (foreign,),
        )
    with tenant_scope(foreign):
        with tx() as c:
            assert config.head(c, "models/" + aid) is None
            assert all_rows(c, "SELECT * FROM lead.admin_runtime_state") == []
            assert notices.visible(c) == []
            with pytest.raises(LookupError):
                notices.mark_read(c, str(notification["id"]))


def test_new_admin_status_endpoints_do_not_expose_credentials(workspace):
    client = web()
    for path in (
        "/admin/model-options",
        "/admin/runtime-status",
        "/admin/notification-recipients",
        "/admin/notifications",
    ):
        response = client.get("/api" + path)
        assert response.status_code == 200, response.text
        assert "api_key" not in response.text and "password" not in response.text
