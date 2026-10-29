"""Real TCP outage and Temporal retry, without stopping the user's database."""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from lead_app.config import CONFIG, database_kwargs
from lead_app.workflows import PublishWorkflow
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Worker


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_WORKFLOW_TESTS") != "1",
    reason="real database transport and Temporal",
)
async def test_one_database_transport_outage_does_not_block_other_workflows():
    reservation = await asyncio.start_server(lambda *a: None, "127.0.0.1", 0)
    port = reservation.sockets[0].getsockname()[1]
    reservation.close()
    await reservation.wait_closed()
    kwargs = database_kwargs()
    attempts = []
    channels = set()

    async def relay(reader, writer):
        channels.add(writer)
        try:
            other_reader, other_writer = await asyncio.open_connection(
                kwargs["host"], kwargs["port"]
            )
            channels.add(other_writer)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()
                destination.close()

            await asyncio.gather(copy(reader, other_writer), copy(other_reader, writer))
        finally:
            writer.close()

    @activity.defn(name="execute_task")
    async def execute(args: dict):
        attempts.append(args["task_id"])
        selected = (
            {**kwargs, "port": port, "connect_timeout": 1}
            if args["task_id"] == "outage"
            else kwargs
        )

        def query():
            with psycopg.connect(**selected) as c:
                assert c.execute("SELECT 1").fetchone()[0] == 1

        await asyncio.to_thread(query)
        return {"state": "confirmed"}

    @activity.defn(name="mark_task_failed")
    async def failed(args: dict):
        raise AssertionError("Temporary database loss cannot discard the task")

    client = await Client.connect(CONFIG["temporal_address"])
    queue = "lead-transport-" + uuid4().hex
    handles = []
    server = None
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PublishWorkflow],
        activities=[execute, failed],
    ):
        try:
            for name in ("outage", "parallel-b", "parallel-c"):
                handles.append(
                    await client.start_workflow(
                        PublishWorkflow.run,
                        {"task_id": name},
                        id=queue + name,
                        task_queue=queue,
                        execution_timeout=timedelta(minutes=2),
                    )
                )
            await asyncio.wait_for(
                asyncio.gather(handles[1].result(), handles[2].result()), 10
            )
            assert attempts.count("outage") >= 1
            assert (await handles[0].describe()).status.name == "RUNNING"
            server = await asyncio.start_server(relay, "127.0.0.1", port)
            assert (await asyncio.wait_for(handles[0].result(), 30))[
                "state"
            ] == "confirmed"
            assert attempts.count("outage") >= 2
        finally:
            if server:
                server.close()
                await server.wait_closed()
            for channel in channels:
                channel.close()
            for handle in handles:
                try:
                    await handle.terminate("isolated transport verification cleanup")
                except Exception:
                    pass
