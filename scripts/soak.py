"""Resumable controlled load: real Temporal, PostgreSQL, file decode and browsers.

No social login, external publication, synthetic acquisition, or repeated model bill.
The private verification workspace is separate from the user's workspace.
"""

import argparse
import asyncio
import fcntl
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import av
import httpx
import psutil
import psycopg
from lead_app import assets, domain
from lead_app.config import ROOT, STATE, database_kwargs
from lead_app.db import all_rows, one, tx
from lead_app.ops import atomic_json, collect
from lead_app.scope import service_scope, tenant_scope
from lead_app.security import internal_token
from PIL import Image
from starlette.datastructures import UploadFile

FOLDER = STATE / "endurance"
REPORT = FOLDER / "current.json"
PID = FOLDER / "runner.pid"


def read():
    return json.loads(REPORT.read_text()) if REPORT.exists() else {}


def reclaim_profiles(report):
    """Only profiles created by this completed verification run are eligible."""
    if report.get("state") != "passed":
        return 0
    count = 0
    with psycopg.connect(**database_kwargs(True)) as c:
        eligible = {
            str(row[0])
            for row in c.execute(
                "SELECT a.id FROM lead.agents a JOIN lead.tenants t ON t.id=a.tenant_id WHERE t.id=%s AND t.name=%s AND a.desired_state='archived'",
                (report["tenant_id"], "Verification " + report["run_id"]),
            ).fetchall()
        }
    for aid in set(report["agent_ids"]) & eligible:
        folder = STATE / "profiles" / aid
        storage = folder / "storage"
        if not storage.is_dir() or storage.is_symlink():
            continue
        with (folder / "actor.lock").open("a") as held:
            try:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            shutil.rmtree(storage)
            count += 1
    return count


def running():
    try:
        process = psutil.Process(int(PID.read_text()))
        args = process.cmdline()
        return (
            process.pid
            if str(Path(__file__).resolve()) in args and "run" in args
            else None
        )
    except (OSError, ValueError, psutil.Error):
        return None


def sample_video():
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp4") as container:
        stream = container.add_stream("libx264", rate=12)
        stream.width = stream.height = 64
        stream.pix_fmt = "yuv420p"
        for index in range(12):
            frame = av.VideoFrame.from_image(
                Image.new("RGB", (64, 64), (index * 15, 90, 60))
            )
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buffer.getvalue()


def initialize(hours, interval):
    old = read()
    if old.get("state") in ("running", "starting", "interrupted"):
        return
    FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    if old:
        atomic_json(FOLDER / (old["run_id"] + ".json"), old)
    identifier = str(uuid4())
    atomic_json(
        REPORT,
        {
            "run_id": identifier,
            "tenant_id": str(uuid4()),
            "agent_ids": [],
            "state": "starting",
            "target_seconds": hours * 3600,
            "observed_seconds": 0,
            "rounds": 0,
            "interval_seconds": interval,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "restarts": 0,
            "coverage": [
                "three_parallel_temporal_tasks",
                "native_lab_publication",
                "image_and_video_decode_and_scoped_storage",
                "scoped_memory",
                "task_deduplication",
                "profile_reopen",
                "resource_measurements",
            ],
            "real_social_verified": False,
            "model_load_included": False,
        },
    )


async def run():
    FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (FOLDER / "runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        report = read()
        if report.get("state") not in ("starting", "running", "interrupted"):
            return
        PID.write_text(str(os.getpid()))
        PID.chmod(0o600)
        stopping = asyncio.Event()
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, stopping.set)
        report["restarts"] += 1
        report["state"] = "running"
        last = time.monotonic()
        next_round = last
        asset_ids = report.setdefault("asset_ids", {})
        try:
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    "INSERT INTO lead.tenants(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                    (report["tenant_id"], "Verification " + report["run_id"]),
                )
            with tenant_scope(report["tenant_id"], None), service_scope():
                for index in range(3):
                    if len(report["agent_ids"]) <= index:
                        aid = domain.create_agent(report["run_id"] + ":" + str(index))
                        report["agent_ids"].append(aid)
                        atomic_json(REPORT, report)
                    aid = report["agent_ids"][index]
                    domain.connect_lab(aid)
                    if aid not in asset_ids:
                        data = io.BytesIO()
                        Image.new("RGB", (64, 64), (30, 70, 90)).save(data, "PNG")
                        content = data.getvalue() if index != 2 else sample_video()
                        item = await assets.upload(
                            aid,
                            UploadFile(
                                file=io.BytesIO(content),
                                filename="soak.png" if index != 2 else "soak.mp4",
                            ),
                        )
                        asset_ids[aid] = {
                            "id": str(item["id"]),
                            "sha256": item["sha256"],
                        }
                        with tx(aid) as c:
                            domain.remember(c, aid, "Endurance " + aid)
                        atomic_json(REPORT, report)
                async with httpx.AsyncClient(
                    base_url="http://127.0.0.1:8089",
                    timeout=120,
                    headers={"Authorization": "Bearer " + internal_token()},
                    trust_env=False,
                ) as client:
                    while (
                        not stopping.is_set()
                        and report["observed_seconds"] < report["target_seconds"]
                    ):
                        now = time.monotonic()
                        round_observed = 0
                        if now >= next_round:
                            started = now
                            number = report["rounds"] + 1
                            tasks = []
                            for aid in report["agent_ids"]:
                                assets.resolve(
                                    aid, asset_ids[aid]["id"], asset_ids[aid]["sha256"]
                                )
                                with tx(aid) as c:
                                    memories = all_rows(
                                        c,
                                        "SELECT body FROM lead.memory_entries WHERE deleted_at IS NULL",
                                    )
                                    assert any(
                                        aid in m["body"].get("text", "")
                                        for m in memories
                                    )
                                    assert all(
                                        not any(
                                            other in str(m["body"])
                                            for other in report["agent_ids"]
                                            if other != aid
                                        )
                                        for m in memories
                                    )
                                key = "soak:" + report["run_id"] + ":" + str(number)
                                body = "Controlled endurance " + str(number) + " " + aid
                                first = domain.queue_lab_publish(aid, body, key)
                                assert (
                                    first["id"]
                                    == domain.queue_lab_publish(aid, body, key)["id"]
                                )
                                tasks.append((aid, str(first["id"]), key))
                            deadline = time.monotonic() + 180
                            rows = []
                            while time.monotonic() < deadline:
                                rows = []
                                for aid, task, _ in tasks:
                                    with tx(aid) as c:
                                        rows.append(
                                            one(
                                                c,
                                                "SELECT state,started_at,completed_at,created_at FROM lead.tasks WHERE id=%s",
                                                (task,),
                                            )
                                        )
                                if all(r["state"] == "succeeded" for r in rows):
                                    break
                                assert not any(
                                    r["state"] in ("failed", "waiting_user", "canceled")
                                    for r in rows
                                ), "task_did_not_complete"
                                await asyncio.sleep(0.5)
                            assert all(r["state"] == "succeeded" for r in rows), (
                                "task_deadline"
                            )
                            for aid, _, key in tasks:
                                with tx(aid) as c:
                                    assert (
                                        one(
                                            c,
                                            "SELECT count(*) AS n FROM lead.lab_posts WHERE operation_key=%s",
                                            (key,),
                                        )["n"]
                                        == 1
                                    )
                                    assert domain.metrics(c)["published"] == 0
                            if number % 12 == 0:
                                response = await client.post(
                                    "/agents/" + report["agent_ids"][0] + "/close"
                                )
                                response.raise_for_status()
                            duration = time.monotonic() - started
                            assert duration <= 180, "round_deadline"
                            round_observed = duration
                            if number > 1 and (number - 1) % 12 == 0:
                                report["profile_reopens"] = (
                                    report.get("profile_reopens", 0) + 1
                                )
                            report["rounds"] = number
                            report["last_round_seconds"] = round(duration, 3)
                            report["last_completion_seconds"] = [
                                round(
                                    (
                                        r["completed_at"] - r["created_at"]
                                    ).total_seconds(),
                                    3,
                                )
                                for r in rows
                            ]
                            report["last_overlap"] = max(
                                r["started_at"] for r in rows
                            ) < min(r["completed_at"] for r in rows)
                            measurement = await asyncio.to_thread(collect)
                            measurement.update(
                                round=number,
                                completion_seconds=report["last_completion_seconds"],
                                overlap=report["last_overlap"],
                            )
                            with (FOLDER / (report["run_id"] + ".jsonl")).open(
                                "a"
                            ) as stream:
                                os.chmod(stream.name, 0o600)
                                stream.write(
                                    json.dumps(measurement, default=str) + "\n"
                                )
                            next_round = time.monotonic() + report["interval_seconds"]
                        current = time.monotonic()
                        delta = current - last
                        if delta > round_observed + 20:
                            report["observation_gaps"] = (
                                report.get("observation_gaps", 0) + 1
                            )
                            delta = round_observed
                        report["observed_seconds"] += delta
                        last = current
                        report["updated_at"] = datetime.now(timezone.utc).isoformat()
                        atomic_json(REPORT, report)
                        try:
                            await asyncio.wait_for(
                                stopping.wait(),
                                timeout=min(10, max(0.1, next_round - current)),
                            )
                        except asyncio.TimeoutError:
                            pass
                    report["state"] = "stopped" if stopping.is_set() else "passed"
        except BaseException as error:
            report["state"] = "failed"
            report["error_type"] = type(error).__name__
        finally:
            with tenant_scope(report["tenant_id"], None), service_scope():
                for aid in report["agent_ids"]:
                    try:
                        domain.set_control(aid, "archive")
                        async with httpx.AsyncClient(
                            timeout=15, trust_env=False
                        ) as client:
                            await client.post(
                                "http://127.0.0.1:8089/agents/" + aid + "/close",
                                headers={"Authorization": "Bearer " + internal_token()},
                            )
                    except Exception:
                        pass
            with psycopg.connect(**database_kwargs(True)) as c:
                c.execute(
                    "UPDATE lead.tenants SET deleted_at=now() WHERE id=%s",
                    (report["tenant_id"],),
                )
            report["profiles_reclaimed"] = reclaim_profiles(report)
            atomic_json(REPORT, report)
            PID.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["start", "resume", "run", "status", "stop"])
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--interval", type=float, default=300)
    args = parser.parse_args()
    if args.hours <= 0 or args.interval < 1:
        raise SystemExit("positive duration and interval required")
    if args.action == "run":
        asyncio.run(run())
    elif args.action == "status":
        report = read()
        print(json.dumps({**report, "process_running": bool(running())}))
    elif args.action == "stop":
        if pid := running():
            os.kill(pid, signal.SIGTERM)
    else:
        if args.action == "start":
            initialize(args.hours, args.interval)
        report = read()
        if (
            report.get("state") in ("starting", "running", "interrupted")
            and not running()
        ):
            with (FOLDER / "runner.log").open("a") as output:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "run"],
                    cwd=ROOT,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        print(
            json.dumps({"state": report.get("state"), "run_id": report.get("run_id")})
        )


if __name__ == "__main__":
    os.umask(0o077)
    main()
