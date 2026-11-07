"""Independent data worker. It does not run social publishing or model tasks."""

import asyncio
import logging
import signal
from datetime import timedelta

from temporalio.worker import Worker

from .analytics_delivery import deliver_analytics
from .analytics_ingest import TASK_QUEUE, process_analytics_ingest
from .analytics_retention import maintain_all
from .identity_store import execution_tenants
from .temporal_client import connect
from .workflows import AnalyticsIngestWorkflow


async def dispatch(client, stop):
    while not stop.is_set():
        changed = 0
        try:
            for tenant in await asyncio.to_thread(execution_tenants):
                try:
                    changed += (
                        await deliver_analytics(
                            client,
                            str(tenant["id"])
                            if isinstance(tenant, dict)
                            else str(tenant),
                        )
                    ) or 0
                except Exception as error:
                    logging.warning(
                        "Analytics dispatch retry (%s)", type(error).__name__
                    )
        except Exception as error:
            logging.warning(
                "Analytics tenant discovery retry (%s)", type(error).__name__
            )
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.1 if changed else 2)
        except asyncio.TimeoutError:
            pass


async def retention(stop):
    while not stop.is_set():
        try:
            await asyncio.to_thread(maintain_all)
        except Exception as error:
            logging.warning("Analytics maintenance retry (%s)", type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass


async def main():
    client = await connect()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[AnalyticsIngestWorkflow],
        activities=[process_analytics_ingest],
        max_concurrent_activities=4,
        max_concurrent_workflow_tasks=4,
        graceful_shutdown_timeout=timedelta(seconds=5),
    )
    async with worker:
        jobs = [
            asyncio.create_task(dispatch(client, stop)),
            asyncio.create_task(retention(stop)),
        ]
        print("lead analytics worker ready", flush=True)
        await stop.wait()
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
