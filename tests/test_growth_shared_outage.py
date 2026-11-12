import asyncio
import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from lead_app import domain
from lead_app.config import ROOT, STATE
from lead_app.db import uid
from lead_app.growth import runtime
from lead_app.growth.contracts import utcnow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker
from test_growth_runtime import new_work


def test_shared_database_failure_does_not_create_new_business_identity(growth_agent,monkeypatch):
    other = domain.create_agent(runtime_engine="growth")
    try:
        refs = [new_work(aid) for aid in (growth_agent,other)]
        original = runtime.tx
        def unavailable(*args,**kwargs):
            raise ConnectionError("Synthetic shared database interruption")
        monkeypatch.setattr(runtime,"tx",unavailable)
        for ref in refs:
            with pytest.raises(ConnectionError):
                runtime.advance(ref)
        monkeypatch.setattr(runtime,"tx",original)
        for ref in refs:
            claim=runtime.advance(ref)["claim"]
            assert claim["ref"]==ref
            assert runtime.advance(ref)["state"]=="wait"
    finally:
        domain.set_control(other,"archive")


async def test_real_queue_server_restart_resumes_both_original_projects(tmp_path):
    from lead_app.workflows import GrowthWorkWorkflow
    binary = shutil.which("temporal")
    if not binary:
        common = __import__("subprocess").check_output(["git","rev-parse","--git-common-dir"],cwd=ROOT,text=True).strip()
        candidate=(ROOT/common).resolve().parent/".state/tools/temporal"
        binary=str(candidate) if candidate.is_file() else None
    kwargs={"dev_server_database_filename":str(tmp_path/"queue.sqlite"),
        "download_dest_dir":str(Path(STATE).parent/"test-server"),"dev_server_log_level":"error"}
    if binary:
        kwargs["dev_server_existing_path"]=binary
    seen,executed,completed=set(),[],set()
    deadline=utcnow()+timedelta(seconds=3)
    @activity.defn(name="growth_advance")
    async def advance(ref:dict):
        seen.add(ref["task_id"])
        if ref["task_id"] in completed:
            return {"state":"terminal"}
        if utcnow()<deadline:
            return {"state":"wait","wait":{"check_at":deadline.isoformat()}}
        return {"state":"ready","claim":{"ref":ref,"kind":"research","attempt_token":uid()}}
    @activity.defn(name="growth_execute")
    async def execute(args:dict):
        executed.append(args["claim"]["ref"]["task_id"])
        return {"outcome":"completed"}
    @activity.defn(name="growth_finish")
    async def finish(args:dict):
        completed.add(args["claim"]["ref"]["task_id"])
        return {"accepted_control":True}
    queue=uid()
    refs=[{"agent_id":uid(),"task_id":uid(),"task_generation":1} for _ in range(2)]
    names=[]
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        async with Worker(env.client,task_queue=queue,workflows=[GrowthWorkWorkflow],activities=[advance,execute,finish]):
            for ref in refs:
                name=uid()
                names.append(name)
                await env.client.start_workflow(GrowthWorkWorkflow.run,ref,id=name,task_queue=queue)
            async with asyncio.timeout(8):
                while len(seen)<2:
                    await asyncio.sleep(.02)
        # Exiting really stops this test's queue process, leaving its SQLite.
    async with await WorkflowEnvironment.start_local(**kwargs) as env:
        async with Worker(env.client,task_queue=queue,workflows=[GrowthWorkWorkflow],activities=[advance,execute,finish]):
            handles=[env.client.get_workflow_handle(name) for name in names]
            await asyncio.wait_for(asyncio.gather(*(handle.result() for handle in handles)),15)
        for handle in handles:
            await Replayer(workflows=[GrowthWorkWorkflow]).replay_workflow(await handle.fetch_history())
    assert sorted(executed)==sorted(ref["task_id"] for ref in refs)
