import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)


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
                await workflow.execute_activity(
                    "chat_job",
                    {"agent_id": agent_id, "job_id": job},
                    start_to_close_timeout=timedelta(minutes=4),
                    retry_policy=RETRY,
                )
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
            result = await workflow.execute_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=policy,
                schedule_to_close_timeout=timedelta(days=1) if resilient else None,
            )
        except ActivityError:
            await workflow.execute_activity(
                "mark_task_failed",
                args,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RETRY,
            )
            return {"state": "failed"}
        if workflow.patched("durable-publish-reconciliation-v1"):
            for delay in (5, 30, 120, 300, 900, 1800, 3600):
                if result.get("state") != "unknown":
                    break
                await workflow.sleep(delay)
                result = await workflow.execute_activity(
                    "execute_task",
                    args,
                    start_to_close_timeout=timedelta(minutes=4),
                    retry_policy=policy,
                    schedule_to_close_timeout=timedelta(days=1) if resilient else None,
                )
        elif result.get("state") == "unknown":
            await workflow.sleep(5)
            result = await workflow.execute_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
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
        for _ in range(100):
            self.changed = False
            pending_cycle = pending_cycle or str(workflow.uuid4())
            try:
                result = await workflow.execute_activity(
                    "autonomous_cycle",
                    {"agent_id": aid, "cycle_id": pending_cycle},
                    start_to_close_timeout=timedelta(minutes=4),
                    retry_policy=RETRY,
                )
                pending_cycle = None
            except ActivityError:
                if not recover:
                    raise
                result = {"delay": 60}
            if workflow.patched("stop-archived-supervisor-v1") and result.get(
                "archived"
            ):
                return
            try:
                await workflow.wait_condition(
                    lambda: self.changed, timeout=timedelta(seconds=result["delay"])
                )
            except asyncio.TimeoutError:
                pass
        if recover:
            workflow.continue_as_new(args=[aid, pending_cycle])
        workflow.continue_as_new(aid)
