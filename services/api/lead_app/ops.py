"""Private operator health, measurements and deduplicated alerts."""

import hashlib
import hmac
import json
import os
import shutil
import time
from datetime import datetime, timezone

import httpx
import psutil
import psycopg
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from psycopg.rows import dict_row

from .config import CONFIG, ROOT, STATE, database_kwargs
from .security import internal_token
from .telemetry import HTTP

router = APIRouter()


def credential():
    return hmac.new(
        CONFIG["session_secret"].encode(), b"lead-operator-monitor-v1", hashlib.sha256
    ).hexdigest()


def check(request):
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, "Bearer " + credential()):
        raise HTTPException(403)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".next")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def process_usage():
    result = {}
    path = STATE / "processes.json"
    if not path.exists():
        return result
    for name, pid in json.loads(path.read_text()).items():
        try:
            parent = psutil.Process(int(pid))
            if not any(str(ROOT) in arg for arg in parent.cmdline()):
                continue
            processes = [parent] + parent.children(recursive=True)
            rss = 0
            cpu = 0.0
            live = 0
            for process in processes:
                try:
                    rss += process.memory_info().rss
                    timing = process.cpu_times()
                    cpu += timing.user + timing.system
                    live += 1
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            result[name] = {
                "rss_bytes": rss,
                "cpu_seconds": round(cpu, 3),
                "process_count": live,
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            result[name] = {"unavailable": True}
    return result


def collect():
    now = datetime.now(timezone.utc)
    result = {
        "sampled_at": now.isoformat(),
        "disk_free_bytes": shutil.disk_usage(STATE).free,
        "processes": process_usage(),
        "services": {},
        "queues": {},
        "backup": {},
    }
    worker = STATE / "worker-heartbeat"
    result["services"]["worker"] = (
        worker.exists() and time.time() - worker.stat().st_mtime < 20
    )
    try:
        response = httpx.get(
            "http://127.0.0.1:8089/health",
            headers={"Authorization": "Bearer " + internal_token()},
            timeout=3,
            trust_env=False,
        )
        response.raise_for_status()
        browser = response.json()
        result["services"]["browser"] = bool(browser.get("ok"))
        result["active_profiles"] = browser.get("active_profiles", 0)
        result["waiting_profiles"] = browser.get("waiting", 0)
    except Exception:
        result["services"]["browser"] = False
    try:
        with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
            result["services"]["database"] = bool(c.execute("SELECT 1").fetchone())
            rows = c.execute(
                "SELECT t.state,count(*) AS n,coalesce(max(extract(epoch FROM now()-t.created_at)),0) AS oldest FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id JOIN lead.tenants w ON w.id=a.tenant_id WHERE a.desired_state<>'archived' AND w.deleted_at IS NULL AND t.state IN ('queued','running','reconciling') GROUP BY t.state"
            ).fetchall()
            result["queues"] = {
                r["state"]: {"count": r["n"], "oldest_seconds": float(r["oldest"])}
                for r in rows
            }
            result["unknown_actions"] = c.execute(
                "SELECT count(*) AS n FROM lead.actions x JOIN lead.agents a ON a.id=x.agent_id JOIN lead.tenants t ON t.id=a.tenant_id WHERE x.state IN ('dispatching','unknown') AND a.desired_state<>'archived' AND t.deleted_at IS NULL"
            ).fetchone()["n"]
            result["needs_auth"] = c.execute(
                "SELECT count(*) AS n FROM lead.social_accounts s JOIN lead.agents a ON a.id=s.agent_id JOIN lead.tenants t ON t.id=a.tenant_id WHERE s.state='needs_auth' AND a.desired_state<>'archived' AND t.deleted_at IS NULL"
            ).fetchone()["n"]
            latest = c.execute(
                "SELECT id,started_at,completed_at,object_ref,size_bytes,manifest FROM lead.backup_records WHERE kind='database' AND state='verified' ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            if latest:
                result["backup"] = {
                    "id": str(latest["id"]),
                    "age_seconds": max(0, (now - latest["started_at"]).total_seconds()),
                    "exists": __import__("pathlib")
                    .Path(latest["object_ref"])
                    .is_file(),
                    "size_bytes": latest["size_bytes"],
                    "offsite_verified": c.execute(
                        "SELECT lead.offsite_backup_ready()"
                    ).fetchone()["offsite_backup_ready"],
                }
            else:
                result["backup"] = {
                    "age_seconds": None,
                    "exists": False,
                    "offsite_verified": False,
                }
    except Exception:
        result["services"]["database"] = False
    return result


def evaluate(snapshot):
    alerts = {}
    for name, healthy in snapshot["services"].items():
        if not healthy:
            alerts[name + ".unavailable"] = {
                "code": name + ".unavailable",
                "severity": "critical",
            }
    if snapshot["disk_free_bytes"] < 1024**3:
        alerts["disk.low"] = {
            "code": "disk.low",
            "severity": "critical"
            if snapshot["disk_free_bytes"] < 512 * 1024**2
            else "warning",
        }
    for state, value in snapshot.get("queues", {}).items():
        if value["oldest_seconds"] > (300 if state == "queued" else 900):
            alerts["queue." + state] = {"code": "queue." + state, "severity": "warning"}
    if snapshot.get("unknown_actions"):
        alerts["publish.unknown"] = {"code": "publish.unknown", "severity": "warning"}
    if snapshot.get("needs_auth"):
        alerts["account.needs_auth"] = {
            "code": "account.needs_auth",
            "severity": "warning",
        }
    backup = snapshot.get("backup", {})
    if (
        not backup.get("exists")
        or backup.get("age_seconds") is None
        or backup["age_seconds"] > 900
    ):
        alerts["backup.overdue"] = {"code": "backup.overdue", "severity": "critical"}
    return alerts


def persist_alerts(alerts, kwargs=None):
    with psycopg.connect(**(kwargs or database_kwargs(True))) as c:
        for key, value in alerts.items():
            c.execute(
                "INSERT INTO lead.ops_alerts(event_key,code,severity,state,first_seen_at,last_seen_at) VALUES(%s,%s,%s,'active',now(),now()) ON CONFLICT(event_key) DO UPDATE SET state='active',severity=excluded.severity,first_seen_at=CASE WHEN lead.ops_alerts.state='resolved' THEN now() ELSE lead.ops_alerts.first_seen_at END,last_seen_at=now(),resolved_at=NULL,occurrences=lead.ops_alerts.occurrences+1",
                (key, value["code"], value["severity"]),
            )
        c.execute(
            "UPDATE lead.ops_alerts SET state='resolved',resolved_at=now() WHERE state='active' AND NOT(event_key=ANY(%s::text[]))",
            (list(alerts),),
        )


@router.get("/internal/status")
def status(request: Request):
    check(request)
    path = STATE / "ops-status.json"
    if not path.exists():
        raise HTTPException(503, "运行监控尚未启动")
    result = json.loads(path.read_text())
    result["fresh"] = time.time() - path.stat().st_mtime < 30
    return result


@router.get("/internal/metrics", response_class=PlainTextResponse)
def metrics(request: Request):
    check(request)
    output = HTTP.render()
    path = STATE / "ops-status.json"
    if path.exists():
        values = json.loads(path.read_text())
        output += (
            f"lead_ops_snapshot_fresh {int(time.time() - path.stat().st_mtime < 30)}\n"
        )
        output += f"lead_disk_free_bytes {values['disk_free_bytes']}\n"
        for name, healthy in values["services"].items():
            output += f'lead_service_up{{service="{name}"}} {int(healthy)}\n'
        for name, usage in values.get("processes", {}).items():
            for field in ("rss_bytes", "cpu_seconds", "process_count"):
                if field in usage:
                    output += (
                        f'lead_process_{field}{{service="{name}"}} {usage[field]}\n'
                    )
        for state, value in values.get("queues", {}).items():
            output += f'lead_queue_depth{{state="{state}"}} {value["count"]}\nlead_queue_oldest_seconds{{state="{state}"}} {value["oldest_seconds"]}\n'
        output += f"lead_publish_unknown {values.get('unknown_actions', 0)}\n"
        age = values.get("backup", {}).get("age_seconds")
        if age is not None:
            output += f"lead_database_backup_age_seconds {age}\n"
    return PlainTextResponse(output, media_type="text/plain; version=0.0.4")
