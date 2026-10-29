"""Operator monitor and safe-point backups, independent from Agent workflows."""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .backup_crypto import key_init, load_key
from .config import ROOT, STATE, database_kwargs, env
from .ops import atomic_json, collect, evaluate, persist_alerts


class BackupDeferred(Exception):
    pass


class Maintenance:
    def __init__(self):
        self.stop = asyncio.Event()
        self.job_state = {}
        self.children = set()
        self.folder = STATE / "backups/managed"
        self.keyfile = Path(
            env().get("LEAD_BACKUP_KEY_FILE") or STATE / "keys/backup.key"
        )

    def ensure_key(self):
        if env().get(
            "LEAD_DEPLOYMENT_MODE"
        ) == "production" and self.keyfile.resolve().is_relative_to(ROOT):
            raise ValueError("生产备份密钥必须托管在代码和备份目录之外")
        if not self.keyfile.exists():
            if env().get("LEAD_DEPLOYMENT_MODE") == "production":
                raise ValueError("生产备份需要独立托管的密钥文件")
            try:
                key_init(self.keyfile)
            except FileExistsError:
                pass  # Another scheduled backup initialized the same private key.
        load_key(self.keyfile)

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def backup(self, kind, aid=None):
        command = [
            sys.executable,
            str(ROOT / "scripts/backup.py"),
            kind,
            "--key-file",
            str(self.keyfile),
            "--folder",
            str(self.folder),
            "--managed",
        ]
        if aid:
            command += ["--agent", str(aid)]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=ROOT,
        )
        self.children.add(process)
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=600)
            result = json.loads(output[-16384:] or b"{}")
            if process.returncode:
                if result.get("error_code") == "backup_busy":
                    raise BackupDeferred
                raise RuntimeError(result.get("error", "backup_failed"))
            return result
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            self.children.discard(process)

    def due(self, kind):
        with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
            if kind == "database":
                latest = c.execute(
                    "SELECT extract(epoch FROM now()-max(started_at)) AS age FROM lead.backup_records WHERE kind='database' AND state='verified' AND manifest->>'managed'='true'"
                ).fetchone()["age"]
                return latest is None or float(latest) >= 600
            return [
                str(r["id"])
                for r in c.execute(
                    "SELECT a.id FROM lead.agents a JOIN lead.tenants t ON t.id=a.tenant_id WHERE a.desired_state<>'archived' AND t.deleted_at IS NULL AND NOT EXISTS(SELECT 1 FROM lead.backup_records b WHERE b.agent_id=a.id AND b.kind='profile' AND b.state='verified' AND b.started_at>now()-interval '24 hours') ORDER BY a.created_at"
                ).fetchall()
                if (STATE / "profiles" / str(r["id"]) / "identity.json").is_file()
            ]

    async def database_loop(self):
        while not self.stop.is_set():
            try:
                self.ensure_key()
                if await asyncio.to_thread(self.due, "database"):
                    self.job_state["database"] = "running"
                    await self.backup("database")
                self.job_state["database"] = "ready"
            except BackupDeferred:
                self.job_state["database"] = "waiting"
            except Exception as error:
                self.job_state["database"] = "failed:" + type(error).__name__
            await self.pause(30)

    async def profile_loop(self):
        while not self.stop.is_set():
            try:
                self.ensure_key()
                for aid in await asyncio.to_thread(self.due, "profile"):
                    if self.stop.is_set():
                        break
                    # Temporary busy/safe-point failures are retried; no user
                    # login or daily approval is requested by this scheduler.
                    try:
                        await self.backup("profile", aid)
                    except Exception:
                        continue
            except Exception as error:
                self.job_state["profile"] = "failed:" + type(error).__name__
            await self.pause(60)

    async def monitor_loop(self):
        while not self.stop.is_set():
            try:
                sample = await asyncio.to_thread(collect)
                alerts = evaluate(sample)
                if self.job_state.get("database", "").startswith("failed:"):
                    alerts["backup.failed"] = {
                        "code": "backup.failed",
                        "severity": "critical",
                    }
                if env().get("LEAD_DEPLOYMENT_MODE") == "production" and not sample.get(
                    "backup", {}
                ).get("offsite_verified"):
                    alerts["backup.offsite"] = {
                        "code": "backup.offsite",
                        "severity": "critical",
                    }
                sample["alerts"] = list(alerts.values())
                sample["backup_jobs"] = dict(self.job_state)
                atomic_json(STATE / "ops-status.json", sample)
                await asyncio.to_thread(persist_alerts, alerts)
                (STATE / "maintenance-heartbeat").write_text(str(time.time()))
            except Exception:
                pass  # The next sample retries; last snapshot becomes visibly stale.
            await self.pause(10)

    async def run(self):
        loop = asyncio.get_running_loop()
        for name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(name, self.stop.set)
        jobs = [asyncio.create_task(self.monitor_loop())]
        if env().get("LEAD_BACKUP_AUTO", "1") == "1":
            jobs.extend(
                (
                    asyncio.create_task(self.database_loop()),
                    asyncio.create_task(self.profile_loop()),
                )
            )
        await self.stop.wait()
        for child in list(self.children):
            if child.returncode is None:
                child.terminate()
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)


if __name__ == "__main__":
    os.umask(0o077)
    asyncio.run(Maintenance().run())
