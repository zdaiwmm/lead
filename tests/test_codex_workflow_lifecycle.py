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


async def chain_histories(temporal, handle):
    histories, run_id = [], handle.first_execution_run_id
    while run_id:
        history = await temporal.get_workflow_handle(handle.id, run_id=run_id).fetch_history()
        histories.append(history)
        continued = [event.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
            for event in history.events if event.HasField("workflow_execution_continued_as_new_event_attributes")]
        run_id = continued[0] if continued else None
    return histories


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["growth", "growth_existing_history", "channel", "supervisor", "publish", "publish_reconcile", "legacy_reconcile"])
async def test_long_native_pending_rotates_history_without_new_business_attempt(kind):
    from lead_app.workflows import (
        ChannelDeliveryWorkflow,
        GrowthWorkWorkflow,
        PublishWorkflow,
        SupervisorWorkflow,
    )
    from temporalio.testing import WorkflowEnvironment

    assert os.environ.get("LEAD_GROWTH_TEST_ENV") == "1"
    workflows = {"growth": GrowthWorkWorkflow, "growth_existing_history": GrowthWorkWorkflow, "channel": ChannelDeliveryWorkflow,
        "supervisor": SupervisorWorkflow, "publish": PublishWorkflow,
        "publish_reconcile": PublishWorkflow, "legacy_reconcile": PublishWorkflow}
    target = workflows[kind]
    ref = {"agent_id": uuid4().hex, "task_id": uuid4().hex, "task_generation": 1}
    claim = {"ref": ref, "kind": "research", "attempt_token": uuid4().hex}
    executions, advances, finishes, policies = [], [], [], []

    @activity.defn(name="growth_advance")
    async def advance(args: dict):
        advances.append(copy.deepcopy(args))
        return {"state": "terminal"} if finishes else {"state": "ready", "claim": claim}

    @activity.defn(name="growth_finish")
    async def finish(args: dict):
        finishes.append(copy.deepcopy(args))
        return {"accepted_control": True}

    @activity.defn(name="execution_policy")
    async def policy(args: dict):
        policies.append(copy.deepcopy(args))
        return {"retry_interval_seconds": 2, "retry_attempts": 3}

    async def body(args):
        executions.append(copy.deepcopy(args))
        if kind.endswith("reconcile"):
            # The initial call observes unknown. The first reconciliation then
            # waits for 105 native polls, spanning a new Workflow execution.
            if len(executions) == 1 or len(executions) > 106:
                return {"state": "unknown"}
        if len(executions) <= (106 if kind.endswith("reconcile") else 105):
            return {"executor_pending": True}
        return {"stop": True, "state": "completed", "outcome": "completed"}

    names = {"growth": "growth_execute", "growth_existing_history": "growth_execute", "channel": "channel_delivery_tick", "supervisor": "autonomous_cycle"}
    execute = activity.defn(name=names.get(kind, "execute_task"))(body)
    activities = [advance, finish, policy, execute]
    folder = STATE.parent/"test-server"
    folder.mkdir(exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(folder)) as env:
        queue = uuid4().hex
        def admit_patch(info):
            # Model an existing history whose first 102 pending results have no
            # new marker. A memoized False must not disable rotation forever.
            return not (kind == "growth_existing_history"
                and info.patch_id.startswith("local-codex-pending-history-v1") and len(executions) < 103)

        async with Worker(env.client, task_queue=queue, workflows=[target], activities=activities,
                          max_concurrent_activities=1, patch_activation_callback=admit_patch):
            args = ref if kind != "legacy_reconcile" else {"task_id": ref["task_id"]}
            if kind == "supervisor":
                args = ref["agent_id"]
            handle = await env.client.start_workflow(target.run, args, id=uuid4().hex, task_queue=queue)
            result = await asyncio.wait_for(handle.result(), 25)
        histories = await chain_histories(env.client, handle)
        assert len(histories) == 2, "a long native wait must rotate before unbounded history accumulates"
        assert executions and all(item == executions[0] for item in executions)
        if kind.startswith("growth"):
            assert len(advances) == 2, "resume the original claim without acquiring another attempt"
            assert len(finishes) == 1 and finishes[0]["claim"] == claim
        if kind.startswith("publish"):
            assert len(policies) == 1, "the original admitted retry policy survives rotation"
        if kind.endswith("reconcile"):
            assert result == {"state": "unknown"}
            timers = [event.timer_started_event_attributes.start_to_fire_timeout.seconds
                for history in histories for event in history.events
                if event.HasField("timer_started_event_attributes")]
            assert [delay for delay in timers if delay != 2] == [5, 30, 120, 300, 900, 1800, 3600]
        before = len(executions), len(advances), len(finishes), len(policies)
        for index, history in enumerate(histories):
            (STATE/f"codex-rollover-{kind}-{index}.json").write_text(history.to_json())
            await Replayer(workflows=[target]).replay_workflow(history)
        assert (len(executions), len(advances), len(finishes), len(policies)) == before


@pytest.mark.asyncio
async def test_chat_pending_rotation_keeps_current_job_and_signaled_queue():
    from lead_app.workflows import ChatWorkflow
    from temporalio.testing import WorkflowEnvironment

    assert os.environ.get("LEAD_GROWTH_TEST_ENV") == "1"
    polls, completed = [], []
    second_done = asyncio.Event()

    @activity.defn(name="chat_job")
    async def chat(args: dict):
        polls.append(copy.deepcopy(args))
        if args["job_id"] == "first" and len(polls) <= 105:
            return {"executor_pending": True}
        completed.append(args["job_id"])
        if args["job_id"] == "second":
            second_done.set()
        return {"completed": True}

    folder = STATE.parent/"test-server"
    folder.mkdir(exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(folder)) as env:
        queue, aid = uuid4().hex, uuid4().hex
        result = None
        handle = None
        try:
            async with Worker(env.client, task_queue=queue, workflows=[ChatWorkflow], activities=[chat],
                              max_concurrent_activities=1):
                handle = await env.client.start_workflow(ChatWorkflow.run, args=[aid, ["first"]],
                    id=uuid4().hex, task_queue=queue)
                await eventually(lambda: len(polls) >= 1)
                await handle.signal("enqueue", "second")
                result = asyncio.create_task(handle.result())  # Enable real server time skipping.
                await asyncio.wait_for(second_done.wait(), 25)
                await eventually(lambda: len(polls) == 107)
            histories = await chain_histories(env.client, handle)
            assert len(histories) == 2
            assert completed == ["first", "second"]
            assert all(item == {"agent_id": aid, "job_id": "first"} for item in polls[:-1])
            before = len(polls)
            for index, history in enumerate(histories):
                (STATE/f"codex-rollover-chat-{index}.json").write_text(history.to_json())
                await Replayer(workflows=[ChatWorkflow]).replay_workflow(history)
            assert len(polls) == before
        finally:
            if result:
                result.cancel()
                await asyncio.gather(result, return_exceptions=True)
            if handle:
                await handle.terminate("isolated chat rotation cleanup")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["growth", "channel", "supervisor", "publish", "chat"])
async def test_old_run_adopts_future_native_pending_without_advancing_identity(kind):
    from lead_app.workflows import (
        ChannelDeliveryWorkflow,
        ChatWorkflow,
        GrowthWorkWorkflow,
        PublishWorkflow,
        SupervisorWorkflow,
    )
    from temporalio.testing import WorkflowEnvironment

    assert os.environ.get("LEAD_GROWTH_TEST_ENV") == "1"
    targets = {"growth": GrowthWorkWorkflow, "channel": ChannelDeliveryWorkflow,
        "supervisor": SupervisorWorkflow, "publish": PublishWorkflow, "chat": ChatWorkflow}
    target = targets[kind]
    ref = {"agent_id": uuid4().hex, "task_id": uuid4().hex, "task_generation": 1}
    claims = [{"ref": ref, "kind": "research", "attempt_token": uuid4().hex} for _ in range(2)]
    calls, finishes, applied = [], [], []
    chat_done = asyncio.Event()
    v1_markers = {"local-codex-activity-continuation-v1", "local-codex-channel-continuation-v1",
        "local-codex-chat-continuation-v1", "local-codex-supervisor-continuation-v1"}

    @activity.defn(name="growth_advance")
    async def advance(args: dict):
        return {"state": "terminal"} if len(finishes) >= 2 else {"state": "ready", "claim": claims[len(finishes)]}

    @activity.defn(name="growth_finish")
    async def finish(args: dict):
        finishes.append(copy.deepcopy(args))
        return {"accepted_control": True}

    @activity.defn(name="execution_policy")
    async def policy(args: dict):
        return {"retry_interval_seconds": 2, "retry_attempts": 3}

    async def execute(args):
        calls.append(copy.deepcopy(args))
        if kind == "chat" and args["job_id"] == "third":
            chat_done.set()
            return {"completed": True}
        if len(calls) == 1:
            # This normal result causes an old Run to cache v1=False before a
            # later, newly paired native execution first becomes pending.
            return {"outcome": "deferred", "state": "deferred", "delay": 1, "cadence_version": 1}
        if len(calls) <= 4:
            return {"executor_pending": True, "delay": 1, "cadence_version": 1}
        return {"stop": True, "state": "completed", "outcome": "completed"}

    names = {"growth": "growth_execute", "channel": "channel_delivery_tick",
        "supervisor": "autonomous_cycle", "publish": "execute_task", "chat": "chat_job"}
    execute_activity = activity.defn(name=names[kind])(execute)
    folder = STATE.parent/"test-server"
    folder.mkdir(exist_ok=True)
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(folder)) as env:
        queue, handle, result_waiter = uuid4().hex, None, None
        def old_patch(info):
            applied.append(info.patch_id)
            return info.patch_id not in v1_markers

        try:
            async with Worker(env.client, task_queue=queue, workflows=[target],
                              activities=[advance, finish, policy, execute_activity],
                              max_concurrent_activities=1, patch_activation_callback=old_patch):
                if kind == "chat":
                    handle = await env.client.start_workflow(target.run,
                        args=[ref["agent_id"], ["first", "second", "third"]], id=uuid4().hex, task_queue=queue)
                    result_waiter = asyncio.create_task(handle.result())
                    await asyncio.wait_for(chat_done.wait(), 15)
                    await eventually(lambda: calls[-1]["job_id"] == "third")
                else:
                    arg = ref["agent_id"] if kind == "supervisor" else ref
                    handle = await env.client.start_workflow(target.run, arg, id=uuid4().hex, task_queue=queue)
                    await asyncio.wait_for(handle.result(), 15)
            expected = calls[1:5]
            assert len(expected) == 4 and all(item == expected[0] for item in expected), (
                "an old v1=False Run must wait on the same new native claim/job/tick/cycle")
            assert any(marker in v1_markers for marker in applied)
            if kind == "growth":
                assert len(finishes) == 2
                assert not any(item["result"].get("executor_pending") for item in finishes)
                assert finishes[1]["claim"] == claims[1]
            if kind == "chat":
                assert [item["job_id"] for item in calls] == ["first", "second", "second", "second", "second", "third"]
            before = len(calls), len(finishes)
            history = await record_history(handle, f"codex-adoption-{kind}.json")
            await Replayer(workflows=[target]).replay_workflow(history)
            assert (len(calls), len(finishes)) == before
        finally:
            if result_waiter:
                result_waiter.cancel()
                await asyncio.gather(result_waiter, return_exceptions=True)
            if handle:
                try:
                    await handle.terminate("isolated old-run adoption cleanup")
                except Exception:
                    pass
