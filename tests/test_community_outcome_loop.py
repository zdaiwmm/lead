from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from lead_app import community_health, community_opportunities, community_sources
from lead_app.community_growth import GrowthConfig
from lead_app.community_participation import (
    FAQMatch,
    ParticipationPolicy,
    business_outcome,
    contextual_answer,
    grounded_answer,
    quota_due,
    rule_permissions,
)
from lead_app.config import database_kwargs
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


@pytest.mark.parametrize(
    "text,reply,promotion",
    [
        ("No advertisements. Language exchange welcome.", "eligible", "denied"),
        ("禁止广告，欢迎正常交流", "eligible", "denied"),
        ("No bots or automated messages.", "denied", "denied"),
        ("Bots are not allowed.", "denied", "denied"),
        ("禁止机器人", "denied", "denied"),
        ("No ads and no bots.", "denied", "denied"),
        ("Pas de publicité", "eligible", "denied"),
        ("Welcome to our community.", "eligible", "unknown"),
    ],
)
def test_permissions_are_per_action(text, reply, promotion):
    result = rule_permissions(text, complete=True)
    assert result["reply"] == reply
    assert result["promotion"] == promotion
    assert rule_permissions("Welcome", complete=False)["join"] == "unknown"


def community(body="When does the library open?", **overrides):
    return {
        "description": "Q: When does the library open?\nA: The library opens at nine.",
        "history": [{"mid": "41", "body": body, "own": False}],
        **overrides,
    }


def test_expanded_faq_is_bound_to_unanswered_evidence():
    observed = community("Hello, when does the library open, please?")
    assert grounded_answer(observed, "exact_faq") is None
    assert (
        grounded_answer(observed, "grounded_faq")["body"]
        == "The library opens at nine."
    )
    observed["history"].append(
        {"mid": "42", "body": "An unrelated update", "own": False}
    )
    assert grounded_answer(observed, "grounded_faq")["reply_to_mid"] == "41"
    observed["history"].append({"mid": "43", "body": "At nine", "reply_parent": "41"})
    assert grounded_answer(observed, "grounded_faq") is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"rules_unresolved": True},
        {"description": "Q: When does the library open?\nA: Visit https://example.com"},
        {
            "description": "Q: When does the library open?\nA: At nine.\nQ: When does the library open?\nA: At ten."
        },
        {
            "history": [
                {"mid": "41", "body": "When does the library open?", "own": True}
            ]
        },
    ],
)
def test_faq_rejects_missing_or_conflicting_evidence(overrides):
    assert grounded_answer(community(**overrides), "grounded_faq") is None


@pytest.mark.asyncio
async def test_semantic_matching_requires_grant_and_returns_only_source_answer():
    calls = []

    async def invoke(schema, payload, **kwargs):
        calls.append(payload)
        return FAQMatch(question_mid="41", faq_index=0, reason="Same question")

    observed = community("What time can I start using the library?")
    assert (
        await contextual_answer(
            observed, mode="semantic_faq", reasoning_authorized=False, invoke=invoke
        )
        is None
    )
    assert calls == []
    result = await contextual_answer(
        observed, mode="semantic_faq", reasoning_authorized=True, invoke=invoke
    )
    assert result["body"] == "The library opens at nine." and len(calls) == 1

    async def invented(*args, **kwargs):
        return FAQMatch(question_mid="999", faq_index=0, reason="invalid anchor")

    assert (
        await contextual_answer(
            observed, mode="semantic_faq", reasoning_authorized=True, invoke=invented
        )
        is None
    )


def test_join_quota_does_not_consume_opt_in_reply_quota():
    now = datetime.now(timezone.utc)
    attempts = [("join", now - timedelta(minutes=1))]
    assert quota_due({}, "reply", attempts, now)
    split = ParticipationPolicy(
        quota_mode="per_action", joins_per_24h=1, replies_per_24h=3
    )
    assert quota_due(split, "join", attempts, now)
    assert quota_due(split, "reply", attempts, now) is None
    attempts.append(("reply", now - timedelta(minutes=2)))
    assert quota_due(split, "reply", attempts, now)
    assert quota_due(split, "reply", attempts, now + timedelta(minutes=31)) is None
    assert (
        quota_due({"joins_per_24h": 0, "quota_mode": "per_action"}, "join", [], now)[
            "next_eligible_at"
        ]
        is None
    )


@pytest.mark.parametrize(
    "result,expected",
    [
        ({"outcome": {"state": "skipped"}}, "skipped"),
        ({"outcome": {"state": "blocked"}}, "blocked"),
        (
            {"reply_state": "unknown", "outcome": {"state": "skipped"}},
            "pending_receipt",
        ),
        ({"reply_state": "confirmed"}, "no_change"),
        (
            {"reply_state": "confirmed", "reply_external_id": "peer:41"},
            "message_confirmed",
        ),
        ({"join_state": "confirmed"}, "no_change"),
        ({"join_state": "confirmed", "join_submitted": True}, "membership_confirmed"),
        (
            {"join_state": "confirmed", "join_submitted": True, "reply_state": "unknown"},
            "pending_receipt",
        ),
    ],
)
def test_finished_task_is_not_automatically_a_business_result(result, expected):
    assert business_outcome({"state": "succeeded", "result": result}) == expected


@pytest.fixture
def queue(monkeypatch):
    conn = psycopg.connect(**database_kwargs(owner=True), row_factory=dict_row)

    class Scoped:
        def execute(self, sql, args=()):
            return conn.execute(sql.replace("lead.", "pg_temp."), args)

    c = Scoped()
    c.execute(
        "CREATE TEMP TABLE agents(id text,control_version int,config_revision int,desired_state text)"
    )
    c.execute(
        "CREATE TEMP TABLE tasks(id text,agent_id text,type text,state text,input jsonb,result jsonb,created_at timestamptz,completed_at timestamptz)"
    )
    c.execute(
        "CREATE TEMP TABLE interaction_cards(id text,agent_id text,type text,state text,expires_at timestamptz,payload jsonb)"
    )
    c.execute(
        "CREATE TEMP TABLE social_accounts(id text,agent_id text,external_account_id text,platform text,state text,automation_enabled boolean)"
    )
    c.execute(
        "CREATE TEMP TABLE memory_entries(id uuid PRIMARY KEY,tenant_id uuid,agent_id text,kind text,body jsonb,source_refs jsonb,confirmation text)"
    )
    c.execute("INSERT INTO agents VALUES('agent',32,1,'running')")
    c.execute(
        "INSERT INTO social_accounts VALUES('account','agent','bound','telegram','connected',true)"
    )
    item = {"telegram_url": "https://t.me/fixture", "discovery_task_id": "discovery"}
    payload = {
        "account_id": "account",
        "external_account_id": "bound",
        "control_version": 28,
        "config_revision": 1,
        "candidate": item,
        "delegation_id": "grant",
    }
    grant = {
        "purpose": "bounded_candidate_participation",
        "account_id": "account",
        "external_account_id": "bound",
        "allowed_actions": ["join_public_group", "read_recent_history"],
        "discovery_task": "discovery",
    }
    c.execute(
        "INSERT INTO interaction_cards VALUES('grant','agent','manage_community','accepted',now()+interval '1 day',%s)",
        (Jsonb(grant),),
    )
    c.execute(
        "INSERT INTO tasks VALUES('discovery','agent','community.discover','succeeded',%s,%s,now()-interval '3 days',now()-interval '2 days')",
        (Jsonb(payload), Jsonb({"candidates": [item]})),
    )
    c.execute(
        "INSERT INTO tasks VALUES('read','agent','community.engage','succeeded',%s,%s,now()-interval '1 day',now()-interval '1 hour')",
        (
            Jsonb(payload),
            Jsonb(
                {
                    "target_id": "-12",
                    "outcome": {"reason": "仍有未读完整的置顶内容或外部群规，暂缓"},
                }
            ),
        ),
    )
    monkeypatch.setattr(community_health, "blocked", lambda *a, **kw: False)
    yield c
    conn.rollback()
    conn.close()


def review(c):
    return community_opportunities.next_rule_review(
        c, "agent", {"id": "account", "external_account_id": "bound"}, 1
    )


def test_control_change_revalidates_read_without_mutating_original(queue):
    result = review(queue)
    assert (
        result["rule_review_parent"] == "read"
        and result["rule_review_control_version"] == 28
    )
    assert (
        queue.execute(
            "SELECT input->>'control_version' v FROM tasks WHERE id='read'"
        ).fetchone()["v"]
        == "28"
    )


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE agents SET desired_state='paused'",
        "UPDATE agents SET config_revision=2",
        "UPDATE social_accounts SET external_account_id='replacement'",
        "UPDATE social_accounts SET automation_enabled=false",
        "UPDATE interaction_cards SET state='revoked'",
        "UPDATE interaction_cards SET expires_at=now()-interval '1 minute'",
        "UPDATE interaction_cards SET payload=jsonb_set(payload,'{allowed_actions}','[]')",
        "UPDATE tasks SET state='running' WHERE id='read'",
        "UPDATE tasks SET result=result || '{\"join_state\":\"unknown\"}' WHERE id='read'",
        "UPDATE tasks SET result=result || '{\"join_submitted\":true}' WHERE id='read'",
        "UPDATE tasks SET input=input || '{\"reply\":{}}' WHERE id='read'",
        "UPDATE tasks SET state='failed' WHERE id='discovery'",
    ],
)
def test_revalidation_preserves_all_current_authority_and_receipt_gates(queue, change):
    queue.execute(change)
    assert review(queue) is None


def test_read_review_backoff_and_retry_cap(queue):
    queue.execute(
        "UPDATE tasks SET completed_at=now()-interval '5 minutes' WHERE id='read'"
    )
    assert review(queue) is None
    queue.execute(
        "UPDATE tasks SET completed_at=now()-interval '31 minutes' WHERE id='read'"
    )
    assert review(queue)
    for index in range(3):
        queue.execute(
            """INSERT INTO tasks SELECT %s,agent_id,type,state,
            jsonb_set(input,'{candidate,rule_review_parent}','\"read\"'),result,
            now()-interval '25 hours'+%s*interval '1 minute',now()-interval '25 hours'
            FROM tasks WHERE id='read'""",
            (f"retry{index}", index),
        )
    assert review(queue) is None


def test_backlog_prioritizes_review_but_leaves_exploration_window(queue):
    agent = {"id": "agent", "config_revision": 1, "desired_state": "running"}
    config = GrowthConfig(enabled=True, mode="contextual_outreach")
    now = datetime(2026, 11, 11, 12, 5, tzinfo=timezone.utc)
    assert (
        community_opportunities.discovery_pressure(queue, agent, config, now)["reason"]
        == "rule_review_ready"
    )
    assert (
        community_opportunities.discovery_pressure(
            queue, agent, config, now.replace(minute=15)
        )
        is None
    )
    queue.execute("UPDATE tasks SET result='{}' WHERE id='read'")
    queue.execute("DELETE FROM tasks WHERE id='read'")
    items = [{"telegram_url": f"https://t.me/fixture{i}"} for i in range(5)]
    queue.execute(
        "UPDATE tasks SET result=%s WHERE id='discovery'",
        (Jsonb({"candidates": items}),),
    )
    assert (
        community_opportunities.discovery_pressure(queue, agent, config, now)["ready"]
        == 5
    )


def test_source_failure_cools_across_campaigns_without_inventing_zero_yield(
    queue, monkeypatch
):
    monkeypatch.setattr(
        community_sources,
        "current_tenant",
        lambda: "00000000-0000-0000-0000-000000000001",
    )
    for campaign in ("a", "a", "b", "c"):
        community_sources.remember_failure(
            queue, "agent", 1, "https://example.com/search", 1, "read_failed", campaign
        )
    row = queue.execute("SELECT body FROM memory_entries").fetchone()["body"]
    assert row["read_failures"] == 3 and row["qualified"] is None
    assert not row.get("zero_rounds")
    assert community_sources.cooling(queue, "agent", 1, "https://example.com/search", 1)


def test_live_board_reports_scope_and_non_outcomes():
    from lead_app import domain
    from lead_app.db import j, tx, uid
    from lead_app.scope import current_tenant
    from lead_app.telegram_group_board import execution, participation_status

    aid = domain.create_agent()
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        for result in (
            {"outcome": {"state": "skipped", "reason": "no opportunity"}},
            {"outcome": {"state": "blocked", "reason": "permissions"}},
            {"outcome": {"state": "deferred", "reason": "read later"}},
            {},
        ):
            c.execute(
                """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at)
                VALUES(%s,%s,'community.engage','succeeded',%s,'{}',%s,now(),%s)""",
                (current_tenant(), aid, uid(), j(result), now),
            )
        view = execution(c, aid, now - timedelta(hours=1), now)
    assert view["outcomes"] == {
        "skipped": 1,
        "blocked": 1,
        "deferred": 1,
        "no_change": 1,
    }
    agent = {"onboarding_state": {"telegram_executor": {"mode": "production"}}}
    config = GrowthConfig(allow_promotion=False, allow_proactive_post=False)
    assert not participation_status(agent, config)["policy_reported"]
    agent["onboarding_state"]["telegram_executor"]["participation_policy"] = (
        ParticipationPolicy(quota_mode="per_action").model_dump()
    )
    status = participation_status(agent, config)
    assert status["policy_reported"] and "产品推广未开启" in status["scope"]
    agent["onboarding_state"]["telegram_executor"]["participation_policy"] = {
        "quota_mode": "per_action"
    }
    assert not participation_status(agent, config)["policy_reported"]
