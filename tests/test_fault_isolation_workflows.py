"""Reserved capacity and real Temporal replay; only fake activities run."""

import asyncio
import os
from datetime import timedelta

import pytest
from lead_app.config import CONFIG
from lead_app.db import uid
from lead_app.workflows import ChatWorkflow, PublishWorkflow, SupervisorWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1", reason="requires isolated Temporal"
)


@pytest.mark.asyncio
async def test_long_research_does_not_occupy_chat_or_publication_capacity():
    client = await Client.connect(CONFIG["temporal_address"])
    running, release, chatted = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @activity.defn(name="autonomous_cycle")
    async def research(args: dict):
        running.set()
        await release.wait()
        return {"archived": True, "delay": 300}

    @activity.defn(name="chat_job")
    async def chat(args: dict):
        chatted.set()

    @activity.defn(name="execution_policy")
    async def policy(args: dict):
        return {"retry_interval_seconds": 5, "retry_attempts": 2}

    @activity.defn(name="execute_task")
    async def publish(args: dict):
        return {"state": "confirmed", "external_id": "test-only"}

    @activity.defn(name="mark_task_failed")
    async def failed(args: dict):
        return {"state": "failed"}

    aid = uid()
    async with (
        Worker(
            client,
            task_queue="lead",
            workflows=[SupervisorWorkflow, ChatWorkflow, PublishWorkflow],
            activities=[research],
            max_concurrent_activities=1,
        ),
        Worker(
            client,
            task_queue="lead-chat",
            activities=[chat],
            max_concurrent_activities=1,
        ),
        Worker(
            client,
            task_queue="lead-publish",
            activities=[policy, publish, failed],
            max_concurrent_activities=1,
        ),
    ):
        supervisor = await client.start_workflow(
            SupervisorWorkflow.run,
            aid,
            id="capacity-supervisor:" + aid,
            task_queue="lead",
            execution_timeout=timedelta(seconds=30),
        )
        await asyncio.wait_for(running.wait(), 10)
        conversation = await client.start_workflow(
            ChatWorkflow.run,
            aid,
            id="capacity-chat:" + aid,
            task_queue="lead",
            start_signal="enqueue",
            start_signal_args=[uid()],
            execution_timeout=timedelta(seconds=30),
        )
        publication = await client.start_workflow(
            PublishWorkflow.run,
            {"agent_id": aid, "task_id": uid()},
            id="capacity-publish:" + aid,
            task_queue="lead",
            execution_timeout=timedelta(seconds=30),
        )
        try:
            result = await asyncio.wait_for(publication.result(), 10)
            await asyncio.wait_for(chatted.wait(), 10)
            assert result["state"] == "confirmed" and not release.is_set()
        finally:
            release.set()
            await conversation.cancel()
        await supervisor.result()
        await Replayer(workflows=[PublishWorkflow]).replay_workflow(
            await publication.fetch_history()
        )
        await Replayer(workflows=[SupervisorWorkflow]).replay_workflow(
            await supervisor.fetch_history()
        )
