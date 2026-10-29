import asyncio
import hashlib
import json
import os
import shutil
import time

import httpx
import psutil
from lead_app.config import ROOT, STATE
from lead_app.ops import credential


def source_hash():
    value = hashlib.sha256()
    paths = (
        list((ROOT / "services/api").rglob("*.py"))
        + list((ROOT / "scripts").glob("*.py"))
        + list((ROOT / "tests/fixtures").rglob("*.html"))
        + list((ROOT / "tests").glob("*.py"))
    )
    for path in sorted(paths):
        value.update(str(path.relative_to(ROOT)).encode())
        value.update(path.read_bytes())
    return value.hexdigest()


def resource_sample(previous):
    ids = {os.getpid()}
    try:
        ids |= set(json.loads((STATE / "processes.json").read_text()).values())
    except (OSError, ValueError):
        pass
    processes = {}
    for identifier in ids:
        try:
            parent = psutil.Process(int(identifier))
            for process in [parent] + parent.children(recursive=True):
                processes[process.pid] = process
        except psutil.Error:
            pass
    rss = fds = 0
    delta = 0.0
    current = {}
    for process in processes.values():
        try:
            key = (process.pid, process.create_time())
            usage = process.cpu_times()
            current[key] = usage.user + usage.system
            if key in previous:
                delta += max(0, current[key] - previous[key])
            rss += process.memory_info().rss
            fds += process.num_fds()
        except psutil.Error:
            pass
    previous.clear()
    previous.update(current)
    return {
        "rss_bytes": rss,
        "file_descriptors": fds,
        "process_count": len(current),
        "cpu_delta_seconds": delta,
        "disk_free_bytes": shutil.disk_usage(STATE).free,
    }


async def sample(report, stopping, output):
    previous = {}
    beginning = time.monotonic()
    initial = report.get("observed_seconds", 0)
    async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
        while not stopping.is_set():
            item = resource_sample(previous)
            item.update(
                elapsed_seconds=initial + time.monotonic() - beginning,
                phase=report.get("phase", "starting"),
                sampled_at=time.time(),
            )
            start = time.monotonic()
            try:
                response = await client.get(
                    "http://127.0.0.1:8088/internal/status",
                    headers={"Authorization": "Bearer " + credential()},
                )
                response.raise_for_status()
                item["api_ms"] = (time.monotonic() - start) * 1000
            except Exception as e:
                item.update(api_ms=None, api_error=type(e).__name__)
            with output.open("a") as stream:
                os.chmod(output, 0o600)
                stream.write(json.dumps(item) + "\n")
            try:
                await asyncio.wait_for(stopping.wait(), 5)
            except asyncio.TimeoutError:
                pass
