"""Real Temporal execution and replay of bounded retries and safe deferrals."""

import asyncio
import os
from datetime import timedelta

import pytest
from lead_app.config import CONFIG
from lead_app.db import uid
from lead_app.workflows import PublishWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1", reason="requires local Temporal"
)


@pytest.mark.asyncio
async def test_managed_publish_honors_retry_limit_and_reconciles_same_task():
    calls = {"bounded": 0, "reconcile": 0, "failed_uncertain": 0}
    failures = []

    @activity.defn(name="execution_policy")
    async def policy(args: dict):
        return {
            "retry_attempts": 2,
            "retry_interval_seconds": 5,
            "version": "runtime/verification@1",
        }

    @activity.defn(name="execute_task")
    async def execute(args: dict):
        task = args["task_id"]
        calls[task] += 1
        if task == "bounded":
            raise RuntimeError("not_dispatched")
        if task == "failed_uncertain":
            if calls[task] <= 2:
                raise RuntimeError("transport_failed_after_dispatch")
            return {"state": "confirmed"}
        return (
            {"state": "deferred", "delay": 1}
            if calls[task] == 1
            else {"state": "unknown"}
            if calls[task] == 2
            else {"state": "confirmed"}
        )

    @activity.defn(name="mark_task_failed")
    async def failed(args: dict):
        failures.append(args["task_id"])
        return (
            {"state": "unknown"}
            if args["task_id"] == "failed_uncertain"
            else {"state": "failed"}
        )

    client = await Client.connect(CONFIG["temporal_address"])
    queue = "admin-workflow-verification-" + uid()
    handles = []
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PublishWorkflow],
        activities=[policy, execute, failed],
    ):
        try:
            for task in calls:
                handles.append(
                    await client.start_workflow(
                        PublishWorkflow.run,
                        {"agent_id": "verification-agent", "task_id": task},
                        id=queue + task,
                        task_queue=queue,
                        execution_timeout=timedelta(seconds=45),
                    )
                )
            results = await asyncio.wait_for(
                asyncio.gather(*[handle.result() for handle in handles]), timeout=40
            )
            assert results == [
                {"state": "failed"},
                {"state": "confirmed"},
                {"state": "confirmed"},
            ]
            assert calls == {"bounded": 2, "reconcile": 3, "failed_uncertain": 3}
            assert set(failures) == {"bounded", "failed_uncertain"}
            for handle in handles:
                await Replayer(workflows=[PublishWorkflow]).replay_workflow(
                    await handle.fetch_history()
                )
        finally:
            for handle in handles:
                try:
                    await handle.terminate("admin workflow verification finished")
                except Exception:
                    pass
