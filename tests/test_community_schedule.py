"""Persistent cadence and real timer wakeups, using isolated test Agent IDs."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from lead_app import (
    activities,
    community_discovery,
    community_growth,
    community_schedule,
    domain,
)
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    monkeypatch.setattr(community_growth, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    community_growth.set_strategy(aid, {"enabled": True, "mode": "contextual_outreach"})
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET desired_state='running',heartbeat_at=now()-interval '8 hours',health_state='queued' WHERE id=%s",
            (aid,),
        )
    return aid


def discovery(c, aid, completed, created=None):
    return one(
        c,
        "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,created_at,completed_at) VALUES(%s,%s,'community.discover','succeeded',%s,%s,%s,now(),%s,%s) RETURNING id",
        (
            current_tenant(),
            aid,
            "fixture:" + uid(),
            j({"checkpoint_version": "2"}),
            j(
                {
                    "adapter_revision": community_discovery.ADAPTER_REVISION,
                    "link_extractor_version": 5,
                    "native_selector_version": 8,
                    "native_cursor": 1000,
                    "cursor": 0,
                    "queue": [],
                    "candidates": [],
                }
            ),
            created or completed,
            completed,
        ),
    )


def test_channel_wait_cannot_delay_review_and_heartbeat(agent):
    now = datetime.now(timezone.utc)
    with tx(agent) as c:
        discovery(c, agent, now - timedelta(hours=3))
        row = one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at) VALUES(%s,%s,'community.engage','succeeded',%s,%s,%s,now(),%s) RETURNING id",
            (
                current_tenant(),
                agent,
                "fixture:" + uid(),
                j(
                    {
                        "candidate": {"telegram_url": "https://t.me/readers_fixture"},
                        "external_account_id": "fixture",
                    }
                ),
                j(
                    {
                        "join_submitted": True,
                        "join_state": "confirmed",
                        "history_observation": {"joined": True},
                    }
                ),
                now - timedelta(hours=1) + timedelta(seconds=20),
            ),
        )
    result = community_schedule.completed(
        agent, {"delay": 30434, "reason": "scheduled"}, now
    )
    assert result["delay"] == 20
    with tx(agent) as c:
        a = one(c, "SELECT * FROM lead.agents WHERE id=%s", (agent,))
        assert a["health_state"] == "ready" and a["heartbeat_at"] > now - timedelta(
            seconds=3
        )
        schedule = a["onboarding_state"]["community_schedule"]
        assert datetime.fromisoformat(schedule["next_discovery_at"]) == now + timedelta(
            hours=21
        )
        c.execute(
            "UPDATE lead.tasks SET result=jsonb_set(result,'{history_observation,joined}','false') WHERE id=%s",
            (row["id"],),
        )
    assert (
        community_schedule.completed(
            agent, {"delay": 30434, "reason": "scheduled"}, now
        )["delay"]
        == 300
    )
    domain.set_control(agent, "pause")
    assert (
        community_schedule.completed(
            agent, {"delay": 30434, "reason": "scheduled"}, now
        )["delay"]
        == 30434
    )


@pytest.mark.parametrize("join_state", ["pending_approval", "unknown"])
def test_pending_join_has_own_deadline_without_blocking_discovery(agent, join_state):
    now = datetime.now(timezone.utc)
    with tx(agent) as c:
        discovery(c, agent, now)
        one(
            c,
            "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at) VALUES(%s,%s,'community.engage','succeeded',%s,'{}',%s,now(),now()) RETURNING id",
            (
                current_tenant(),
                agent,
                "fixture:" + uid(),
                j(
                    {
                        "join_state": join_state,
                        "join_mode": "approval_request",
                        "join_checked_at": (
                            now - timedelta(minutes=14, seconds=40)
                        ).isoformat(),
                    }
                ),
            ),
        )
    result = community_schedule.completed(
        agent, {"delay": 30434, "reason": "scheduled"}, now
    )
    assert result["delay"] == 20
    with tx(agent) as c:
        schedule = one(c, "SELECT onboarding_state FROM lead.agents")[
            "onboarding_state"
        ]["community_schedule"]
        assert datetime.fromisoformat(
            schedule["next_join_approval_check_at"]
        ) == now + timedelta(seconds=20)


@pytest.mark.asyncio
async def test_discovery_repeats_daily_without_new_research_request(agent, monkeypatch):
    from lead_app.community_search import QUERIES

    class Browser:
        def __init__(self, *a, **k):
            pass

        async def command(self, *a, **k):
            return {"error": "isolated fixture"}

        async def finish(self, *a, **k):
            return []

    monkeypatch.setattr(community_discovery, "BrowserTools", Browser)
    with tx(agent) as c:
        a = one(c, "SELECT * FROM lead.agents WHERE id=%s", (agent,))
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs) VALUES(%s,%s,1,'EPUB reading','{}','[]')",
            (current_tenant(), agent),
        )
        now = datetime.now(timezone.utc)
        first = discovery(c, agent, now, now - timedelta(days=2))
        c.execute(
            "UPDATE lead.tasks SET input=input || %s,result=result || %s WHERE id=%s",
            (
                j(
                    {
                        "control_version": a["control_version"],
                        "config_revision": a["config_revision"],
                    }
                ),
                j({"native_cursor": len(QUERIES)}),
                first["id"],
            ),
        )
    assert await community_discovery.advance(agent) is None
    with tx(agent) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.tasks")["n"] == 1
        c.execute(
            "UPDATE lead.tasks SET completed_at=now()-interval '24 hours 1 second' WHERE id=%s",
            (first["id"],),
        )
    assert (await community_discovery.advance(agent))[
        "reason"
    ] == "community_discovery_progress"
    with tx(agent) as c:
        rows = c.execute(
            "SELECT id,input FROM lead.tasks WHERE type='community.discover' ORDER BY created_at"
        ).fetchall()
        assert len(rows) == 2 and rows[-1]["id"] != first["id"]
        assert rows[-1]["input"]["research_request"] == ""


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1",
    reason="local Temporal timer verification",
)
async def test_product_supervisor_wakes_without_chat_or_new_signal(agent, monkeypatch):
    from lead_app.temporal_client import connect
    from lead_app.workflows import SupervisorWorkflow
    from temporalio import activity
    from temporalio.worker import Replayer, Worker

    monkeypatch.setattr(community_schedule, "HEARTBEAT_SECONDS", 1)
    calls = []

    async def channel_wait(args):
        return {"delay": 30434, "reason": "scheduled"}

    monkeypatch.setattr(activities, "_autonomous_cycle", channel_wait)
    with tx(agent) as c:
        discovery(c, agent, datetime.now(timezone.utc))

    @activity.defn(name="autonomous_cycle")
    async def cycle(args: dict):
        calls.append(datetime.now(timezone.utc))
        if len(calls) == 3:
            return {"archived": True, "delay": 300}
        return await activities.autonomous_cycle(args)

    client = await connect()
    queue = "community-cadence-test-" + uid()
    async with Worker(
        client, task_queue=queue, workflows=[SupervisorWorkflow], activities=[cycle]
    ):
        handle = await client.start_workflow(
            SupervisorWorkflow.run,
            agent,
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=20),
        )
        try:
            await asyncio.wait_for(handle.result(), timeout=15)
            assert len(calls) == 3 and (calls[1] - calls[0]).total_seconds() >= 0.8
            await Replayer(workflows=[SupervisorWorkflow]).replay_workflow(
                await handle.fetch_history()
            )
        finally:
            if len(calls) < 3:
                await handle.terminate("isolated cadence verification cleanup")


@pytest.mark.parametrize(
    "reason,delay,expected", [("work_hours", 86400, 300), ("concurrency_limit", 10, 10)]
)
def test_deferred_admission_refreshes_heartbeat_without_ignoring_gate(
    agent, reason, delay, expected
):
    now = datetime.now(timezone.utc)
    result = community_schedule.completed(
        agent, {"delay": delay, "reason": reason}, now, respect_delay=True
    )
    assert result["reason"] == reason
    assert result["delay"] == expected
    with tx(agent) as c:
        row = one(
            c,
            "SELECT heartbeat_at,onboarding_state FROM lead.agents WHERE id=%s",
            (agent,),
        )
        assert row["heartbeat_at"] >= now
        assert (
            row["onboarding_state"]["community_schedule"]["deferred_reason"] == reason
        )
        assert datetime.fromisoformat(
            row["onboarding_state"]["community_schedule"]["next_check_at"]
        ) == now + timedelta(seconds=expected)
