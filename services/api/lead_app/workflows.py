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
        while True:
            await workflow.wait_condition(lambda: bool(self.queue))
            job = self.queue.pop(0)
            await workflow.execute_activity(
                "chat_job",
                {"agent_id": agent_id, "job_id": job},
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RETRY,
            )
            count += 1
            if count >= 100:
                workflow.continue_as_new(args=[agent_id, self.queue])


@workflow.defn
class PublishWorkflow:
    @workflow.run
    async def run(self, args: dict):
        try:
            result = await workflow.execute_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RETRY,
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
                    retry_policy=RETRY,
                )
        elif result.get("state") == "unknown":
            await workflow.sleep(5)
            result = await workflow.execute_activity(
                "execute_task",
                args,
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RETRY,
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
    async def run(self, aid: str):
        for _ in range(100):
            self.changed = False
            result = await workflow.execute_activity(
                "autonomous_cycle",
                {"agent_id": aid, "cycle_id": str(workflow.uuid4())},
                start_to_close_timeout=timedelta(minutes=4),
                retry_policy=RETRY,
            )
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
        workflow.continue_as_new(aid)
