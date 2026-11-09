"""A synthetic workflow on a per-boot queue. It has no business activities."""

import os
import re
from datetime import timedelta
from pathlib import Path

from temporalio import activity, workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.worker import Worker

REVISION = Path("/run/lead-home/release-revision")


@workflow.defn
class HomeReleaseProbe:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity("home_release_identity", start_to_close_timeout=timedelta(seconds=5))


@activity.defn(name="home_release_identity")
async def identity() -> dict:
    sha = REVISION.read_text().strip()
    invocation = os.environ["INVOCATION_ID"]
    if not re.fullmatch("[0-9a-f]{40}", sha) or not re.fullmatch("[0-9a-f]{32}", invocation):
        raise ValueError("unrecognized worker identity")
    return {"sha": sha, "invocation": invocation}


async def prove_boot(client):
    observed = await identity()
    queue = "lead-home-probe-" + observed["invocation"]
    async with Worker(client, task_queue=queue, workflows=[HomeReleaseProbe], activities=[identity]):
        result = await client.execute_workflow(
            HomeReleaseProbe.run, id=queue + "-" + observed["sha"], task_queue=queue,
            execution_timeout=timedelta(seconds=40), id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )
    if result != observed:
        raise ValueError("synthetic worker receipt mismatch")
