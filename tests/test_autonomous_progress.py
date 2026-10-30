"""Blocked account work must leave independent, authorized work progressing."""

import pytest
from lead_app import (
    account_setup,
    activities,
    community_discovery,
    community_growth,
    community_runtime,
    domain,
    operating_workspace,
)
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,1,'Long-form translation with glossary settings.','https://seka.one','{}')",
            (current_tenant(), aid),
        )
        pending = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'account.prepare','running',%s,'{}',%s,now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j({"creation_state": "unknown"}),
            ),
        )
    return aid, str(pending["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "account", "research"])
async def test_drafts_and_public_research_progress_before_account_wait(
    agent, monkeypatch, failure
):
    aid, pending = agent
    calls = []

    async def discovery(*args):
        with tx(aid) as c:
            assert (
                len(
                    all_rows(
                        c, "SELECT id FROM lead.tasks WHERE type='channel.prepare'"
                    )
                )
                == 10
            )
        calls.append("research")
        if failure == "research":
            raise TimeoutError("private-provider-payload")
        return {"delay": 7, "reason": "community_discovery_progress"}

    async def nothing(*args):
        return None

    async def prepare(*args):
        calls.append("account")
        if failure == "account":
            raise ConnectionError("private-browser-payload")
        return {"delay": 60, "reason": "account_preparation_unknown"}

    async def no_participation(*args):
        raise AssertionError("Unknown account writes still fence participation")

    monkeypatch.setattr(community_growth, "discover_communities", discovery)
    monkeypatch.setattr(account_setup, "recover_saved_login", nothing)
    monkeypatch.setattr(account_setup, "prepare_requested_channel", nothing)
    monkeypatch.setattr(account_setup, "prepare_accounts", prepare)
    monkeypatch.setattr(community_runtime, "community_cycle", no_participation)
    result = await activities._autonomous_cycle({"agent_id": aid, "cycle_id": uid()})
    assert calls == ["research", "account"]
    assert result["prepared_channels"] == 10
    assert result["delay"] == (60 if failure == "research" else 7)
    with tx(aid) as c:
        assert one(
            c, "SELECT result,state FROM lead.tasks WHERE id=%s", (pending,)
        ) == {"result": {"creation_state": "unknown"}, "state": "running"}
        assert not all_rows(c, "SELECT id FROM lead.actions")
        assert "private-" not in str(
            all_rows(c, "SELECT title,detail FROM lead.activity")
        )


def test_one_invalid_channel_does_not_block_other_drafts_or_spam_warnings(
    agent, monkeypatch
):
    aid, _ = agent
    original = operating_workspace.channel_config

    def channel(c, aid, platform, target_id=None):
        if platform == "instagram":
            raise ValueError("private-invalid-configuration")
        return original(c, aid, platform, target_id)

    monkeypatch.setattr(operating_workspace, "channel_config", channel)
    for _ in range(2):
        packs = operating_workspace.prepare_all(aid, continue_on_error=True)
        assert sum("plans" in pack for pack in packs) == 9
        assert next(p for p in packs if p["platform"] == "instagram") == {
            "platform": "instagram",
            "state": "failed",
            "error_type": "ValueError",
        }
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.activity WHERE title='部分渠道底稿暂未完成'",
            )["n"]
            == 1
        )
        assert not all_rows(c, "SELECT id FROM lead.actions")


@pytest.mark.asyncio
async def test_paused_agent_does_not_prepare_or_research(agent, monkeypatch):
    aid, _ = agent
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (aid,))

    async def unexpected(*args):
        raise AssertionError("Paused work must not start")

    monkeypatch.setattr(community_growth, "discover_communities", unexpected)
    result = await activities._autonomous_cycle({"agent_id": aid, "cycle_id": uid()})
    assert result["idle"]
    with tx(aid) as c:
        assert not all_rows(c, "SELECT id FROM lead.tasks WHERE type='channel.prepare'")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["discovery", "contextual_outreach"])
async def test_pending_account_write_allows_only_public_discovery(
    agent, monkeypatch, mode
):
    aid, pending = agent
    community_growth.set_strategy(aid, {"enabled": True, "mode": mode})
    visits = []

    class Browser:
        def __init__(self, *args, **kwargs):
            assert kwargs["mode"] == "community_discovery"

        async def command(self, operation, args):
            assert operation == "open"
            visits.append(args["url"])
            return {"error": "local fixture: no remote request"}

        async def finish(self, *args, **kwargs):
            return []

    monkeypatch.setattr(community_discovery, "BrowserTools", Browser)
    result = await community_discovery.advance(aid)
    assert len(visits) == (1 if mode == "discovery" else 0)
    if mode == "discovery":
        assert result["reason"] == "community_discovery_progress"
    else:
        assert result is None
    with tx(aid) as c:
        assert one(c, "SELECT result FROM lead.tasks WHERE id=%s", (pending,))[
            "result"
        ] == {"creation_state": "unknown"}
        assert not all_rows(c, "SELECT id FROM lead.actions")
