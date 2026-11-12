import asyncio
import os
from datetime import datetime, timedelta, timezone

import pytest
from lead_app.db import uid
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker


@pytest.mark.asyncio
async def test_fixed_ref_delivery_restart_and_replay():
    from lead_app.workflows import GrowthWorkWorkflow
    address = os.environ.get("LEAD_TEST_CHANNEL_TEMPORAL")
    assert address, "An isolated Temporal service is required"
    client = await Client.connect(address)
    ref = {"agent_id": uid(), "task_id": uid(), "task_generation": 1}
    ready = False
    claims, executions, completions = [], [], []
    deadline = datetime.now(timezone.utc)+timedelta(seconds=2)

    @activity.defn(name="growth_advance")
    async def advance(args: dict):
        assert args == ref
        claims.append(args)
        if completions:
            return {"state": "terminal"}
        if not ready:
            return {"state": "wait", "wait": {"check_at": deadline.isoformat()}}
        return {"state": "ready", "claim": {"ref": args, "kind": "research", "attempt_token": uid()}}

    @activity.defn(name="growth_execute")
    async def execute(args: dict):
        executions.append(args)
        return {"outcome": "completed"}

    @activity.defn(name="growth_finish")
    async def finish(args: dict):
        completions.append(args)
        return {"accepted_control": True}

    queue = uid()
    async with Worker(client, task_queue=queue, workflows=[GrowthWorkWorkflow], activities=[advance, execute, finish]):
        handle = await client.start_workflow(GrowthWorkWorkflow.run, ref, id=uid(), task_queue=queue)
        while not claims:
            await asyncio.sleep(.02)
    ready = True
    async with Worker(client, task_queue=queue, workflows=[GrowthWorkWorkflow], activities=[advance, execute, finish]):
        await asyncio.wait_for(handle.result(), 12)
    assert len(executions) == len(completions) == 1
    await Replayer(workflows=[GrowthWorkWorkflow]).replay_workflow(await handle.fetch_history())


@pytest.mark.asyncio
async def test_long_wait_time_skipping_continues_and_replays():
    from pathlib import Path

    from lead_app.config import STATE
    from lead_app.workflows import GrowthWorkWorkflow
    from temporalio.testing import WorkflowEnvironment
    assert os.environ.get("LEAD_GROWTH_TEST_ENV")=="1"
    folder = Path(STATE).parent/"test-server"
    folder.mkdir(exist_ok=True)
    advances = executions = 0
    async with await WorkflowEnvironment.start_time_skipping(download_dest_dir=str(folder)) as env:
        first_time = await env.get_current_time()
        @activity.defn(name="growth_advance")
        async def advance(args:dict):
            nonlocal advances
            advances += 1
            if executions:
                return {"state":"terminal"}
            if advances<=104:
                return {"state":"wait","wait":{"check_at":((await env.get_current_time())+timedelta(days=30)).isoformat()}}
            return {"state":"ready","claim":{"ref":args,"kind":"observe","attempt_token":uid()}}
        @activity.defn(name="growth_execute")
        async def execute(args:dict):
            nonlocal executions
            executions += 1
            return {"outcome":"completed"}
        @activity.defn(name="growth_finish")
        async def finish(args:dict):
            return {"accepted_control":True}
        queue,identity = uid(),uid()
        async with Worker(env.client,task_queue=queue,workflows=[GrowthWorkWorkflow],activities=[advance,execute,finish]):
            handle = await env.client.start_workflow(GrowthWorkWorkflow.run,{"agent_id":uid(),"task_id":uid(),"task_generation":1},id=identity,task_queue=queue)
            result = await handle.result()
        histories,run_id = [],handle.first_execution_run_id
        while run_id:
            history = await env.client.get_workflow_handle(identity,run_id=run_id).fetch_history()
            histories.append(history)
            continued = [e.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                for e in history.events if e.HasField("workflow_execution_continued_as_new_event_attributes")]
            run_id = continued[0] if continued else None
        assert result["state"]=="terminal" and executions==1 and len(histories)==2
        assert (await env.get_current_time())-first_time>=timedelta(days=30*104)
        for history in histories:
            await Replayer(workflows=[GrowthWorkWorkflow]).replay_workflow(history)
