"""Guided creation remains private until a verified channel is connected."""

import pytest
from lead_app import domain
from lead_app.api import AgentSetupCreate
from lead_app.db import one, record, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def profile():
    return {
        "product_name": "Guided fixture",
        "product_url": "https://example.com",
        "description": "Owner supplied product description",
        "audience": "Small teams",
        "goal": "Acquire qualified visits",
        "content_expectations": "Practical examples in English",
        "strategy": "Steady operation",
        "platform": "telegram",
    }


@pytest.fixture
def setup_agent(monkeypatch, profile):
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    return domain.create_agent(uid(), setup=profile)


def test_setup_draft_is_hidden_and_preferences_are_saved(setup_agent, profile):
    assert setup_agent not in {str(a["id"]) for a in domain.list_agents()}
    a = domain.agent_detail(setup_agent)
    assert (
        a["name"] == profile["product_name"] and a["onboarding_state"]["setup_pending"]
    )
    assert a["business"]["facts"]["audience"] == profile["audience"]
    assert a["business"]["facts"]["operating_goal"] == profile["goal"]
    assert len(a["memory"]) == 3 and all(
        m["confirmation"] == "user_explicit" for m in a["memory"]
    )
    with pytest.raises(ValueError, match="创建流程"):
        domain.set_control(setup_agent, "start")
    with pytest.raises(ValueError, match="登录验证"):
        domain.complete_setup(setup_agent)


def test_completed_setup_requires_the_selected_verified_channel_and_is_idempotent(
    setup_agent,
):
    with tx(setup_agent) as c:
        pid = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        c.execute(
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id,verified_at) VALUES(%s,%s,%s,'x','connected',%s,now())",
            (current_tenant(), setup_agent, pid, uid()),
        )
    with pytest.raises(ValueError, match="登录验证"):
        domain.complete_setup(setup_agent)
    with tx(setup_agent) as c:
        c.execute(
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id,verified_at) VALUES(%s,%s,%s,'telegram','connected',%s,now())",
            (current_tenant(), setup_agent, pid, uid()),
        )
    assert domain.complete_setup(setup_agent) == {"id": setup_agent}
    assert setup_agent in {str(a["id"]) for a in domain.list_agents()}
    before = domain.agent_detail(setup_agent)
    assert domain.complete_setup(setup_agent) == {"id": setup_agent}
    assert len(domain.agent_detail(setup_agent)["messages"]) == len(before["messages"])
    assert domain.set_control(setup_agent, "archive", only_setup=True) is False
    assert domain.agent_detail(setup_agent)["desired_state"] != "archived"


def test_setup_back_navigation_replaces_preferences_and_checks_revision(
    setup_agent, profile
):
    a = domain.agent_detail(setup_agent)
    changed = {**profile, "goal": "Build brand awareness", "platform": "x"}
    domain.update_setup(setup_agent, changed, a["config_revision"])
    a2 = domain.agent_detail(setup_agent)
    assert a2["business"]["facts"]["operating_goal"] == changed["goal"]
    assert a2["onboarding_state"]["setup"]["platform"] == "x"
    assert len(a2["memory"]) == 3
    assert not any(profile["goal"] in m["body"]["text"] for m in a2["memory"])
    with pytest.raises(ValueError, match="刚有更新"):
        domain.update_setup(setup_agent, profile, a["config_revision"])


def test_setup_creation_retries_do_not_duplicate_or_change_the_original(
    profile, monkeypatch
):
    monkeypatch.setattr(domain, "emit", lambda *a, **kw: None)
    key = uid()
    aid = domain.create_agent(key, setup=profile)
    assert str(domain.create_agent(key, setup=profile)) == aid
    with pytest.raises(ValueError, match="资料已变化"):
        domain.create_agent(key, setup={**profile, "goal": "different"})
    parsed = AgentSetupCreate(client_id=uid(), **profile).profile()
    assert parsed == profile and "client_id" not in parsed


def test_cancel_only_archives_an_incomplete_draft(setup_agent):
    assert domain.set_control(setup_agent, "archive", only_setup=True)
    with tx(setup_agent) as c:
        assert (
            one(c, "SELECT desired_state FROM lead.agents")["desired_state"]
            == "archived"
        )


def test_activity_filter_is_applied_before_the_feed_limit(setup_agent):
    with tx(setup_agent) as c:
        for i in range(55):
            record(c, setup_agent, f"Telegram record {i}", "telegram")
        record(c, setup_agent, "X specific record", "x")
        record(c, setup_agent, "System event")
    telegram = domain.agent_activity(setup_agent, "telegram", 3)
    assert len(telegram) == 3 and all(a["platform"] == "telegram" for a in telegram)
    assert len(domain.agent_activity(setup_agent, "x", 3)) == 1
    assert all(
        a["platform"] is None for a in domain.agent_activity(setup_agent, "system")
    )
    assert len(domain.agent_activity(setup_agent, None, 3)) == 3
    other = domain.create_agent()
    assert not domain.agent_activity(other, "telegram")
    with pytest.raises(ValueError, match="社媒不存在"):
        domain.agent_activity(setup_agent, "unknown")


def test_activity_history_pages_do_not_repeat_records(setup_agent):
    with tx(setup_agent) as c:
        for i in range(12):
            record(c, setup_agent, f"History page {i}", "telegram")
    assert len(domain.agent_activity(setup_agent, "", 5, 0)) == 5
    first = domain.agent_activity(setup_agent, "telegram", 5, 0)
    second = domain.agent_activity(setup_agent, "telegram", 5, 5)
    assert len(first) == 5 and len(second) == 5
    assert not {r["id"] for r in first} & {r["id"] for r in second}
    assert not domain.agent_activity(setup_agent, "telegram", 5, 20)
    with pytest.raises(ValueError, match="小于零"):
        domain.agent_activity(setup_agent, "telegram", 5, -1)


def test_setup_draft_cannot_start_via_a_channel_control(setup_agent):
    with pytest.raises(ValueError, match="创建流程"):
        domain.channel_control(setup_agent, "telegram", True)
