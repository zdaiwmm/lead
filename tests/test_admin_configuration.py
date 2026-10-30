"""Management configuration must affect runtime without weakening isolation."""

import copy

import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import admin_config as config
from lead_app import api as api_module
from lead_app import budget, domain
from lead_app import operating_playbooks as playbooks
from lead_app.config import database_kwargs
from lead_app.db import one, tx, uid
from lead_app.scope import tenant_scope
from lead_app.security import token

HEADERS = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}


@pytest.fixture
def workspace(monkeypatch):
    tenant = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'admin verification')",
            (tenant,),
        )
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(budget, "emit", lambda *a, **kw: None)
    with tenant_scope(
        tenant,
        {
            "tenant_id": tenant,
            "role": "owner",
            "issuer": "local",
            "subject": "admin-verification",
        },
    ):
        yield tenant
        with tx() as c:
            c.execute(
                "UPDATE lead.agents SET desired_state='archived' WHERE desired_state<>'archived'"
            )


def client():
    result = TestClient(api_module.app)
    result.cookies.set("lead_session", token("owner"))
    return result


def publish(key, body, action="publish", version=None):
    with tx() as c:
        expected = config.read(c, key)["version"] if version is None else version
        return config.write(c, key, body, expected, action=action, note="verification")


def test_budget_default_override_and_bulk_never_reset_or_cross_agent_usage(workspace):
    a, b = domain.create_agent(), domain.create_agent()
    reservation = budget.reserve(a, uid(), 100)
    budget.settle(a, reservation, actual=70)
    pending = budget.reserve(a, uid(), 20)
    budget.settle(a, pending)
    budget.set_daily_limit(b, 300)
    publish("budgets/default", {"daily_token_limit": 200})
    assert budget.get_configuration(a)["daily_token_limit"] == 200
    assert budget.get_configuration(b)["daily_token_limit"] == 300
    response = client().post(
        "/api/admin/budgets/bulk",
        headers=HEADERS,
        json={
            "agents": [a, b],
            "daily_token_limit": 75,
            "expected_versions": {a: 0, b: 1},
        },
    )
    assert response.status_code == 200, response.text
    with tx(a) as c:
        assert one(
            c, "SELECT spent_units,reserved_units,limit_units FROM lead.budget_periods"
        ) == {"spent_units": 70, "reserved_units": 20, "limit_units": 75}
    with pytest.raises(budget.BudgetExceeded):
        budget.reserve(a, uid(), 1)
    rows = client().get("/api/admin/agents").json()
    row = next(row for row in rows if row["id"] == a)
    assert row["remaining"] == 0 and row["reserved"] == 20
    budget.set_daily_limit(a, None)
    assert budget.get_configuration(a)["daily_token_limit"] == 200


def test_bulk_conflict_rolls_back_the_whole_batch_and_checks_input(workspace):
    a, b = sorted([domain.create_agent(), domain.create_agent()])
    budget.set_daily_limit(b, 50)
    response = client().post(
        "/api/admin/budgets/bulk",
        headers=HEADERS,
        json={
            "agents": [a, b],
            "daily_token_limit": 1,
            "expected_versions": {a: 0, b: 0},
        },
    )
    assert response.status_code == 409
    with tx() as c:
        assert config.read(c, "budgets/" + a)["version"] == 0
        assert config.read(c, "budgets/" + b)["body"]["daily_token_limit"] == 50
    for bad in (-1, True, 1.5):
        response = client().post(
            "/api/admin/config",
            headers=HEADERS,
            json={
                "key": "budgets/default",
                "expected_version": 0,
                "body": {"daily_token_limit": bad},
            },
        )
        assert response.status_code == 409


def test_strategy_draft_publish_specific_scope_history_and_rollback(
    workspace, monkeypatch
):
    aid = domain.create_agent()
    with tx() as c:
        template = config.read(c, "strategies/facebook")["body"]
    template["cadence"] = "first published cadence"
    template["experience_notes"] = "A pending hypothesis, based on real records"
    publish("strategies/facebook", template, "draft")
    with tx(aid) as c:
        assert config.strategy_for(c, "facebook", aid, uid()) == (None, None)
    publish("strategies/facebook", template)
    agent_strategy = {**template, "cadence": "agent cadence"}
    publish("strategies/facebook/" + aid, agent_strategy)
    monkeypatch.setattr(playbooks, "evidence", lambda *a: ([], "not_ready"))
    target = uid()
    with tx(aid) as c:
        context = playbooks.planning_context(
            c,
            [
                {
                    "platform": "facebook",
                    "target_id": target,
                    "available_formats": ["text"],
                }
            ],
            {"id": uid(), "agent_id": aid, "facts": {}},
            1,
        )[target]
    assert context["cadence"] == "agent cadence"
    assert context["catalog_version"].endswith(aid + "@1")
    snapshot = playbooks.plan_snapshot(context)
    assert (
        snapshot["strategy_context"]["experience_notes"] == template["experience_notes"]
    )
    assert snapshot["selected_plan"]["id"]
    publish("strategies/facebook/" + aid, {**agent_strategy, "cadence": "new cadence"})
    with tx() as c:
        rolled = config.write(
            c, "strategies/facebook/" + aid, {}, 2, "rollback", rollback_version=1
        )
        rows = config.history(c, "strategies/facebook/" + aid)
    assert (
        rolled["body"]["cadence"] == "agent cadence"
        and rolled["published_version"] == 3
    )
    assert [r["action"] for r in rows] == ["rollback", "publish", "publish"]
    assert rows[0]["before_state"]["body"]["cadence"] == "new cadence"
    assert snapshot["strategy_context"]["cadence"] == "agent cadence"
    with pytest.raises(ValueError, match="其他人更新"):
        publish("strategies/facebook/" + aid, agent_strategy, version=1)


def test_onboarding_pins_version_and_validates_required_fields_on_the_server(workspace):
    template = config.onboarding_default()
    template["fields"]["audience"]["required"] = True
    template["fields"]["audience"]["label"] = "期望客户"
    publish("onboarding/default", template)
    profile = {
        "product_name": "Admin setup",
        "product_url": "",
        "description": "A product",
        "audience": "",
        "goal": "Acquire users",
        "content_expectations": "",
        "strategy": "Steady",
        "platform": "telegram",
    }
    with pytest.raises(ValueError, match="期望客户"):
        domain.create_agent(setup=profile)
    profile["audience"] = "Small teams"
    aid = domain.create_agent(setup=profile, setup_template_version=1)
    newer = copy.deepcopy(template)
    newer["fields"]["content_expectations"]["required"] = True
    publish("onboarding/default", newer)
    original = domain.agent_detail(aid)
    assert original["onboarding_state"]["setup_template"]["version"] == 1
    domain.update_setup(aid, profile, original["config_revision"])
    assert (
        domain.agent_detail(aid)["onboarding_state"]["setup_template"]["version"] == 1
    )
    with pytest.raises(ValueError, match="运营内容期望"):
        domain.create_agent(setup=profile)
    # A browser that started on the old template can finish its saved draft.
    assert domain.create_agent(setup=profile, setup_template_version=1)
    newer["fields"]["description"]["required"] = False
    with pytest.raises(ValueError, match="必要信息"):
        publish("onboarding/default", newer)


@pytest.mark.parametrize("role", ["operator", "viewer"])
def test_read_only_roles_cannot_write_through_admin_or_legacy_budget_routes(
    workspace, monkeypatch, role
):
    from lead_app import identity_store

    aid = domain.create_agent()
    monkeypatch.setattr(api_module, "auth_mode", lambda: "oidc")
    monkeypatch.setattr(
        identity_store,
        "lookup_session",
        lambda _: {
            "tenant_id": workspace,
            "role": role,
            "subject": role,
            "issuer": "verification",
        },
    )
    web = client()
    assert web.get("/api/admin/config?key=onboarding/default").status_code == 200
    assert web.get("/api/admin/history").status_code == 200
    assert (
        web.post(
            "/api/admin/config",
            headers=HEADERS,
            json={
                "key": "budgets/default",
                "expected_version": 0,
                "body": {"daily_token_limit": 1},
            },
        ).status_code
        == 403
    )
    assert (
        web.post(
            f"/api/agents/{aid}/budget",
            headers=HEADERS,
            json={"daily_token_limit": 1, "expected_version": 0},
        ).status_code
        == 403
    )
    assert (
        web.post(
            "/api/admin/budgets/bulk",
            headers=HEADERS,
            json={
                "agents": [aid],
                "daily_token_limit": 1,
                "expected_versions": {aid: 0},
            },
        ).status_code
        == 403
    )


def test_database_isolation_and_immutable_history(workspace):
    a, b = domain.create_agent(), domain.create_agent()
    publish("budgets/default", {"daily_token_limit": 100})
    budget.set_daily_limit(b, 50)
    with tx(a) as c:
        assert config.published(c, "budgets/default")["daily_token_limit"] == 100
        assert config.head(c, "budgets/" + b) is None
    with tx(a) as c:
        assert (
            c.execute(
                "UPDATE lead.admin_configs SET body='{}' WHERE config_key='budgets/default'"
            ).rowcount
            == 0
        )
    # Invisible rows cannot be changed by an Agent-bound transaction.
    with tx() as c:
        assert config.published(c, "budgets/default")["daily_token_limit"] == 100
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx() as c:
            c.execute("UPDATE lead.admin_config_versions SET note='rewritten'")
    foreign = uid()
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "INSERT INTO lead.tenants(id,name) VALUES(%s,'foreign admin verification')",
            (foreign,),
        )
    with tenant_scope(foreign):
        with tx() as c:
            assert config.head(c, "budgets/default") is None
            assert config.history(c) == []
        with pytest.raises(LookupError):
            publish("budgets/" + a, {"daily_token_limit": 1})


def test_target_scope_is_validated_before_configuration_is_written(workspace):
    aid = domain.create_agent()
    with tx() as c:
        body = config.read(c, "strategies/telegram")["body"]
    with pytest.raises(LookupError, match="目标不存在"):
        publish(f"strategies/telegram/{aid}/{uid()}", body)


def test_admin_bootstrap_endpoints_and_all_channel_templates(workspace):
    web = client()
    options = web.get("/api/admin/strategy-options")
    assert options.status_code == 200
    assert len(options.json()["platforms"]) == 11
    for platform in options.json()["platforms"]:
        key = "strategies/" + platform["id"]
        response = web.get("/api/admin/config", params={"key": key})
        assert response.status_code == 200, response.text
        assert config.validate(key, response.json()["body"])
    assert web.get("/api/onboarding-template").status_code == 200


def test_strategy_can_restore_inheritance_and_same_timestamp_history_pages(workspace):
    aid = domain.create_agent()
    with tx() as c:
        base = config.read(c, "strategies/facebook")["body"]
        config.write(c, "strategies/facebook", base, 0)
        config.write(
            c, "strategies/facebook/" + aid, {**base, "cadence": "Agent override"}, 0
        )
        config.write(c, "strategies/facebook/" + aid, {}, 1, reset_to_default=True)
        first = config.history(c, limit=1)
        second = config.history(
            c, before=first[-1]["created_at"], before_id=first[-1]["id"], limit=10
        )
        assert len(first) + len(second) == 3
        assert len({r["id"] for r in first + second}) == 3
    with tx(aid) as c:
        selected, version = config.strategy_for(c, "facebook", aid, uid())
        assert selected["cadence"] == base["cadence"]
        assert version == "strategies/facebook@1"
