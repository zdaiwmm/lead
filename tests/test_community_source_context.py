"""Public source provenance may guide a bounded search before joining."""

import pytest
from lead_app import (
    channels,
    community_growth,
    community_runtime,
    domain,
    model,
    research,
)
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.mark.parametrize(
    "join_state", ["unknown", "dispatching", "pending_approval", "request_unavailable"]
)
def test_daily_discovery_cannot_replace_an_unresolved_or_rejected_application(
    monkeypatch, join_state
):
    monkeypatch.setattr(domain, "emit", lambda *args, **kwargs: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,created_at,scheduled_at) VALUES(%s,%s,'community.engage','succeeded',%s,%s,%s,now()-interval '26 hours',now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(
                    {
                        "candidate": {"telegram_url": "https://t.me/Readers_Fixture"},
                        "decision_version": community_runtime.DECISION_VERSION,
                    }
                ),
                j({"join_mode": "approval_request", "join_state": join_state}),
            ),
        )
        assert community_runtime.candidate_already_assessed(
            c, "https://t.me/readers_fixture"
        )
        assert not community_runtime.candidate_already_assessed(
            c, "https://t.me/other_fixture"
        )
        c.execute("UPDATE lead.tasks SET result='{}' WHERE id=%s", (task["id"],))
        assert not community_runtime.candidate_already_assessed(
            c, "https://t.me/readers_fixture"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("has_reply", [False, True])
async def test_unresolved_join_application_yields_but_uncertain_message_does_not(
    monkeypatch, has_reply
):
    for module in (domain, community_growth):
        monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True, "mode": "contextual_outreach"})
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        agent = one(c, "SELECT * FROM lead.agents")
        original = {
            "candidate": {"telegram_url": "https://t.me/readers_fixture"},
            "control_version": agent["control_version"],
            "config_revision": agent["config_revision"],
            "join_adapter_version": community_runtime.JOIN_ADAPTER_VERSION,
            **({"reply": {"body": "immutable fixture reply"}} if has_reply else {}),
        }
        task = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at) VALUES(%s,%s,'community.engage','running',%s,%s,%s,now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(original),
                j(
                    {
                        "join_mode": "approval_request",
                        "join_state": "confirmed" if has_reply else "unknown",
                        "reply_state": "unknown" if has_reply else None,
                        "join_reconciliation": {"observed": True},
                        "join_submitted": True,
                    }
                ),
            ),
        )
    calls = []

    async def unavailable(path, data):
        calls.append(data["operation"])
        raise ConnectionError("fixture temporary service loss")

    result = await community_runtime.community_cycle(aid, unavailable)
    assert calls == ["reply" if has_reply else "join"]
    assert result["delay"] == (300 if has_reply else 1)
    with tx(aid) as c:
        current = one(
            c,
            "SELECT state,input,result,completed_at FROM lead.tasks WHERE id=%s",
            (task["id"],),
        )
        assert current["input"] == original
        assert current["state"] == ("running" if has_reply else "succeeded")
        if not has_reply:
            assert current["result"]["join_state"] == "unknown"
            assert current["result"]["outcome"]["state"] == "pending_verification"
            assert current["completed_at"] is not None
    if not has_reply:
        assert await community_runtime.community_cycle(aid, unavailable) is None
        assert calls == ["join"]


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_matches", [True, False])
async def test_unjoined_public_group_can_search_only_with_bound_source_context(
    monkeypatch, evidence_matches
):
    for module in (domain, channels, community_growth):
        monkeypatch.setattr(module, "emit", lambda *args, **kwargs: None)
    aid = domain.create_agent()
    identity = str(int(uid().replace("-", "")[:12], 16))
    with tx(aid) as c:
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        account = one(
            c,
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state) VALUES(%s,%s,%s,'telegram','pending') RETURNING id",
            (current_tenant(), aid, profile["id"]),
        )
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts,source_refs) VALUES(%s,%s,1,'EPUB full-book translation','https://example.org','{}','[]')",
            (current_tenant(), aid),
        )
    channels.store_account(
        aid, "telegram", {"external_account_id": identity, "display_name": "Fixture"}
    )
    community_growth.set_strategy(
        aid,
        {"enabled": True, "mode": "contextual_outreach", "topics": ["EPUB 全文翻译"]},
    )
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
    run = research.begin_run(
        aid, "fixture public translation source", mode="community_discovery"
    )
    step = uid()
    source = "https://example.org/translation-project"
    group = "https://t.me/readers_fixture"
    with tx(aid) as c:
        c.execute(
            "INSERT INTO lead.browser_steps(id,tenant_id,agent_id,run_id,seq,command_id,operation,input,state,output) VALUES(%s,%s,%s,%s,1,%s,'open','{}','completed',%s)",
            (
                step,
                current_tenant(),
                aid,
                run,
                uid(),
                j(
                    {
                        "url": source,
                        "title": "EPUB translation project",
                        "text": "EPUB full-book translation. Telegram Group (Chinese): @readers_fixture",
                        "community_links": [
                            {
                                "name": "@readers_fixture",
                                "href": group
                                if evidence_matches
                                else "https://t.me/other_fixture",
                            }
                        ],
                    }
                ),
            ),
        )
        agent = one(c, "SELECT * FROM lead.agents")
        card = one(
            c, "SELECT id FROM lead.interaction_cards WHERE type='manage_community'"
        )
        previous = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at) VALUES(%s,%s,'community.engage','succeeded',%s,%s,%s,now(),now()) RETURNING id",
            (
                current_tenant(),
                aid,
                "fixture:" + uid(),
                j(
                    {
                        "candidate": {
                            "name": "Mixed discussion",
                            "telegram_url": group,
                            "source_url": source,
                            "evidence_id": step,
                            "reading_need": "Unverified model assertion",
                        },
                        "decision_version": community_runtime.DECISION_VERSION,
                        "account_id": str(account["id"]),
                        "external_account_id": identity,
                        "delegation_id": str(card["id"]),
                        "control_version": agent["control_version"],
                        "config_revision": agent["config_revision"],
                    }
                ),
                j(
                    {
                        "history_observation": {
                            "joined": False,
                            "kind": "discussion",
                            "count": 1,
                        },
                        "outcome": {"state": "skipped"},
                    }
                ),
            ),
        )
    calls = []

    async def observe(path, data):
        calls.append(data["operation"])
        assert data["operation"] == "observe"
        return {
            "target_id": "-777777",
            "title": "Mixed discussion",
            "kind": "discussion",
            "description": "General chat",
            "joined": False,
            "can_reply": True,
            "history": [{"mid": "200", "body": "General discussion", "own": False}],
        }

    async def admission(output, prompt, **kwargs):
        assert output is community_runtime.CommunityAdmission
        assert prompt["history_search_attempted"] is False
        if evidence_matches:
            assert prompt["verified_public_source"]["url"] == source
            assert (
                "Telegram Group (Chinese)"
                in prompt["verified_public_source"]["excerpt"]
            )
        else:
            assert prompt["verified_public_source"] is None
        return output(
            community_relevant=False,
            history_search_query="翻译" if evidence_matches else None,
            reason="Search actual accessible history before deciding membership.",
        )

    monkeypatch.setattr(model, "run_model", admission)
    result = await community_runtime.community_cycle(aid, observe)
    assert result["reason"] == (
        "community_history_search" if evidence_matches else "community_cycle"
    )
    assert calls == ["observe"]
    with tx(aid) as c:
        latest = one(
            c,
            "SELECT id,input,result FROM lead.tasks WHERE type='community.engage' ORDER BY created_at DESC LIMIT 1",
        )
        assert latest["id"] != previous["id"]
        assert latest["input"]["source_history_search_version"] == "1"
        assert latest["input"].get("history_query") == (
            "翻译" if evidence_matches else None
        )
        assert not latest["input"].get("reply")
        assert not latest["result"].get("join_submitted")
