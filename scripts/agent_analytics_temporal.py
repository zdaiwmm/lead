"""Run the dedicated analytics queue for a single agent on the restored Temporal namespace."""
import argparse
import asyncio
import signal
from uuid import UUID

from lead_app.analytics_delivery import deliver_scope
from lead_app.analytics_ingest import TASK_QUEUE, process_analytics_ingest, scoped_tx
from lead_app.analytics_reporting import refresh_scope
from lead_app.db import require_agent, tx
from lead_app.identity_store import agent_tenant
from lead_app.scope import service_scope, tenant_scope
from lead_app.temporal_client import connect
from lead_app.workflows import AnalyticsIngestWorkflow
from recover_agent_analytics import ensure_live_day
from temporalio import activity
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import (
    DescribeNamespaceRequest,
    DescribeTaskQueueRequest,
)
from temporalio.worker import Worker


async def run(aid):
    tenant = agent_tenant(aid)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    with service_scope(), tenant_scope(tenant, None):
        with tx(aid) as c:
            require_agent(c, aid)
        client = await connect()
        queue = "lead-analytics-" + aid
        if TASK_QUEUE != queue:
            raise ValueError("target analytics queue mismatch")
        await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=client.namespace))

        @activity.defn(name="process_analytics_ingest")
        async def target_ingest(args: dict):
            if (args.get("agent_id"), args.get("tenant_id")) != (aid, tenant):
                return {"state": "scope_mismatch"}
            return await process_analytics_ingest(args)

        worker = Worker(client, task_queue=queue, workflows=[AnalyticsIngestWorkflow],
                        activities=[target_ingest], max_concurrent_activities=4,
                        max_concurrent_workflow_tasks=4)
        async with worker:
            async with asyncio.timeout(30):
                while True:
                    info = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                        namespace=client.namespace, task_queue=TaskQueue(name=queue),
                        task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY,
                    ))
                    if info.pollers:
                        break
                    await asyncio.sleep(1)
            print("ANALYTICS_TEMPORAL_READY", queue, flush=True)
            while not stop.is_set():
                with scoped_tx(tenant, aid, "live") as c:
                    ensure_live_day(c)
                    tests = c.execute("SELECT id FROM lead.pixel_tests WHERE agent_id=%s", (aid,)).fetchall()
                changed = 0
                for scope in ["live", *(str(row["id"]) for row in tests)]:
                    changed += await deliver_scope(client, tenant, aid, scope)
                    changed += len(await asyncio.to_thread(refresh_scope, tenant, aid, scope))
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1 if changed else 5)
                except TimeoutError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True, type=UUID)
    asyncio.run(run(str(parser.parse_args().agent)))
