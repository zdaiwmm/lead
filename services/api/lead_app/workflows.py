import asyncio
from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)

# Preserve the timer recorded by supervisors running before pressure-aware pacing.
LEGACY_CYCLE_GAP_SECONDS = 480


async def durable_activity(name, args, **options):
    result = await workflow.execute_activity(name, args, **options)
    if workflow.patched("local-codex-channel-continuation-v1"):
        while isinstance(result, dict) and result.get("executor_pending"):
            await workflow.sleep(2)
            result = await workflow.execute_activity(name, args, **options)
    return result


def activity_queue(kind):
    # Recorded patch preserves old histories; custom/test queues remain local.
    isolated = workflow.patched("isolated-activity-capacity-" + kind + "-v1")
    queue = workflow.info().task_queue
    return "lead-" + kind if isolated and queue == "lead" else queue


@workflow.defn
class GrowthWorkWorkflow:
    """One immutable TaskRef. Timers free activity capacity; signals only recheck facts."""
    def __init__(self):
        self.wakes = 0

    @workflow.signal
    def wake(self):
        self.wakes += 1

    @workflow.run
    async def run(self, ref: dict):
        for _ in range(100):
            seen = self.wakes
            step = await workflow.execute_activity("growth_advance", ref,
                start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
            if step["state"] == "terminal":
                return step
            if step["state"] == "wait":
                remaining = max(.05, (datetime.fromisoformat(step["wait"]["check_at"])-workflow.now()).total_seconds())
                try:
                    await workflow.wait_condition(lambda: self.wakes > seen, timeout=timedelta(seconds=remaining))
                except asyncio.TimeoutError:
                    pass
                continue
            claim = step["claim"]
            kind = claim.get("kind")
            lane = "growth-model" if kind == "decision" else "growth-work" if kind in ("research", "content") else "growth-receipts"
            try:
                result = await workflow.execute_activity("growth_execute", {"agent_id": ref["agent_id"], "claim": claim},
                    task_queue=activity_queue(lane), start_to_close_timeout=timedelta(seconds=500),
                    retry_policy=RetryPolicy(maximum_attempts=1))
                if workflow.patched("local-codex-activity-continuation-v1"):
                    while result.get("executor_pending"):
                        await workflow.sleep(2)
                        result = await workflow.execute_activity("growth_execute", {"agent_id": ref["agent_id"], "claim": claim},
                            task_queue=activity_queue(lane), start_to_close_timeout=timedelta(seconds=30),
                            retry_policy=RETRY)
            except ActivityError:
                # A lost worker may have crossed an external barrier. Re-read the original ledger.
                result = {"outcome": "failed", "failure_code": "execution_lost"}
            await workflow.execute_activity("growth_finish", {"agent_id": ref["agent_id"], "claim": claim, "result": result},
                start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
        workflow.continue_as_new(ref)


@workflow.defn
class GrowthDecisionWorkflow:
    """Coalesce triggers; only the ordinary decision task invokes the model."""
    def __init__(self):
        self.pending = []

    @workflow.signal
    def wake(self, trigger: dict):
        self.pending.append(trigger)

    @workflow.run
    async def run(self, agent_id: str, pending: list | None = None):
        self.pending = (pending or []) + self.pending
        for _ in range(100):
            await workflow.wait_condition(lambda: bool(self.pending))
            await workflow.sleep(.25)
            batch, self.pending = self.pending, []
            await workflow.execute_activity("growth_coordinate", {"agent_id": agent_id, "triggers": batch},
                start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
        workflow.continue_as_new(args=[agent_id, self.pending])


@workflow.defn
class ChannelPreparationWorkflow:
    @workflow.run
    async def run(self, args: dict):
        try:
            for attempt in range(8):
                result = await workflow.execute_activity("prepare_channel_atom", args,
                    start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
                if result != "control_busy":
                    return
                if attempt < 7:
                    await workflow.sleep(min(60, 2 ** (attempt + 1)))
            await workflow.execute_activity("close_channel_atom", args,
                start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
        except ActivityError:
            await workflow.execute_activity("close_channel_atom", args,
                start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
            raise


@workflow.defn
class ChannelDeliveryWorkflow:
    def __init__(self):
        self.wake_count = 0
        self.wake_version = -1

    @workflow.signal
    def wake(self, policy_version: int):
        self.wake_count += 1
        self.wake_version = max(self.wake_version, policy_version)

    async def wait_cadence(self, result, seen):
        decision = result.get("execution_cadence") or {}
        state = decision.get("state")
        version = max((item.get("policy_version", 0) for item in decision.get("constraints", [])), default=0)
        def changed():
            return self.wake_count > seen and self.wake_version >= version
        if state == "waiting_event":
            await workflow.wait_condition(changed)
        elif state == "waiting_time" and decision.get("next_at"):
            remaining = max(0, (datetime.fromisoformat(decision["next_at"]) - workflow.now()).total_seconds())
            if remaining:
                try:
                    await workflow.wait_condition(changed, timeout=timedelta(seconds=remaining))
                except asyncio.TimeoutError:
                    pass
        else:
            await workflow.sleep(max(1, float(result["delay"])))

    @workflow.run
    async def run(self, args: dict, pending_tick: str | None = None):
        cadence_waits = workflow.patched("execution-cadence-waits-v1")
        for _ in range(100):
            seen = self.wake_count
            pending_tick = pending_tick or str(workflow.uuid4())
            try:
                result = await durable_activity(
                    "channel_research_tick" if args.get("lane")=="research" else "channel_delivery_tick", {**args, "tick": pending_tick},
                    start_to_close_timeout=timedelta(minutes=8), retry_policy=RETRY,
                )
                if result.get("stop"):
                    return
                pending_tick = None
                # Old runs replay the old patch branch. A newly completed,
                # protocol-bearing activity can opt that same run into waits;
                # historical results without the field retain their commands.
                if cadence_waits or (result.get("execution_cadence") or {}).get("execution_cadence_protocol") == 1:
                    await self.wait_cadence(result, seen)
                else:
                    await workflow.sleep(max(1, float(result["delay"])))
            except ActivityError:
                try:
                    result = await workflow.execute_activity(
                        "park_channel_target",
                        {**args, "tick": pending_tick},
                        start_to_close_timeout=timedelta(seconds=30),
                        retry_policy=RETRY,
                    )
                    if result.get("stop"):
                        return
                    pending_tick = None
                    await workflow.sleep(result["delay"])
                except ActivityError:
                    # A storage outage preserves the original tick across restarts.
                    await workflow.sleep(60)
        workflow.continue_as_new(args=[args, pending_tick])


@workflow.defn
class AnalyticsIngestWorkflow:
    @workflow.run
    async def run(self, args: dict):
        for _ in range(100):
            result = await workflow.execute_activity(
                "process_analytics_ingest",
                args,
                start_to_close_timeout=timedelta(seconds=45),
                retry_policy=RETRY,
            )
            if result.get("state") != "pending":
                return result
            await workflow.sleep(result.get("delay", 2))
        workflow.continue_as_new(args)


@workflow.defn
class ChatWorkflow:
    def __init__(self):
        self.queue = []

    @workflow.signal
    async def enqueue(self, job_id: str):
        self.queue.append(job_id)

    @workflow.run
    async def run(self, agent_id: str, pending: list[str] | None = None):
        self.queue = (pending or []) + self.queue
        count = 0
        recover = workflow.patched("chat-transient-recovery-v1")
        while True:
            await workflow.wait_condition(lambda: bool(self.queue))
            job = self.queue.pop(0)
            try:
                result = await workflow.execute_activity(
                    "chat_job",
                    {"agent_id": agent_id, "job_id": job},
                    start_to_close_timeout=timedelta(minutes=4),
                    task_queue=activity_queue("chat"),
                    retry_policy=RETRY,
                )
                if workflow.patched("local-codex-chat-continuation-v1"):
                    while isinstance(result, dict) and result.get("executor_pending"):
                        await workflow.sleep(2)
                        result = await workflow.execute_activity("chat_job", {"agent_id": agent_id, "job_id": job},
                            start_to_close_timeout=timedelta(seconds=30), task_queue=activity_queue("chat"), retry_policy=RETRY)
            except ActivityError:
                if not recover:
                    raise
                self.queue.insert(0, job)
                await workflow.sleep(60)
            count += 1
            if count >= 100:
                workflow.continue_as_new(args=[agent_id, self.queue])


@workflow.defn
class PublishWorkflow:
    @workflow.run
    async def run(self, args: dict):
        if "agent_id" in args and workflow.patched("admin-bounded-runtime-v1"):
            settings = await workflow.execute_activity(
                "execution_policy",
                args,
                start_to_close_timeout=timedelta(seconds=30),
                task_queue=activity_queue("publish"),
                retry_policy=RETRY,
            )
            policy = RetryPolicy(
                initial_interval=timedelta(seconds=settings["retry_interval_seconds"]),
                maximum_interval=timedelta(
                    seconds=settings["retry_interval_seconds"] * 4
                ),
                maximum_attempts=settings["retry_attempts"],
                non_retryable_error_types=[
                    "ValueError",
                    "LookupError",
                    "TypeError",
                    "KeyError",
                    "PermissionError",
                ],
            )
            for _ in range(100):
                try:
                    result = await durable_activity(
                        "execute_task",
                        args,
                        start_to_close_timeout=timedelta(minutes=4),
                        task_queue=activity_queue("publish"),
                        retry_policy=policy,
                    )
                except ActivityError:
                    stopped = await workflow.execute_activity(
                        "mark_task_failed",
                        args,
                        start_to_close_timeout=timedelta(seconds=30),
                        task_queue=activity_queue("publish"),
                        retry_policy=RETRY,
                    )
                    if stopped and stopped.get("state") == "unknown":
                        result = stopped
                        break
                    return {"state": "failed"}
                if result.get("state") != "deferred":
                    break
                await workflow.sleep(result["delay"])
            else:
                workflow.continue_as_new(args)
            for delay in (5, 30, 120, 300, 900, 1800, 3600):
                if result.get("state") != "unknown":
                    break
                await workflow.sleep(delay)
                result = await durable_activity(
                    "execute_task",
                    args,
                    start_to_close_timeout=timedelta(minutes=4),
                    task_queue=activity_queue("publish"),
                    retry_policy=policy,
                )
            return result
        resilient = workflow.patched("publish-transient-retry-v1")
        policy = (
            RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_interval=timedelta(seconds=60),
                maximum_attempts=0,
                non_retryable_error_types=[
                    "ValueError",
                    "LookupError",
                    "TypeError",
                    "KeyError",
                    "PermissionError",
                ],
            )
            if resilient
            else RETRY
        )
        try:
            result = await durable_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
                task_queue=activity_queue("publish"),
                retry_policy=policy,
                schedule_to_close_timeout=timedelta(days=1) if resilient else None,
            )
        except ActivityError:
            await workflow.execute_activity(
                "mark_task_failed",
                args,
                start_to_close_timeout=timedelta(seconds=30),
                task_queue=activity_queue("publish"),
                retry_policy=RETRY,
            )
            return {"state": "failed"}
        if workflow.patched("durable-publish-reconciliation-v1"):
            for delay in (5, 30, 120, 300, 900, 1800, 3600):
                if result.get("state") != "unknown":
                    break
                await workflow.sleep(delay)
                result = await durable_activity(
                    "execute_task",
                    args,
                    start_to_close_timeout=timedelta(minutes=4),
                    task_queue=activity_queue("publish"),
                    retry_policy=policy,
                    schedule_to_close_timeout=timedelta(days=1) if resilient else None,
                )
        elif result.get("state") == "unknown":
            await workflow.sleep(5)
            result = await durable_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
                task_queue=activity_queue("publish"),
                retry_policy=policy,
                schedule_to_close_timeout=timedelta(days=1) if resilient else None,
            )
        return result


@workflow.defn
class SupervisorWorkflow:
    def __init__(self):
        self.changed = False

    @workflow.signal
    async def wake(self):
        self.changed = True

    @workflow.run
    async def run(self, aid: str, pending_cycle: str | None = None):
        recover = workflow.patched("supervisor-transient-recovery-v1")
        stage_heartbeat = workflow.patched("community-stage-heartbeat-v1")
        for _ in range(100):
            self.changed = False
            pending_cycle = pending_cycle or str(workflow.uuid4())
            try:
                result = await workflow.execute_activity(
                    "autonomous_cycle",
                    {"agent_id": aid, "cycle_id": pending_cycle},
                    # 东京主机的 CPU 突发额度耗尽时单轮运营循环会慢数倍，
                    # 4 分钟超时会导致活动超时重试堆叠、CPU 需求倍增。
                    # 提高到 20 分钟让限速下单轮循环能真正跑完，而不是无限重试。
                    start_to_close_timeout=timedelta(minutes=20),
                    heartbeat_timeout=timedelta(minutes=2) if stage_heartbeat else None,
                    retry_policy=RETRY,
                )
                if workflow.patched("local-codex-supervisor-continuation-v1"):
                    while result.get("executor_pending"):
                        await workflow.sleep(2)
                        result = await workflow.execute_activity("autonomous_cycle", {"agent_id": aid, "cycle_id": pending_cycle},
                            start_to_close_timeout=timedelta(seconds=30), retry_policy=RETRY)
                pending_cycle = None
            except ActivityError:
                if not recover:
                    raise
                result = {"delay": 60}
            if result.get("stop"):
                return
            if workflow.patched("stop-archived-supervisor-v1") and result.get(
                "archived"
            ):
                return
            # A replaying run can memoize the old patch branch until continue-as-new.
            # Fresh activity results explicitly opt that same run into the new timer.
            if result.get("cadence_version") == 1 or workflow.patched("supervisor-pressure-aware-cadence-v1"):
                delay = max(1, int(result.get("delay", 60)))
            else:
                delay = max(int(result.get("delay", 0)), LEGACY_CYCLE_GAP_SECONDS)
            try:
                await workflow.wait_condition(
                    lambda: self.changed, timeout=timedelta(seconds=delay)
                )
            except asyncio.TimeoutError:
                pass
        if recover:
            workflow.continue_as_new(args=[aid, pending_cycle])
        workflow.continue_as_new(aid)
