#!/usr/local/lib/lead-home-release/venv/bin/python3 -IB
"""Node-local read-only verifier using a separately installed SDK and TLS identity."""

import asyncio
import json
import re
import sys
from pathlib import Path

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import TLSConfig

CONFIG = Path("/etc/lead-home-release/temporal")


async def check(client, sha, invocation):
    if not re.fullmatch("[0-9a-f]{40}", sha) or not re.fullmatch("[0-9a-f]{32}", invocation):
        raise ValueError("invalid receipt identity")
    queue = "lead-home-probe-" + invocation
    handle = client.get_workflow_handle(queue + "-" + sha)
    info = await handle.describe()
    if (info.workflow_type, info.task_queue, info.status) != (
        "HomeReleaseProbe", queue, WorkflowExecutionStatus.COMPLETED
    ):
        raise ValueError("workflow not independently verified")
    result = await client.get_workflow_handle(info.id, run_id=info.run_id).result(follow_runs=False)
    if result != {"sha": sha, "invocation": invocation}:
        raise ValueError("workflow result mismatch")


async def main(sha, invocation, *, site=False):
    if site:
        payload = json.load(sys.stdin)
        address, namespace = "127.0.0.1:17233", "lead-prod"
        tls = TLSConfig(server_root_ca_cert=payload["ca"].encode(),
                        client_cert=payload["crt"].encode(), client_private_key=payload["key"].encode())
    else:
        address = (CONFIG / "address").read_text().strip()
        namespace = (CONFIG / "namespace").read_text().strip()
        tls = TLSConfig(server_root_ca_cert=(CONFIG / "ca.pem").read_bytes(),
                        client_cert=(CONFIG / "client.pem").read_bytes(), client_private_key=(CONFIG / "client.key").read_bytes())
    async with asyncio.timeout(10):
        client = await Client.connect(address, namespace=namespace, tls=tls)
        await check(client, sha, invocation)


if __name__ == "__main__":
    try:
        site = sys.argv[1:2] == ["--site-stdin"]
        asyncio.run(main(*(sys.argv[2:] if site else sys.argv[1:]), site=site))
    except Exception:
        sys.exit("durable workflow receipt unavailable or inconsistent")
