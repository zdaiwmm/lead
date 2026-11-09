import asyncio
import logging
import signal
import time
from contextlib import AsyncExitStack
from datetime import timedelta

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from .activities import autonomous_cycle, chat_job, execute_task, mark_task_failed
from .admin_operations import execution_policy
from .analytics_ingest import TASK_QUEUE as ANALYTICS_QUEUE
from .analytics_ingest import process_analytics_ingest
from .async_db import run as run_db
from .config import STATE, any_model_configured, env, model_config
from .db import all_rows, emit, one
from .db import pooled_tx as tx
from .identity_store import execution_tenants
from .scope import service_scope, tenant_scope
from .temporal_client import connect
from .workflows import (
    AnalyticsIngestWorkflow,
    ChatWorkflow,
    PublishWorkflow,
    SupervisorWorkflow,
)


def model_became_available():
    with tx() as c:
        running = all_rows(
            c, "SELECT id FROM lead.agents WHERE desired_state='running'"
        )
        for agent in running:
            emit(c, str(agent["id"]), "supervisor.wake")
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


def outbox_rows():
    with tx() as c:
        return all_rows(c, "SELECT * FROM lead.outbox_events WHERE published_at IS NULL AND event_type<>'analytics.ingest' ORDER BY occurred_at LIMIT 50")


def outbox_agent(aid):
    with tx(aid) as c:
        return one(c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,))


def ack_outbox(aid, eid):
    with tx(aid) as c:
        c.execute("UPDATE lead.outbox_events SET published_at=now() WHERE id=%s", (eid,))


async def deliver_workspace(client, tenant, configured, key_was_configured):
    from .analytics_delivery import deliver_analytics

    if env().get("LEAD_ANALYTICS_DEDICATED") != "1":
        await deliver_analytics(client, tenant)
    with tenant_scope(tenant, None), service_scope():
        if configured and not key_was_configured:
            await run_db(model_became_available)
        events = await run_db(outbox_rows)
        for e in events:
            aid = str(e["agent_id"])
            payload = e["payload"]
            agent = await run_db(outbox_agent, aid)
            if agent and agent["desired_state"] == "paused" and e["event_type"] == "supervisor.wake":
                await run_db(ack_outbox, aid, e["id"])
                continue
            if not agent or agent["desired_state"] == "archived":
                if agent and e["event_type"] == "supervisor.wake":
                    try:
                        await client.get_workflow_handle("supervisor:" + aid).signal(
                            "wake"
                        )
                    except RPCError as error:
                        if error.status != RPCStatusCode.NOT_FOUND:
                            raise
                await run_db(ack_outbox, aid, e["id"])
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
            elif e["event_type"] in ("task.enqueue", "task.reconcile"):
                try:
                    await client.start_workflow(
                        PublishWorkflow.run,
                        {"agent_id": aid, "task_id": payload["task_id"]},
                        id=(
                            "task-reconcile:"
                            + payload["task_id"]
                            + ":"
                            + payload["recovery_id"]
                        )
                        if e["event_type"] == "task.reconcile"
                        else "task:" + payload["task_id"],
                        task_queue="lead",
                        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    )
                except WorkflowAlreadyStartedError:
                    pass
            await run_db(ack_outbox, aid, e["id"])


def release_fenced():
    # Admission remains enforced by PostgreSQL. Avoid queueing ordinary outbox updates
    # behind the guardian while systemd verifies the newly started worker.
    with tx() as conn:
        return one(conn, """SELECT EXISTS (
            SELECT FROM pg_locks l JOIN pg_stat_activity a ON l.pid=a.pid
            WHERE l.database=a.datid AND a.datname=current_database()
              AND a.usename='lead-release-fence' AND starts_with(a.application_name,'lead-release-')
              AND l.relation='lead.agents'::regclass AND l.mode='ShareLock' AND l.granted
        ) AS held""")["held"]


async def publish_outbox(client):
    poll_seconds = max(1, float(env().get("LEAD_OUTBOX_POLL_SECONDS") or 1))
    configured_workspaces = set()
    workspace_failures = {}
    workspace_retry_at = {}
    notification_ticks = {}
    experience_ticks = {}
    external_delivery_tasks = {}
    notification_slots = asyncio.Semaphore(3)

    async def deliver_external(tenant):
        from .admin_delivery import dispatch

        with tenant_scope(tenant, None):
            async with notification_slots:
                await dispatch(limit=5)

    use_release_probe = env().get("LEAD_HOME_RELEASE_PROBE") == "1"
    credential_state = None
    while True:
        try:
            if use_release_probe and await asyncio.to_thread(release_fenced):
                (STATE / "worker-heartbeat").write_text(str(time.time()))
                await asyncio.sleep(poll_seconds)
                continue
            configured = any_model_configured()
            current_credentials = tuple(
                bool(model_config(provider)["key"])
                for provider in ("deepseek", "openai")
            )
            if not configured or current_credentials != credential_state:
                configured_workspaces.clear()
            if current_credentials != credential_state:
                workspace_failures.clear()
                workspace_retry_at.clear()
            credential_state = current_credentials
            for tenant in await asyncio.to_thread(execution_tenants):
                if time.monotonic() < workspace_retry_at.get(tenant, 0):
                    continue
                try:
                    await deliver_workspace(
                        client, tenant, configured, tenant in configured_workspaces
                    )
                    if configured:
                        configured_workspaces.add(tenant)
                    if time.monotonic() - notification_ticks.get(tenant, 0) >= 30:
                        from .admin_notifications import collect

                        with tenant_scope(tenant, None):
                            await asyncio.to_thread(collect)
                            previous = external_delivery_tasks.get(tenant)
                            if previous is None or previous.done():
                                if previous is not None:
                                    previous.exception()
                                external_delivery_tasks[tenant] = asyncio.create_task(
                                    deliver_external(tenant)
                                )
                        notification_ticks[tenant] = time.monotonic()
                    if time.monotonic() - experience_ticks.get(tenant, 0) >= 600:
                        from .admin_experiences import collect as collect_experiences

                        with tenant_scope(tenant, None):
                            await asyncio.to_thread(collect_experiences)
                        experience_ticks[tenant] = time.monotonic()
                    workspace_failures.pop(tenant, None)
                    workspace_retry_at.pop(tenant, None)
                except Exception:
                    failures = workspace_failures.get(tenant, 0) + 1
                    workspace_failures[tenant] = failures
                    delay = min(60, 2 ** min(failures, 6))
                    workspace_retry_at[tenant] = time.monotonic() + delay
                    logging.warning(
                        "Workspace delivery unavailable; retry in %s seconds; other workspaces continue.",
                        delay,
                    )
            (STATE / "worker-heartbeat").write_text(str(time.time()))
        except Exception:
            logging.warning(
                "Task delivery temporarily unavailable; durable outbox retained."
            )
        await asyncio.sleep(poll_seconds)


async def main():
    client = await connect()
    activity_slots = max(1, int(env().get("LEAD_ACTIVITY_SLOTS") or 12))
    workflow_slots = max(1, int(env().get("LEAD_WORKFLOW_SLOTS") or 12))
    worker = Worker(
        client,
        task_queue="lead",
        workflows=[
            ChatWorkflow,
            PublishWorkflow,
            SupervisorWorkflow,
        ],
        activities=[
            chat_job,
            execute_task,
            autonomous_cycle,
            mark_task_failed,
            execution_policy,
        ],
        max_concurrent_activities=activity_slots,
        max_concurrent_workflow_tasks=workflow_slots,
        graceful_shutdown_timeout=timedelta(seconds=5),
    )
    analytics_worker = Worker(
        client,
        task_queue=ANALYTICS_QUEUE,
        workflows=[AnalyticsIngestWorkflow],
        activities=[process_analytics_ingest],
        max_concurrent_activities=4,
        max_concurrent_workflow_tasks=4,
        graceful_shutdown_timeout=timedelta(seconds=5),
    )
    print(
        f"lead worker ready: {activity_slots} activity slots, {workflow_slots} workflow slots.", flush=True
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(worker)
        if env().get("LEAD_ANALYTICS_DEDICATED") != "1":
            await stack.enter_async_context(analytics_worker)
        if env().get("LEAD_HOME_RELEASE_PROBE") == "1":
            from .home_release_probe import prove_boot

            await prove_boot(client)
        delivery = asyncio.create_task(publish_outbox(client))
        try:
            await stop.wait()
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
