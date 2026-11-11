import asyncio
import hashlib
import logging
import signal
import time
from contextlib import AsyncExitStack
from datetime import timedelta

from temporalio import activity
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from .activities import autonomous_cycle as managed_autonomous_cycle
from .activities import chat_job, execute_task, mark_task_failed
from .admin_operations import execution_policy
from .analytics_ingest import TASK_QUEUE as ANALYTICS_QUEUE
from .analytics_ingest import process_analytics_ingest
from .async_db import run as run_db
from .atomic_dispatch import close_channel_atom, prepare_channel_atom
from .channel_delivery import channel_delivery_tick, park_channel_target
from .channel_research import channel_research_tick
from .config import STATE, any_model_configured, env, model_config
from .db import all_rows, emit, one
from .db import pooled_tx as tx
from .identity_store import execution_tenants
from .resident_ownership import resident_cycle_agent_ids
from .scope import service_scope, tenant_scope
from .temporal_client import connect
from .workflows import (
    AnalyticsIngestWorkflow,
    ChannelDeliveryWorkflow,
    ChannelPreparationWorkflow,
    ChatWorkflow,
    PublishWorkflow,
    SupervisorWorkflow,
)


@activity.defn(name="autonomous_cycle")
async def autonomous_cycle(args: dict):
    # Route before scoped/managed execution acquires any shared business resources.
    # A resident owns only this supervisor cycle, never channel.clock delivery queues.
    if str(args["agent_id"]) in resident_cycle_agent_ids(env()):
        return {"delay": 300, "cadence_version": 1, "reason": "device_resident_owns_cycle"}
    return await managed_autonomous_cycle(args)


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
        return all_rows(c, """WITH picked AS (SELECT o.id FROM lead.outbox_events o LEFT JOIN lead.agents a ON a.id=o.agent_id
            WHERE o.published_at IS NULL AND o.event_type<>'analytics.ingest' AND o.available_at<=now()
            AND (o.lease_until IS NULL OR o.lease_until<now())
            ORDER BY (a.desired_state='archived'),o.occurred_at LIMIT 20 FOR UPDATE OF o SKIP LOCKED)
            UPDATE lead.outbox_events o SET lease_token=gen_random_uuid(),lease_until=now()+interval '2 minutes',
            delivery_attempts=delivery_attempts+1 FROM picked WHERE o.id=picked.id RETURNING o.*""")


def outbox_agent(aid):
    with tx(aid) as c:
        return one(c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,))


def ack_outbox(aid, eid, lease=None):
    with tx(aid) as c:
        c.execute("UPDATE lead.outbox_events SET published_at=now(),delivery_error=NULL,lease_token=NULL,lease_until=NULL WHERE id=%s AND (%s::uuid IS NULL OR lease_token=%s)", (eid, lease, lease))
        from .runtime_issues import resolve
        resolve(c, aid, "delivery:" + str(eid))


def defer_outbox(aid, event, error):
    with tx(aid) as c:
        c.execute("""UPDATE lead.outbox_events SET delivery_error=%s,lease_token=NULL,lease_until=NULL,
            available_at=now()+make_interval(secs=>least(300,power(2,least(delivery_attempts,8))::integer))
            WHERE id=%s AND lease_token=%s""", (type(error).__name__, event["id"], event.get("lease_token")))
        from .runtime_issues import report
        report(c, aid, "delivery:" + str(event["id"]), type(error).__name__,
               "这项任务暂未送达执行器，已保留并自动重试。", retry_seconds=min(300, 2 ** min(event.get("delivery_attempts", 1), 8)))


async def start_workflow(client, *args, **kwargs):
    # Workflow IDs make an ambiguous RPC retry safe; never retry external sends here.
    return await asyncio.wait_for(client.start_workflow(*args, **kwargs), timeout=5)


async def dispatch_channel_event(client, aid, payload):
    args = {"agent_id": aid, "task_id": payload["task_id"]}
    workflow_id = "dispatch-channel:" + args["task_id"]
    if "generation" in payload:
        args["generation"] = payload["generation"]
        workflow_id += ":" + str(args["generation"])
    try:
        await start_workflow(client,
            ChannelPreparationWorkflow.run, args, id=workflow_id, task_queue="lead-offline",
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        pass
    return client.get_workflow_handle(workflow_id)


async def deliver_workspace(client, tenant, configured, key_was_configured):
    with tenant_scope(tenant, None), service_scope():
        if configured and not key_was_configured:
            await run_db(model_became_available)
        from .cadence_events import reconcile_source_config
        await run_db(reconcile_source_config)
        events = await run_db(outbox_rows)
        async def deliver_event(e):
            try:
                aid = str(e["agent_id"])
                payload = e["payload"]
                agent = await run_db(outbox_agent, aid)
                if agent and agent["desired_state"] == "paused" and e["event_type"] == "supervisor.wake":
                    await run_db(ack_outbox, aid, e["id"], e.get("lease_token"))
                    return
                if not agent or (agent["desired_state"] == "archived" and e["event_type"] != "dispatch.channel"):
                    if agent and e["event_type"] == "supervisor.wake":
                        try:
                            await asyncio.wait_for(
                                client.get_workflow_handle("supervisor:" + aid).signal("wake"),
                                timeout=5,
                            )
                        except RPCError as error:
                            if error.status != RPCStatusCode.NOT_FOUND:
                                raise
                    await run_db(ack_outbox, aid, e["id"], e.get("lease_token"))
                    return
                if e["event_type"] == "chat.enqueue":
                    await start_workflow(client,
                        ChatWorkflow.run,
                        aid,
                        id="chat:" + aid,
                        task_queue="lead",
                        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                        start_signal="enqueue",
                        start_signal_args=[payload["job_id"]],
                    )
                elif e["event_type"] == "supervisor.wake":
                    await start_workflow(client,
                        SupervisorWorkflow.run,
                        aid,
                        id="supervisor:" + aid,
                        task_queue="lead",
                        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                        start_signal="wake",
                    )
                elif e["event_type"] == "channel.clock":
                    try:
                        await start_workflow(client,
                            ChannelDeliveryWorkflow.run, {**payload,"agent_id":aid},
                            id=f"channel-clock:{payload['task_id']}:{payload['generation']}",
                            task_queue="lead-research" if payload.get("lane")=="research" else "lead-delivery", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                        )
                    except WorkflowAlreadyStartedError:
                        pass
                elif e["event_type"] == "channel.clock.wake":
                    try:
                        await client.get_workflow_handle(
                            f"channel-clock:{payload['task_id']}:{payload['generation']}"
                        ).signal("wake", payload["policy_version"])
                    except RPCError as error:
                        if error.status != RPCStatusCode.NOT_FOUND:
                            raise
                    # Never create a replacement clock from a wake event.
                elif e["event_type"] == "dispatch.channel":
                    await dispatch_channel_event(client, aid, payload)
                elif e["event_type"] in ("task.enqueue", "task.reconcile"):
                    try:
                        await start_workflow(client,
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
                await run_db(ack_outbox, aid, e["id"], e.get("lease_token"))
            except Exception as error:
                await run_db(defer_outbox, str(e["agent_id"]), e, error)
                logging.warning("Individual task delivery unavailable (%s)", type(error).__name__)

        groups = {}
        for event in sorted(events, key=lambda e: (e["occurred_at"], str(e["id"]))):
            # Keep conversation signals ordered; independent tasks can dispatch concurrently.
            key = str(event["agent_id"]) + ":chat" if event["event_type"] == "chat.enqueue" else str(event["id"])
            groups.setdefault(key, []).append(event)
        slots = asyncio.Semaphore(4)
        async def deliver_group(rows):
            async with slots:
                for event in rows:
                    await deliver_event(event)
        await asyncio.gather(*(deliver_group(rows) for rows in groups.values()))



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
    external_delivery_tasks = {}
    notification_slots = asyncio.Semaphore(3)

    async def deliver_external(tenant):
        from .admin_delivery import dispatch

        with tenant_scope(tenant, None):
            async with notification_slots:
                await dispatch(limit=5)

    auxiliary_tasks = {}

    async def auxiliary(tenant, kind):
        try:
            with tenant_scope(tenant, None), service_scope():
                if kind == "analytics":
                    from .analytics_delivery import deliver_analytics
                    await deliver_analytics(client, tenant)
                elif kind == "notifications":
                    from .admin_notifications import collect
                    await asyncio.to_thread(collect)
                elif kind == "external_notifications":
                    from .admin_delivery import enqueue_pending
                    await asyncio.to_thread(enqueue_pending)
                    await deliver_external(tenant)
                elif kind == "reddit_review":
                    from .reddit_workspace import review_enabled
                    await asyncio.to_thread(review_enabled, tenant)
                else:
                    from .admin_experiences import collect
                    await asyncio.to_thread(collect)
        except Exception as error:
            logging.warning("Auxiliary %s unavailable (%s)", kind, type(error).__name__)

    use_release_probe = env().get("LEAD_HOME_RELEASE_PROBE") == "1"
    credential_state = None
    try:
        while True:
            try:
                if use_release_probe and await asyncio.to_thread(release_fenced):
                    (STATE / "worker-heartbeat").write_text(str(time.time()))
                    await asyncio.sleep(poll_seconds)
                    continue
                configured = any_model_configured()
                current_credentials = tuple(
                    hashlib.sha256(model_config(provider)["key"].encode()).digest()
                    for provider in ("deepseek", "openai")
                )
                if not configured or current_credentials != credential_state:
                    configured_workspaces.clear()
                if current_credentials != credential_state:
                    workspace_failures.clear()
                    workspace_retry_at.clear()
                credential_state = current_credentials
                for tenant in await asyncio.to_thread(execution_tenants):
                    for kind, interval in (("analytics", 5), ("notifications", 5), ("external_notifications", 5), ("experiences", 600), ("reddit_review", 60)):
                        if kind == "analytics" and env().get("LEAD_ANALYTICS_DEDICATED") == "1":
                            continue
                        key = (tenant, kind)
                        prior = auxiliary_tasks.get(key)
                        if (prior is None or prior.done()) and time.monotonic() - notification_ticks.get(key, 0) >= interval:
                            auxiliary_tasks[key] = asyncio.create_task(auxiliary(tenant, kind))
                            notification_ticks[key] = time.monotonic()
                    if time.monotonic() < workspace_retry_at.get(tenant, 0):
                        continue
                    try:
                        await deliver_workspace(
                            client, tenant, configured, tenant in configured_workspaces
                        )
                        if configured:
                            configured_workspaces.add(tenant)
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
    finally:
        tasks = list(auxiliary_tasks.values()) + list(external_delivery_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            from asyncio import gather
            await gather(*tasks, return_exceptions=True)


async def await_release_window(stop):
    # Attest the new boot before polling business workflow histories. A failed
    # cutover can then return to the old queues without recording new commands.
    while await asyncio.to_thread(release_fenced):
        (STATE / "worker-heartbeat").write_text(str(time.time()))
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
            return False
        except asyncio.TimeoutError:
            pass
    return not stop.is_set()


async def main():
    # Fail closed before connecting or starting any queue when operator routing is invalid.
    resident_cycle_agent_ids(env())
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
    # Reserved activity capacity prevents long research from occupying all send/chat slots.
    publish_worker = Worker(client, task_queue="lead-publish",
        activities=[execute_task, mark_task_failed, execution_policy],
        max_concurrent_activities=max(1, int(env().get("LEAD_PUBLISH_SLOTS") or 3)),
        graceful_shutdown_timeout=timedelta(seconds=5))
    chat_worker = Worker(client, task_queue="lead-chat", activities=[chat_job],
        max_concurrent_activities=max(1, int(env().get("LEAD_CHAT_SLOTS") or 2)),
        graceful_shutdown_timeout=timedelta(seconds=5))
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
    if env().get("LEAD_HOME_RELEASE_PROBE") == "1":
        from .home_release_probe import prove_boot

        await prove_boot(client)
        if not await await_release_window(stop):
            return
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(worker)
        await stack.enter_async_context(Worker(
            client, task_queue="lead-offline", workflows=[ChannelPreparationWorkflow],
            activities=[prepare_channel_atom, close_channel_atom],
            max_concurrent_activities=2 if env().get("LEAD_CHANNEL_ATOM_AGENT_IDS") else 1, max_concurrent_workflow_tasks=2,
        ))
        await stack.enter_async_context(Worker(
            client, task_queue="lead-delivery", workflows=[ChannelDeliveryWorkflow],
            activities=[channel_delivery_tick, park_channel_target], max_concurrent_activities=2,
        ))
        await stack.enter_async_context(Worker(
            client, task_queue="lead-research", workflows=[ChannelDeliveryWorkflow],
            activities=[channel_research_tick, park_channel_target], max_concurrent_activities=2,
        ))
        await stack.enter_async_context(publish_worker)
        await stack.enter_async_context(chat_worker)
        if env().get("LEAD_ANALYTICS_DEDICATED") != "1":
            await stack.enter_async_context(analytics_worker)
        delivery = asyncio.create_task(publish_outbox(client))
        try:
            await stop.wait()
        finally:
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
