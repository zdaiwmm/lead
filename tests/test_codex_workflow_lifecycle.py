"""Real Temporal histories plus the scoped DB; model transport is a fixture.

These tests neither contact a model nor send a business action. A single
Activity slot must remain usable while the original Codex request is pending.
"""
import asyncio
import copy
import os
from datetime import datetime, timedelta
from time import monotonic
from uuid import uuid4

import pytest
from lead_app import local_executor, model
from lead_app.config import STATE
from lead_app.db import j, one, tx
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import submit_work
from lead_app.local_executor_client import ActivityJobs
from pydantic import BaseModel
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

pytestmark = pytest.mark.skipif(
    not os.environ.get("LEAD_TEST_CHANNEL_TEMPORAL"),
    reason="requires an explicitly isolated Temporal service",
)


class LifecycleAnswer(BaseModel):
    text: str


async def client():
    assert os.environ.get("LEAD_GROWTH_TEST_ENV") == "1"
    address = os.environ["LEAD_TEST_CHANNEL_TEMPORAL"]
    host, port = address.rsplit(":", 1)
    assert host in {"127.0.0.1", "localhost"} and int(port) != 7233
    return await Client.connect(address)


async def eventually(predicate, seconds=15):
    deadline = monotonic()+seconds
    while not predicate():
        if monotonic() >= deadline:
            raise AssertionError("the original workflow did not reach the expected condition")
        await asyncio.sleep(.02)


async def stop_jobs(jobs):
    tasks = list(jobs.jobs.values())
    for task in tasks:
        if not task.done():
            task.cancel()  # Worker loss, not the business-cancellation marker.
    await asyncio.gather(*tasks, return_exceptions=True)


async def record_history(handle, name):
    history = await handle.fetch_history()
    (STATE/name).write_text(history.to_json())
    return history


def new_task(aid, purpose):
    with tx(aid) as connection:
        connection.execute("""UPDATE lead.agents SET desired_state='running',
            onboarding_state=onboarding_state||%s WHERE id=%s""",
            (j({"growth_runtime": {"decision": {"engine": "growth", "generation": 1}}}), aid))
        return submit_work(connection, aid, TaskSpec(kind="research", purpose=purpose,
            review_at=utcnow()+timedelta(hours=1)))


@pytest.mark.asyncio
async def test_growth_pending_releases_slot_and_worker_loss_rejoins_original_claim(growth_agent, monkeypatch):
    from lead_app import local_executor_client
    from lead_app.workflows import GrowthWorkWorkflow

    temporal = await client()
    aid = str(growth_agent)
    invitation = local_executor.create_pairing(aid)
    device = local_executor.pair(invitation["code"], "Workflow fixture", "linux", {"ready": True})
    original = new_task(aid, "original-native-request")
    probe = new_task(aid, "independent-slot-probe")
    polls, finishes, body_calls = [], [], []
    jobs = ActivityJobs()
    monkeypatch.setattr(local_executor_client, "activity_jobs", jobs)

    def no_legacy_model(*args, **kwargs):
        raise AssertionError("an admitted Codex task entered the legacy model provider")
    monkeypatch.setattr(model, "Agent", no_legacy_model)
    monkeypatch.setattr(model, "reserve", no_legacy_model)

    async def business_tool(project, row, claim):
        body_calls.append(str(row["id"]))
        if str(row["id"]) == str(probe["task_id"]):
            return WorkResult(outcome="completed", output={"independent_slot": True})
        answer = await model.run_model(LifecycleAnswer, {"question": "Read the original immutable evidence"},
            system="Return the observed answer", agent_id=project, purpose="research")
        return WorkResult(outcome="completed", output={"text": answer.text})
    monkeypatch.setattr(tools, "execute_tool", business_tool)

    @activity.defn(name="growth_execute")
    async def execute(args: dict):
        started = monotonic()
        result = await runtime.growth_execute(args)
        polls.append({"claim": copy.deepcopy(args["claim"]), "result": result, "seconds": monotonic()-started})
        return result

    @activity.defn(name="growth_finish")
    async def finish(args: dict):
        finishes.append(copy.deepcopy(args))
        return await runtime.growth_finish(args)

    queue = "codex-lifecycle-"+uuid4().hex
    worker_options = dict(task_queue=queue, workflows=[GrowthWorkWorkflow],
        activities=[runtime.growth_advance, execute, finish], max_concurrent_activities=1)
    handles = []
    try:
        async with Worker(temporal, **worker_options):
            handle = await temporal.start_workflow(GrowthWorkWorkflow.run, original, id=uuid4().hex, task_queue=queue)
            handles.append(handle)
            await eventually(lambda: len([poll for poll in polls if poll["claim"]["ref"]["task_id"] == original["task_id"]]) >= 2)
            # With one Activity slot, a blocking model Activity would prevent
            # this independent workflow from advancing and completing.
            independent = await temporal.start_workflow(GrowthWorkWorkflow.run, probe, id=uuid4().hex, task_queue=queue)
            handles.append(independent)
            assert (await asyncio.wait_for(independent.result(), 8))["state"] == "terminal"
            with tx(aid) as connection:
                request = one(connection, "SELECT * FROM lead.local_executor_requests WHERE task_id=%s", (original["task_id"],))
                before_lease = one(connection, "SELECT result#>>'{runtime,lease_until}' lease FROM lead.tasks WHERE id=%s", (original["task_id"],))["lease"]
            assert request and request["state"] == "queued"
            assert not any(item["claim"]["ref"]["task_id"] == original["task_id"] for item in finishes)

        # Lose all volatile continuation state as on worker-process shutdown.
        # The persisted request and original Workflow claim are retained.
        await stop_jobs(jobs)
        local_executor.set_mode(aid, "legacy")
        assert local_executor.bound(aid, task_id=original["task_id"])
        restarted = ActivityJobs()
        monkeypatch.setattr(local_executor_client, "activity_jobs", restarted)
        before = len(polls)
        async with Worker(temporal, **worker_options):
            await eventually(lambda: len(polls) > before)
            with tx(aid) as connection:
                saved = one(connection, "SELECT * FROM lead.local_executor_requests WHERE task_id=%s", (original["task_id"],))
                renewed = one(connection, "SELECT result#>>'{runtime,lease_until}' lease FROM lead.tasks WHERE id=%s", (original["task_id"],))["lease"]
                assert one(connection, "SELECT count(*) n FROM lead.local_executor_commands WHERE kind='run'")["n"] == 1
            assert saved["id"] == request["id"] and saved["payload"] == request["payload"]
            assert datetime.fromisoformat(renewed) > datetime.fromisoformat(before_lease)
            local_executor.events(device["token"], device["epoch"], [{"id": uuid4().hex,
                "request_id": str(request["id"]), "method": "result", "params": {
                    "state": "completed", "output": {"text": "Original request result"},
                    "thread_id": "fixture-original-thread", "turn_id": "fixture-original-turn"}}])
            assert (await asyncio.wait_for(handle.result(), 12))["state"] == "terminal"
        original_polls = [poll for poll in polls if poll["claim"]["ref"]["task_id"] == original["task_id"]]
        assert len(original_polls) >= 3 and all(poll["claim"] == original_polls[0]["claim"] for poll in original_polls)
        assert sum(bool(poll["result"].get("executor_pending")) for poll in original_polls) >= 2
        assert len([item for item in finishes if item["claim"]["ref"]["task_id"] == original["task_id"]]) == 1
        assert body_calls.count(str(original["task_id"])) == 2, "only volatile Python work restarts; the native request must not"
        with tx(aid) as connection:
            assert one(connection, "SELECT state FROM lead.tasks WHERE id=%s", (original["task_id"],))["state"] == "succeeded"
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_requests")["n"] == 1
            assert one(connection, "SELECT count(*) n FROM lead.budget_reservations")["n"] == 0
        before_replay = (len(polls), len(finishes), len(body_calls))
        history = await record_history(handle, "codex-growth-workflow-history.json")
        await Replayer(workflows=[GrowthWorkWorkflow]).replay_workflow(history)
        assert (len(polls), len(finishes), len(body_calls)) == before_replay
        with tx(aid) as connection:
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_commands WHERE kind='run'")["n"] == 1
    finally:
        await stop_jobs(local_executor_client.activity_jobs)
        for handle in handles:
            try:
                await handle.terminate("isolated lifecycle test cleanup")
            except Exception:
                pass


@pytest.mark.asyncio
async def test_channel_native_continuation_keeps_original_tick_and_replays(monkeypatch):
    from lead_app import local_executor_client
    from lead_app.workflows import ChannelDeliveryWorkflow

    temporal = await client()
    jobs = ActivityJobs()
    monkeypatch.setattr(local_executor_client, "activity_jobs", jobs)
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)
    ready = asyncio.Event()
    polls, body_ticks = [], []

    @local_executor_client.native_continuation("lifecycle-channel")
    async def native_tick(args):
        body_ticks.append(args["tick"])
        await ready.wait()
        return {"stop": True}

    @activity.defn(name="channel_delivery_tick")
    async def tick(args: dict):
        result = await native_tick(args)
        polls.append(copy.deepcopy(args))
        return result

    @activity.defn(name="park_channel_target")
    async def forbidden(args: dict):
        raise AssertionError("a valid pending execution was incorrectly parked")

    queue = uuid4().hex
    handle = None
    try:
        async with Worker(temporal, task_queue=queue, workflows=[ChannelDeliveryWorkflow], activities=[tick, forbidden],
                          max_concurrent_activities=1):
            handle = await temporal.start_workflow(ChannelDeliveryWorkflow.run, {"agent_id": uuid4().hex},
                id=uuid4().hex, task_queue=queue)
            await eventually(lambda: len(polls) >= 3)
            assert len(body_ticks) == 1
            ready.set()
            await asyncio.wait_for(handle.result(), 8)
        assert len({item["tick"] for item in polls}) == 1
        before = len(polls), len(body_ticks)
        await Replayer(workflows=[ChannelDeliveryWorkflow]).replay_workflow(
            await record_history(handle, "codex-channel-workflow-history.json"))
        assert (len(polls), len(body_ticks)) == before
    finally:
        await stop_jobs(jobs)
        if handle:
            try:
                await handle.terminate("isolated lifecycle test cleanup")
            except Exception:
                pass


@pytest.mark.asyncio
async def test_chat_continuation_keeps_original_job_and_then_processes_next(monkeypatch):
    from lead_app import activities, local_executor_client
    from lead_app.workflows import ChatWorkflow

    temporal = await client()
    jobs = ActivityJobs()
    monkeypatch.setattr(local_executor_client, "activity_jobs", jobs)
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)
    ready, second_finished = asyncio.Event(), asyncio.Event()
    body_jobs, polls = [], []

    async def chat_body(args):
        body_jobs.append(args["job_id"])
        if args["job_id"] == "first":
            await ready.wait()
        else:
            second_finished.set()
    monkeypatch.setattr(activities, "_chat_job", chat_body)

    @activity.defn(name="chat_job")
    async def chat(args: dict):
        # Bypass only the unrelated tenant resolver; use the real chat wrapper.
        result = await activities.chat_job.__wrapped__(args)
        polls.append(copy.deepcopy(args))
        return result

    queue = uuid4().hex
    handle = None
    try:
        async with Worker(temporal, task_queue=queue, workflows=[ChatWorkflow], activities=[chat], max_concurrent_activities=1):
            handle = await temporal.start_workflow(ChatWorkflow.run, uuid4().hex, id=uuid4().hex, task_queue=queue,
                start_signal="enqueue", start_signal_args=["first"])
            await eventually(lambda: len(polls) >= 3)
            await handle.signal("enqueue", "second")
            assert body_jobs == ["first"]
            ready.set()
            await asyncio.wait_for(second_finished.wait(), 8)
            await eventually(lambda: any(item["job_id"] == "second" for item in polls))
            history = await record_history(handle, "codex-chat-workflow-history.json")
        assert body_jobs == ["first", "second"]
        before = len(polls), len(body_jobs)
        await Replayer(workflows=[ChatWorkflow]).replay_workflow(history)
        assert (len(polls), len(body_jobs)) == before
    finally:
        await stop_jobs(jobs)
        if handle:
            try:
                await handle.terminate("isolated lifecycle test cleanup")
            except Exception:
                pass
