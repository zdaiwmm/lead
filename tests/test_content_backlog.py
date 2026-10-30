"""The product builds real draft inventory without a live publishing target."""

import asyncio
import os
from datetime import timedelta

import pytest
from lead_app import activities, content_backlog, domain
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant

FACT = "Seka supports long-form translation with glossary and style settings."


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url,facts) VALUES(%s,%s,1,%s,'https://seka.one','{}')",
            (current_tenant(), aid, FACT),
        )
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        c.execute(
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled) VALUES(%s,%s,%s,'telegram','connected',%s,true)",
            (current_tenant(), aid, profile["id"], uid()),
        )
    return aid


@pytest.fixture
def author(monkeypatch):
    calls = []

    async def generate(schema, payload, **kwargs):
        assert kwargs["purpose"] == "planning" and kwargs["short_request"]
        calls.append(payload)
        topic = payload["assignment"]["topic"]
        return content_backlog.PreparedContent(
            title=topic, body=topic + ": " + FACT, fact_quotes=[FACT]
        )

    monkeypatch.setattr(content_backlog, "run_model", generate)
    return calls


@pytest.mark.asyncio
async def test_prepares_three_distinct_drafts_without_posting_and_stops_at_reserve(
    agent, author
):
    for _ in range(3):
        assert (await content_backlog.prepare_one(agent))["state"] == "ready"
    assert await content_backlog.prepare_one(agent) is None
    assert len(author) == 3
    with tx(agent) as c:
        a = one(c, "SELECT * FROM lead.agents")
        business = one(c, "SELECT * FROM lead.business_profiles")
        drafts = content_backlog.ready(c, business["id"], a["config_revision"])
        assert len({d["result"]["body"] for d in drafts}) == 3
        assert content_backlog.next_due(c, a) is None
        assert not all_rows(c, "SELECT id FROM lead.actions")
        assert not all_rows(c, "SELECT id FROM lead.authorization_grants")
        content_backlog.scheduled(c, drafts[0]["id"], uid())
        assert len(content_backlog.ready(c, business["id"], a["config_revision"])) == 2
    assert len(domain.agent_detail(agent)["operating_workflow"]["ready_content"]) == 2


@pytest.mark.asyncio
async def test_fact_source_failure_retries_same_task_after_backoff(
    agent, monkeypatch, author
):
    original = content_backlog.run_model

    async def ungrounded(*args, **kwargs):
        return content_backlog.PreparedContent(
            title="Unsupported claim",
            body="An unsupported feature with no source material.",
            fact_quotes=["Unconfirmed exact price"],
        )

    monkeypatch.setattr(content_backlog, "run_model", ungrounded)
    assert (await content_backlog.prepare_one(agent))["state"] == "retry_scheduled"
    assert await content_backlog.prepare_one(agent) is None
    with tx(agent) as c:
        task = one(c, "SELECT id,result FROM lead.tasks WHERE type='content.prepare'")
        assert "Unconfirmed" not in str(task["result"])
        c.execute(
            "UPDATE lead.tasks SET result=result || %s WHERE id=%s",
            (j({"retry_at": "2000-01-01T00:00:00+00:00"}), task["id"]),
        )
    monkeypatch.setattr(content_backlog, "run_model", original)
    result = await content_backlog.prepare_one(agent)
    assert result["state"] == "ready" and result["task_id"] == str(task["id"])
    with tx(agent) as c:
        assert (
            one(c, "SELECT count(*) AS n FROM lead.tasks WHERE type='content.prepare'")[
                "n"
            ]
            == 1
        )


@pytest.mark.asyncio
async def test_pause_during_generation_discards_result(agent, monkeypatch):
    async def pause(*args, **kwargs):
        with tx(agent) as c:
            c.execute(
                "UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (agent,)
            )
        return content_backlog.PreparedContent(
            title="Draft", body=FACT, fact_quotes=[FACT]
        )

    monkeypatch.setattr(content_backlog, "run_model", pause)
    assert (await content_backlog.prepare_one(agent))["state"] == "canceled"
    assert await content_backlog.prepare_one(agent) is None
    assert not domain.agent_detail(agent)["operating_workflow"]["ready_content"]


@pytest.mark.asyncio
async def test_failed_channel_cooldown_does_not_block_another_channel(agent, author):
    from datetime import datetime, timezone

    prepared = await content_backlog.prepare_one(agent)
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.tasks SET state='failed',result=%s WHERE id=%s",
            (
                j(
                    {
                        "attempts": 1,
                        "retry_at": (
                            datetime.now(timezone.utc) + timedelta(minutes=10)
                        ).isoformat(),
                    }
                ),
                prepared["task_id"],
            ),
        )
        profile = one(c, "SELECT id FROM lead.browser_profiles")
        c.execute(
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled) VALUES(%s,%s,%s,'reddit','connected',%s,true)",
            (current_tenant(), agent, profile["id"], uid()),
        )
        a = one(c, "SELECT * FROM lead.agents")
        assert content_backlog.next_due(c, a) <= datetime.now(timezone.utc)
    result = await content_backlog.prepare_one(agent)
    assert result["state"] == "ready"
    assert author[-1]["assignment"]["platform"] == "reddit"


@pytest.mark.asyncio
async def test_daily_new_task_cap_allows_bounded_retry_of_original_task(
    agent, author, monkeypatch
):
    from datetime import datetime, timezone

    prepared = await content_backlog.prepare_one(agent)
    monkeypatch.setattr(content_backlog, "DAILY_TASK_LIMIT", 1)
    with tx(agent) as c:
        c.execute(
            "UPDATE lead.tasks SET state='failed',completed_at=now()-interval '1 hour',result=%s WHERE id=%s",
            (
                j({"attempts": 1, "retry_at": "2000-01-01T00:00:00+00:00"}),
                prepared["task_id"],
            ),
        )
        a = one(c, "SELECT * FROM lead.agents")
        assert content_backlog.next_due(c, a) <= datetime.now(timezone.utc)
    result = await content_backlog.prepare_one(agent)
    assert result["state"] == "ready" and result["task_id"] == prepared["task_id"]
    assert await content_backlog.prepare_one(agent) is None
    with tx(agent) as c:
        a = one(c, "SELECT * FROM lead.agents")
        assert content_backlog.next_due(c, a) > datetime.now(timezone.utc) + timedelta(
            hours=23
        )
        assert (
            one(c, "SELECT count(*) AS n FROM lead.tasks WHERE type='content.prepare'")[
                "n"
            ]
            == 1
        )


@pytest.mark.asyncio
async def test_product_cycle_prepares_content_and_schedules_next_step_without_research(
    agent, author
):
    result = await activities.autonomous_cycle({"agent_id": agent, "cycle_id": uid()})
    assert 5 <= result["delay"] <= 30
    detail = domain.agent_detail(agent)
    assert len(detail["operating_workflow"]["ready_content"]) == 1
    assert detail["operating_workflow"]["next_check_at"]
    assert detail["onboarding_state"]["operating_schedule"]["next_content_at"]


@pytest.mark.asyncio
async def test_verified_channel_schedules_prepared_content_once(
    agent, author, monkeypatch
):
    import psycopg
    from lead_app.adapters.catalog import SPECS
    from lead_app.config import database_kwargs
    from lead_app.model import ContentDecision

    prepared = await content_backlog.prepare_one(agent)
    target = uid()
    with tx(agent) as c:
        account = one(c, "SELECT id FROM lead.social_accounts")
        c.execute(
            "INSERT INTO lead.account_targets(id,tenant_id,agent_id,account_id,target_type,external_target_id,display_name,canonical_url,verified_permissions,state) VALUES(%s,%s,%s,%s,'channel','-987654','Owned fixture','https://web.telegram.org/k/#-987654',%s,'active')",
            (
                target,
                current_tenant(),
                agent,
                account["id"],
                j(
                    {
                        "adapter_version": SPECS["telegram"].version,
                        "publishing_verified_formats": ["text"],
                    }
                ),
            ),
        )
        c.execute(
            "INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,granted_by) VALUES(%s,%s,%s,1,%s,'fixture')",
            (current_tenant(), agent, target, j(["publish"])),
        )
    with psycopg.connect(**database_kwargs(True)) as owner:
        owner.execute(
            "UPDATE lead.platform_capabilities SET technical_state='verified',permission_state='approved' WHERE platform='telegram' AND action_type='publish'"
        )

    async def publish(*args, **kwargs):
        return ContentDecision(
            action="publish",
            body="Fallback text should be replaced.",
            target_id=target,
            rationale="Use the prepared content.",
        )

    monkeypatch.setattr(activities, "run_model", publish)
    monkeypatch.setattr(activities, "emit", lambda *a, **k: None)
    result = await activities.autonomous_cycle({"agent_id": agent, "cycle_id": uid()})
    assert "reason" not in result, result
    with tx(agent) as c:
        task = one(c, "SELECT id,input FROM lead.tasks WHERE type='telegram.publish'")
        draft = one(
            c, "SELECT result FROM lead.tasks WHERE id=%s", (prepared["task_id"],)
        )
        assert task["input"]["prepared_content_id"] == prepared["task_id"]
        assert task["input"]["body"].startswith(draft["result"]["body"])
        assert draft["result"]["status"] == "scheduled"
        assert draft["result"]["publication_task_id"] == str(task["id"])
        assert not all_rows(c, "SELECT id FROM lead.actions")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1",
    reason="isolated Temporal content workflow",
)
async def test_product_timer_fills_content_reserve_without_external_wakeup(
    agent, author, monkeypatch
):
    from lead_app.temporal_client import connect
    from lead_app.workflows import SupervisorWorkflow
    from temporalio import activity
    from temporalio.worker import Replayer, Worker

    monkeypatch.setattr(content_backlog, "PREPARE_INTERVAL_SECONDS", 1)
    calls = []

    @activity.defn(name="autonomous_cycle")
    async def cycle(args):
        result = await activities.autonomous_cycle(args)
        calls.append(result)
        return {"archived": True, "delay": 60} if len(calls) == 3 else result

    client = await connect()
    queue = "content-backlog-test-" + uid()
    async with Worker(
        client, task_queue=queue, workflows=[SupervisorWorkflow], activities=[cycle]
    ):
        handle = await client.start_workflow(
            SupervisorWorkflow.run,
            agent,
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=25),
        )
        try:
            await asyncio.wait_for(handle.result(), timeout=20)
            assert len(author) == 3
            await Replayer(workflows=[SupervisorWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
        finally:
            if len(calls) < 3:
                await handle.terminate("isolated test cleanup")
    assert len(domain.agent_detail(agent)["operating_workflow"]["ready_content"]) == 3
