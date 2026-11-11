"""Durable cadence waits on the caller-provided isolated Temporal service."""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from lead_app.db import uid
from lead_app.workflows import ChannelDeliveryWorkflow
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.worker import Replayer, Worker


async def client():
    assert os.environ.get("LEAD_TEST_CHANNEL_TEMPORAL"), (
        "dedicated Temporal address required"
    )
    return await Client.connect(os.environ["LEAD_TEST_CHANNEL_TEMPORAL"])


def waiting(state="waiting_event", *, deadline=None):
    return {
        "delay": 0.1,
        "execution_cadence": {
            "state": state,
            "constraints": [],
            "resume_events": ["source.changed"],
            "next_at": deadline,
            "execution_cadence_protocol": 1,
            "policy_version": 1,
        },
    }


@pytest.mark.asyncio
async def test_event_wait_does_not_poll_research():
    c = await client()
    calls = []

    @activity.defn(name="channel_research_tick")
    async def tick(args: dict):
        calls.append(args["tick"])
        return waiting()

    queue = uid()
    async with Worker(
        c, task_queue=queue, workflows=[ChannelDeliveryWorkflow], activities=[tick]
    ):
        h = await c.start_workflow(
            ChannelDeliveryWorkflow.run,
            {"lane": "research"},
            id=uid(),
            task_queue=queue,
        )
        try:
            while not calls:
                await asyncio.sleep(0.02)
            await asyncio.sleep(1.2)
            assert len(calls) == 1
        finally:
            await h.cancel()


@pytest.mark.asyncio
async def test_signal_rechecks_all_constraints_and_duplicate_signal_has_one_operation():
    c = await client()
    calls, writes = [], []
    ready = False

    @activity.defn(name="channel_research_tick")
    async def tick(args: dict):
        calls.append(args["tick"])
        if ready:
            writes.append(args["tick"])
            return {"stop": True}
        return waiting()

    queue = uid()
    async with Worker(
        c, task_queue=queue, workflows=[ChannelDeliveryWorkflow], activities=[tick]
    ):
        h = await c.start_workflow(
            ChannelDeliveryWorkflow.run,
            {"lane": "research"},
            id=uid(),
            task_queue=queue,
        )
        while not calls:
            await asyncio.sleep(0.02)
        await h.signal("wake", 1)
        while len(calls) < 2:
            await asyncio.sleep(0.02)
        assert not writes
        ready = True
        await asyncio.gather(h.signal("wake", 1), h.signal("wake", 1))
        await asyncio.wait_for(h.result(), 8)
        assert len(writes) == 1
        await Replayer(workflows=[ChannelDeliveryWorkflow]).replay_workflow(
            await h.fetch_history()
        )


@pytest.mark.asyncio
async def test_restart_keeps_tick_and_deadline():
    c = await client()
    calls = []
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=1.5)).isoformat()

    @activity.defn(name="channel_research_tick")
    async def tick(args: dict):
        calls.append(args["tick"])
        return (
            waiting("waiting_time", deadline=deadline)
            if len(calls) == 1
            else {"stop": True}
        )

    queue = uid()
    async with Worker(
        c, task_queue=queue, workflows=[ChannelDeliveryWorkflow], activities=[tick]
    ):
        h = await c.start_workflow(
            ChannelDeliveryWorkflow.run,
            {"lane": "research"},
            id=uid(),
            task_queue=queue,
        )
        while not calls:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
    assert len(calls) == 1
    async with Worker(
        c, task_queue=queue, workflows=[ChannelDeliveryWorkflow], activities=[tick]
    ):
        await asyncio.wait_for(h.result(), 8)
        assert len(calls) == 2 and calls[0] != calls[1]
        assert datetime.now(timezone.utc) >= datetime.fromisoformat(deadline)
        await Replayer(workflows=[ChannelDeliveryWorkflow]).replay_workflow(
            await h.fetch_history()
        )


@workflow.defn(name="ChannelDeliveryWorkflow", sandboxed=False)
class LegacyChannelWorkflow:
    @workflow.run
    async def run(self, args: dict, pending_tick: str | None = None):
        for _ in range(100):
            pending_tick = pending_tick or str(workflow.uuid4())
            result = await workflow.execute_activity(
                "channel_research_tick",
                {**args, "tick": pending_tick},
                start_to_close_timeout=timedelta(minutes=8),
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(seconds=30),
                    maximum_attempts=3,
                ),
            )
            if result.get("stop"):
                return
            pending_tick = None
            await workflow.sleep(max(1, float(result["delay"])))
        workflow.continue_as_new(args=[args, pending_tick])


@pytest.mark.asyncio
async def test_running_legacy_clock_adopts_attested_activity_wait_after_upgrade():
    c = await client()
    calls = []

    @activity.defn(name="channel_research_tick")
    async def tick(args: dict):
        calls.append(args["tick"])
        if len(calls)==1:
            return {"delay":1}
        return waiting() if len(calls)==2 else {"stop":True}

    queue=uid()
    async with Worker(c,task_queue=queue,workflows=[LegacyChannelWorkflow],activities=[tick]):
        h=await c.start_workflow(LegacyChannelWorkflow.run,{"lane":"research"},id=uid(),task_queue=queue)
        while not calls:
            await asyncio.sleep(.02)
        await asyncio.sleep(.1)
    async with Worker(c,task_queue=queue,workflows=[ChannelDeliveryWorkflow],activities=[tick]):
        try:
            while len(calls)<2:
                await asyncio.sleep(.02)
            await asyncio.sleep(1.3)
            assert len(calls)==2
            await h.signal("wake",1)
            await asyncio.wait_for(h.result(),8)
            await Replayer(workflows=[ChannelDeliveryWorkflow]).replay_workflow(await h.fetch_history())
        finally:
            await h.cancel()


@pytest.mark.asyncio
async def test_old_history_replays():
    c = await client()
    count = 0

    @activity.defn(name="channel_research_tick")
    async def tick(args: dict):
        nonlocal count
        count += 1
        return {"delay": 1} if count == 1 else {"stop": True}

    queue = uid()
    async with Worker(
        c, task_queue=queue, workflows=[LegacyChannelWorkflow], activities=[tick]
    ):
        h = await c.start_workflow(
            LegacyChannelWorkflow.run, {"lane": "research"}, id=uid(), task_queue=queue
        )
        await asyncio.wait_for(h.result(), 8)
    await Replayer(workflows=[ChannelDeliveryWorkflow]).replay_workflow(
        await h.fetch_history()
    )


def test_startup_reconciles_changed_source_config_once(monkeypatch):
    from lead_app import domain
    from lead_app.cadence_events import reconcile_source_config
    from lead_app.db import all_rows, tx

    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    with tx(aid) as db:
        db.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        from lead_app.channel_delivery import enroll

        agent = db.execute("SELECT * FROM lead.agents WHERE id=%s", (aid,)).fetchone()
        enroll(db, aid, agent, "reddit")
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "reddit")
    reconcile_source_config()
    reconcile_source_config()
    with tx(aid) as db:
        events = all_rows(
            db,
            "SELECT payload FROM lead.outbox_events WHERE event_type='channel.clock.wake'",
        )
        assert len(events) == 2
        assert {e["payload"]["policy_version"] for e in events} == {1}


def test_rule_gap_has_bounded_read_probe(monkeypatch):
    from lead_app.cadence_events import probe_rule_gap

    calls = []
    task = {
        "id": "task",
        "input": {"candidate": {"telegram_url": "https://t.me/example"}},
        "result": {},
    }
    now = datetime.now(timezone.utc)
    assert (
        probe_rule_gap(
            task,
            now=now,
            source_allowed=True,
            read_after=now + timedelta(seconds=1),
            reader=calls.append,
        )
        is None
    )
    probe_rule_gap(
        task, now=now, source_allowed=True, read_after=now, reader=calls.append
    )
    assert calls == [task]


def test_source_wait_probe_never_reads_external_page():
    from lead_app.cadence_events import probe_rule_gap

    def forbidden(task):
        raise AssertionError("external page opened without permission")

    result = probe_rule_gap(
        {"id": "task"},
        now=datetime.now(timezone.utc),
        source_allowed=False,
        read_after=None,
        reader=forbidden,
    )
    assert result["execution_cadence"]["state"] == "waiting_event"


def test_source_gap_is_persisted_and_supervisor_event_wakes_existing_clocks(
    monkeypatch,
):
    from lead_app import domain
    from lead_app.channel_delivery import enroll
    from lead_app.channel_research import select
    from lead_app.db import all_rows, emit, one, tx
    from lead_app.scope import current_tenant

    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", aid)
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    with tx(aid) as db:
        db.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        profile = one(db, "SELECT id FROM lead.browser_profiles")["id"]
        db.execute(
            "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled) VALUES(%s,%s,%s,'reddit',%s,'connected',true)",
            (current_tenant(), aid, profile, uid()),
        )
        enroll(db, aid, one(db, "SELECT * FROM lead.agents"), "reddit")
        clock = one(
            db,
            "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'",
        )
    args = {
        "agent_id": aid,
        "task_id": str(clock["id"]),
        "generation": clock["version"],
        "tick": uid(),
    }
    result, candidate = select(args)
    assert candidate is None and result["execution_cadence"]["state"] == "waiting_event"
    assert result["execution_cadence"]["next_at"] is None
    with tx(aid) as db:
        assert (
            one(db, "SELECT result FROM lead.tasks WHERE id=%s", (clock["id"],))[
                "result"
            ]["execution_cadence"]
            == result["execution_cadence"]
        )
        emit(db, aid, "supervisor.wake")
        assert (
            len(
                all_rows(
                    db,
                    "SELECT payload FROM lead.outbox_events WHERE event_type='channel.clock.wake'",
                )
            )
            == 2
        )


@pytest.mark.asyncio
async def test_clock_wake_only_signals_existing_generation(monkeypatch):
    from lead_app import worker
    from lead_app.scope import current_tenant

    aid, tid = uid(), uid()
    event = {
        "agent_id": aid,
        "id": uid(),
        "payload": {"task_id": tid, "generation": 4, "policy_version": 1},
        "event_type": "channel.clock.wake",
        "occurred_at": datetime.now(timezone.utc),
    }
    monkeypatch.setattr(worker, "outbox_rows", lambda: [event])
    monkeypatch.setattr(worker, "outbox_agent", lambda _: {"desired_state": "running"})
    monkeypatch.setattr(worker, "ack_outbox", lambda *a: None)

    def rejected(*a):
        raise AssertionError("wake failed")

    monkeypatch.setattr(worker, "defer_outbox", rejected)
    from lead_app import cadence_events

    monkeypatch.setattr(cadence_events, "reconcile_source_config", lambda: None)
    signals = []

    class ClientOnlySignals:
        def get_workflow_handle(self, wid):
            self.wid = wid
            return self

        async def signal(self, *args):
            signals.append((self.wid, *args))

        async def start_workflow(self, *a, **k):
            raise AssertionError("wake created workflow")

    await worker.deliver_workspace(ClientOnlySignals(), current_tenant(), False, False)
    assert signals == [(f"channel-clock:{tid}:4", "wake", 1)]
