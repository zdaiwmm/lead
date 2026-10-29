"""Opt-in tests against real Temporal timers, retries, and history replay."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app.config import CONFIG
from lead_app.workflows import ChatWorkflow, SupervisorWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1",
    reason="requires running Temporal and a real 60 second recovery timer",
)


@pytest.mark.asyncio
async def test_transient_failure_keeps_chat_and_supervisor_alive():
    seen = {"supervisor": [], "chat": []}
    finished = asyncio.Event()

    @activity.defn(name="autonomous_cycle")
    async def cycle(args: dict) -> dict:
        seen["supervisor"].append(args["cycle_id"])
        if len(seen["supervisor"]) <= 3:
            raise OSError("isolated service failure")
        return {"archived": True, "delay": 300}

    @activity.defn(name="chat_job")
    async def chat(args: dict):
        seen["chat"].append(args["job_id"])
        if len(seen["chat"]) <= 3:
            raise OSError("isolated service failure")
        if args["job_id"] == "second":
            finished.set()

    client = await Client.connect(CONFIG["temporal_address"])
    queue = "lead-recovery-test-" + uuid4().hex
    handles = []
    async with Worker(
        client,
        task_queue=queue,
        workflows=[SupervisorWorkflow, ChatWorkflow],
        activities=[cycle, chat],
    ):
        try:
            supervisor = await client.start_workflow(
                SupervisorWorkflow.run,
                "isolated-test-agent",
                id=queue + "-supervisor",
                task_queue=queue,
                execution_timeout=timedelta(minutes=3),
            )
            handles.append(supervisor)
            conversation = await client.start_workflow(
                ChatWorkflow.run,
                "isolated-test-agent",
                id=queue + "-chat",
                task_queue=queue,
                start_signal="enqueue",
                start_signal_args=["first"],
                execution_timeout=timedelta(minutes=3),
            )
            handles.append(conversation)
            await conversation.signal("enqueue", "second")
            await asyncio.wait_for(
                asyncio.gather(supervisor.result(), finished.wait()), timeout=110
            )
            assert len(seen["supervisor"]) == 4
            assert len(set(seen["supervisor"])) == 1
            assert seen["chat"] == ["first"] * 4 + ["second"]
            await Replayer(workflows=[SupervisorWorkflow]).replay_workflow(
                await supervisor.fetch_history()
            )
        finally:
            for handle in handles:
                try:
                    await handle.terminate("isolated recovery test cleanup")
                except Exception:
                    pass
