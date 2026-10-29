import asyncio
import logging
import time

from temporalio.client import Client
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from .activities import autonomous_cycle, chat_job, execute_task, mark_task_failed
from .config import CONFIG, STATE, model_config
from .db import all_rows, emit, one, tx
from .workflows import ChatWorkflow, PublishWorkflow, SupervisorWorkflow


async def publish_outbox(client):
    key_was_configured = False
    while True:
        try:
            configured = bool(model_config()["key"])
            if configured and not key_was_configured:
                with tx() as c:
                    blocked = all_rows(
                        c,
                        "SELECT j.id,j.agent_id,j.message_id FROM lead.chat_jobs j JOIN lead.agents a ON a.id=j.agent_id WHERE j.status='blocked' AND j.error_code='MODEL_NOT_CONFIGURED' AND a.desired_state<>'archived'",
                    )
                for job in blocked:
                    with tx(str(job["agent_id"])) as c:
                        c.execute(
                            "UPDATE lead.chat_jobs SET status='pending',error_code=NULL WHERE id=%s",
                            (job["id"],),
                        )
                        c.execute(
                            "UPDATE lead.conversation_messages SET status='pending' WHERE id=%s",
                            (job["message_id"],),
                        )
                        emit(
                            c,
                            str(job["agent_id"]),
                            "chat.enqueue",
                            {"job_id": str(job["id"])},
                        )
            key_was_configured = configured
            with tx() as c:
                events = all_rows(
                    c,
                    "SELECT * FROM lead.outbox_events WHERE published_at IS NULL ORDER BY occurred_at LIMIT 50",
                )
            for e in events:
                aid = str(e["agent_id"])
                payload = e["payload"]
                with tx(aid) as c:
                    agent = one(
                        c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,)
                    )
                if not agent or agent["desired_state"] == "archived":
                    with tx(aid) as c:
                        c.execute(
                            "UPDATE lead.outbox_events SET published_at=now() WHERE id=%s",
                            (e["id"],),
                        )
                    continue
                if e["event_type"] == "chat.enqueue":
                    await client.start_workflow(
                        ChatWorkflow.run,
                        aid,
                        id="chat:" + aid,
                        task_queue="lead",
                        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                        start_signal="enqueue",
                        start_signal_args=[payload["job_id"]],
                    )
                elif e["event_type"] == "supervisor.wake":
                    await client.start_workflow(
                        SupervisorWorkflow.run,
                        aid,
                        id="supervisor:" + aid,
                        task_queue="lead",
                        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                        start_signal="wake",
                    )
                elif e["event_type"] == "task.enqueue":
                    try:
                        await client.start_workflow(
                            PublishWorkflow.run,
                            {"agent_id": aid, "task_id": payload["task_id"]},
                            id="task:" + payload["task_id"],
                            task_queue="lead",
                            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        )
                    except WorkflowAlreadyStartedError:
                        pass
                with tx(aid) as c:
                    c.execute(
                        "UPDATE lead.outbox_events SET published_at=now() WHERE id=%s",
                        (e["id"],),
                    )
            (STATE / "worker-heartbeat").write_text(str(time.time()))
        except Exception:
            logging.warning(
                "Task delivery temporarily unavailable; durable outbox retained."
            )
        await asyncio.sleep(1)


async def main():
    client = await Client.connect(CONFIG["temporal_address"])
    worker = Worker(
        client,
        task_queue="lead",
        workflows=[ChatWorkflow, PublishWorkflow, SupervisorWorkflow],
        activities=[chat_job, execute_task, autonomous_cycle, mark_task_failed],
        max_concurrent_activities=12,
        max_concurrent_workflow_tasks=12,
    )
    print(
        "lead worker ready: independent Agent workflows, 12 activity slots.", flush=True
    )
    async with worker:
        await publish_outbox(client)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
