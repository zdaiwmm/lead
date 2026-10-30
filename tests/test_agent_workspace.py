"""Workspace edits and independent channels, using isolated agent records."""

import pytest
from lead_app import account_setup, domain
from lead_app.analytics import trend
from lead_app.api import BrowserInput
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(account_setup, "emit", lambda *a, **kw: None)
    monkeypatch.setattr(
        domain, "model_config", lambda: {"key": "fixture-only", "model": "fixture"}
    )
    aid = domain.create_agent()
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        for platform in ("telegram", "x"):
            c.execute(
                "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id) VALUES(%s,%s,%s,%s,'connected',%s)",
                (current_tenant(), aid, profile, platform, uid()),
            )
    return aid


def test_channel_pause_does_not_pause_other_platform_or_global_agent(agent):
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.agents SET desired_state='running' WHERE id=%s", (agent,)
        )
        for platform in ("telegram", "x"):
            c.execute(
                "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at) VALUES(%s,%s,'fixture.publish','queued',%s,%s,now())",
                (current_tenant(), agent, uid(), j({"platform": platform})),
            )
    domain.channel_control(agent, "telegram", False)
    detail = domain.agent_detail(agent)
    assert detail["desired_state"] == "running"
    assert {a["platform"]: a["automation_enabled"] for a in detail["accounts"]} == {
        "telegram": False,
        "x": True,
    }
    with tx(agent) as c:
        assert {
            t["input"]["platform"]: t["state"]
            for t in all_rows(c, "SELECT * FROM lead.tasks")
        } == {"telegram": "canceled", "x": "queued"}
        assert (
            one(c, "SELECT detail FROM lead.activity ORDER BY created_at DESC LIMIT 1")[
                "detail"
            ]
            == "telegram"
        )


def test_start_channel_is_atomic_if_model_missing(agent, monkeypatch):
    domain.channel_control(agent, "telegram", False)
    monkeypatch.setattr(domain, "model_config", lambda: {"key": ""})
    with pytest.raises(ValueError, match="DEEPSEEK"):
        domain.channel_control(agent, "telegram", True)
    with tx(agent) as c:
        assert (
            one(
                c,
                "SELECT automation_enabled FROM lead.social_accounts WHERE platform='telegram'",
            )["automation_enabled"]
            is False
        )
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"] == "draft"
        )
        assert not one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_account'"
        )


def test_start_one_channel_does_not_resume_every_connected_channel(agent):
    domain.channel_control(agent, "telegram", True)
    detail = domain.agent_detail(agent)
    assert detail["desired_state"] == "running"
    assert {a["platform"]: a["automation_enabled"] for a in detail["accounts"]} == {
        "telegram": True,
        "x": False,
    }


def test_inline_preferences_preserve_revision_and_explicit_identity(agent):
    detail = domain.agent_detail(agent)
    domain.update_identity(agent, "我的运营 Agent", 2)
    domain.update_business(
        agent,
        detail["config_revision"],
        "https://example.com",
        "业务介绍",
        "独立开发者",
    )
    updated = domain.agent_detail(agent)
    assert updated["name"] == "我的运营 Agent"
    assert updated["onboarding_state"]["custom_name"] is True
    assert updated["business"]["facts"]["audience"] == "独立开发者"
    with pytest.raises(ValueError, match="更新"):
        domain.update_business(
            agent, detail["config_revision"], "https://example.com", "过期资料", ""
        )
    domain.update_preferences(
        agent, updated["config_revision"], ["使用英文", "保持专业"]
    )
    saved = domain.agent_detail(agent)
    assert {m["body"]["text"] for m in saved["memory"]} == {"使用英文", "保持专业"}
    assert all(m["confirmation"] == "user_explicit" for m in saved["memory"])


def test_trend_uses_real_rolling_attribution_and_is_agent_scoped(agent):
    other = domain.create_agent()
    with tx(agent) as c:
        link = one(
            c,
            "INSERT INTO lead.tracking_links(tenant_id,agent_id,platform,public_code,destination_url,allowed_host) VALUES(%s,%s,'direct',%s,'https://example.com','example.com') RETURNING id",
            (current_tenant(), agent, uid()),
        )["id"]
        for trust, qualified, reason, attributed, age in [
            ("client_observed", True, None, True, "1 day"),
            ("client_observed", False, None, True, "1 day"),
            ("client_observed", True, None, False, "1 day"),
            ("redirect_observed", None, None, True, "1 day"),
            ("redirect_observed", None, "bot", True, "1 day"),
            ("client_observed", True, None, True, "31 days"),
        ]:
            c.execute(
                "INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,qualified,exclusion_reason,rule_version,started_at,last_seen_at) VALUES(%s,%s,%s,'fixture',%s,%s,%s,%s,'engaged-v1',now()-%s::interval,now())",
                (
                    current_tenant(),
                    agent,
                    link if attributed else None,
                    uid(),
                    trust,
                    qualified,
                    reason,
                    age,
                ),
            )
    data = trend(agent)
    assert data["timezone"] == "Asia/Shanghai" and len(data["days"]) == 31
    assert sum(d["visits"] for d in data["days"]) == 2
    assert sum(d["qualified_visits"] for d in data["days"]) == 1
    assert sum(d["clicks"] for d in data["days"]) == 1
    assert sum(d["visits"] for d in trend(other)["days"]) == 0


def test_browser_navigation_commands_are_validated():
    for kind in ("back", "forward", "reload"):
        assert BrowserInput(kind=kind).kind == kind
    with pytest.raises(ValueError):
        BrowserInput(kind="evaluate")


def test_site_coverage_distinguishes_stale_from_never_connected(agent):
    with tx(agent) as c:
        assert domain.metrics(c)["coverage"] == "not_connected"
        c.execute(
            "INSERT INTO lead.analytics_connections(tenant_id,agent_id,provider,site_host,secret_ref,state,last_received_at) VALUES(%s,%s,'lead_site','https://example.com','fixture','verified',now()-interval '3 days')",
            (current_tenant(), agent),
        )
        m = domain.metrics(c)
        assert m["coverage"] == "stale" and m["visits"] is None
        assert m["last_received_at"]
        c.execute("UPDATE lead.analytics_connections SET last_received_at=now()")
        m = domain.metrics(c)
        assert m["coverage"] == "connected" and m["visits"] == 0


def test_usage_exposes_pricing_version_without_hiding_unpriced_calls(agent):
    from lead_app import budget, metering

    known = budget.reserve(agent, uid(), 40)
    budget.settle(
        agent,
        known,
        actual=40,
        details={
            "cost": "1.25",
            "currency": "USD",
            "model": "fixture",
            "rate_version": "fixture-v1",
            "rate_effective_at": "2026-10-01T00:00:00Z",
        },
    )
    unknown = budget.reserve(agent, uid(), 10)
    budget.settle(agent, unknown, actual=10)
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.usage_events SET details=%s,rate_version='fixture-v1' WHERE reservation_id=%s",
            (
                j(
                    {
                        "cost": "1.25",
                        "currency": "USD",
                        "model": "fixture",
                        "rate_version": "fixture-v1",
                        "rate_effective_at": "2026-10-01T00:00:00Z",
                    }
                ),
                known,
            ),
        )
    result = metering.summary(agent)
    assert result["model_tokens"] == 50 and result["model_costs"] == {"USD": "1.25"}
    assert result["unpriced_calls"] == 1
    assert result["pricing_versions"] == [
        ("fixture", "USD", "fixture-v1", "2026-10-01T00:00:00Z")
    ]


def test_paused_channel_rejects_its_existing_delegation(agent):
    domain.channel_control(agent, "telegram", True)
    with tx(agent) as c:
        card = one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_account'"
        )
    domain.channel_control(agent, "telegram", False)
    with tx(agent) as c:
        a = one(c, "SELECT * FROM lead.agents")
        account = one(c, "SELECT * FROM lead.social_accounts WHERE platform='telegram'")
        with pytest.raises(ValueError, match="授权已变化"):
            account_setup.require_delegation(c, a, account, str(card["id"]))
