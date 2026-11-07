"""Passive, restart-safe operating acceptance. Never dispatches work or models."""

import fcntl
import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psutil

from .config import ROOT, STATE
from .db import all_rows, one, require_agent, tx, uid
from .scope import service_scope, tenant_scope

INTERVAL_SECONDS = 60
SOURCE_FILES = (
    "growth_experiments.py",
    "operating_kernel.py",
    "operating_plans.py",
    "activities.py",
    "community_schedule.py",
    "community_discovery.py",
    "community_runtime.py",
    "community_receipts.py",
    "telegram_group_board.py",
    "content_backlog.py",
    "model.py",
    "operating_admission.py",
    "adapters/web.py",
    "analytics.py",
    "promotion.py",
)


def utc():
    return datetime.now(timezone.utc)


def atomic(path, value):
    staged = path.with_suffix(".next")
    staged.write_text(json.dumps(value, ensure_ascii=False, default=str, indent=2))
    staged.chmod(0o600)
    staged.replace(path)


def service_states():
    names = ("api", "worker", "browser", "web", "maintenance")
    if Path("/run/systemd/system").is_dir() and not (STATE / "processes.json").exists():
        names += ("analytics",)
        units = ["lead@" + name + ".service" for name in names]
        result = subprocess.run(
            ["systemctl", "show", "--property=Id,MainPID,ActiveState", *units],
            capture_output=True, text=True, timeout=10, check=True,
        )
        states = {}
        for block in result.stdout.strip().split("\n\n"):
            row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
            states[row["Id"]] = {"pid": int(row.get("MainPID") or 0),
                                 "alive": row.get("ActiveState") == "active" and int(row.get("MainPID") or 0) > 0}
        return {name: states[unit] for name, unit in zip(names, units, strict=True)}
    path = STATE / "processes.json"
    processes = json.loads(path.read_text()) if path.exists() else {}
    return {name: {"pid": processes.get(name),
                   "alive": bool(processes.get(name) and psutil.pid_exists(processes[name]))}
            for name in names}


def snapshot(aid, since, now=None, until=None):
    started = time.monotonic()
    now = now or utc()
    until = until or now
    with tx(aid) as c:
        c.execute("SET TRANSACTION READ ONLY")
        a = require_agent(c, aid)
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        from .admin_settings import effective
        from .operating_admission import model_readiness
        from .operating_plans import acquisition

        models, version = effective(c, "models", aid)
        sample = {
            "at": now.isoformat(),
            "agent_id": aid,
            "agent": {
                "desired_state": a["desired_state"],
                "heartbeat_at": a["heartbeat_at"],
                "config_revision": a["config_revision"],
                "control_version": a["control_version"],
            },
            "kernel": {
                k: a["onboarding_state"].get("operating_kernel", {}).get(k)
                for k in (
                    "selected",
                    "last_result",
                    "next_at",
                    "weekly_plan_id",
                    "daily_plan_id",
                    "daily_plan_version",
                )
            },
            "models": {"version": version, "readiness": model_readiness(models)},
            "model_problem": {
                k: a["onboarding_state"].get("model_backoff", {}).get(k)
                for k in (
                    "kind",
                    "provider_error_code",
                    "status_code",
                    "requires_owner_action",
                    "retry_at",
                    "model_settings_version",
                    "model_reference",
                )
            },
            "usage": one(
                c,
                """SELECT count(*) AS settlements,coalesce(sum(units),0)::bigint AS tokens,
                coalesce(sum((details->>'input_tokens')::bigint),0)::bigint AS input_tokens,
                coalesce(sum((details->>'output_tokens')::bigint),0)::bigint AS output_tokens
                FROM lead.usage_events WHERE category='model_tokens' AND occurred_at>=%s AND occurred_at<=%s""",
                (since, until),
            ),
            "reservations": all_rows(
                c,
                "SELECT state,count(*) AS n,sum(units)::bigint AS units FROM lead.budget_reservations WHERE created_at>=%s AND created_at<=%s GROUP BY state",
                (since, until),
            ),
            "decisions": one(
                c,
                """SELECT count(*) AS total,
                count(*) FILTER(WHERE result->>'daily_plan_id' IS NOT NULL AND result->>'weekly_plan_id' IS NOT NULL) AS linked,
                count(*) FILTER(WHERE result->'selected'->>'kind' IN ('publish','content','operating.journey','operating.opportunities','operating.validate','operating.strategy')) AS business_selected
                FROM lead.tasks WHERE type='operating.decision' AND created_at>=%s AND created_at<=%s""",
                (since, until),
            ),
            "tasks": all_rows(
                c,
                """SELECT type,state,count(*) AS n,
                count(*) FILTER(WHERE input->'operating_plan'->>'day_id' IS NOT NULL) AS plan_bound,
                count(*) FILTER(WHERE created_at>=%s) AS created_in_window,
                count(*) FILTER(WHERE created_at>=%s AND input->'operating_plan'->>'day_id' IS NOT NULL) AS new_plan_bound,
                count(*) FILTER(WHERE started_at BETWEEN %s AND %s) AS executed_in_window,
                count(*) FILTER(WHERE started_at BETWEEN %s AND %s AND input->'operating_plan'->>'day_id' IS NOT NULL AND input->'operating_plan'->>'week_id' IS NOT NULL) AS executed_plan_bound
                FROM lead.tasks WHERE type<>'operating.decision' AND (created_at BETWEEN %s AND %s OR started_at BETWEEN %s AND %s OR completed_at BETWEEN %s AND %s) GROUP BY type,state ORDER BY type,state""",
                (since, since, since, until, since, until, since, until, since, until, since, until),
            ),
            "publications": all_rows(
                c,
                """SELECT a.id,a.state,a.external_id,a.external_url,s.platform,t.id AS task_id,
                coalesce((t.input->>'bootstrap_verification')::boolean,false) AS verification,
                t.input->>'title' AS title,t.input->'operating_plan' AS operating_plan,t.result->'delivery' AS delivery,t.completed_at
                FROM lead.actions a JOIN lead.tasks t ON t.id=a.task_id
                JOIN lead.account_targets target ON target.id=a.target_id JOIN lead.social_accounts s ON s.id=target.account_id
                WHERE s.platform<>'lab' AND (a.created_at BETWEEN %s AND %s OR t.completed_at BETWEEN %s AND %s OR nullif(t.result->'delivery'->>'public_at','')::timestamptz BETWEEN %s AND %s) ORDER BY a.created_at""",
                (since, until, since, until, since, until),
            ),
            "plans": all_rows(
                c,
                "SELECT id,body->>'horizon' horizon,body->>'period' period,body->>'version' version FROM lead.memory_entries WHERE body->>'type'='operating_plan' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 6",
            ),
            "measurement": acquisition(c, a, business, now) if business else None,
        }
        from .operating_kernel import (
            blocked,
            eligible_publish_targets,
            publication_lane_key,
        )

        lanes = a["onboarding_state"].get("operating_kernel", {}).get("lanes", {})
        sample["overdue_publish_targets"] = [
            str(target["target_id"]) for target in eligible_publish_targets(c, blocked(c))
            if (due := lanes.get(publication_lane_key([target]), {}).get("next_at"))
            and datetime.fromisoformat(due) < now - timedelta(minutes=10)
        ]
        sample["community"] = one(c, """
            SELECT count(*) FILTER (WHERE result->>'join_state'='confirmed') AS confirmed_joins,
                   count(*) FILTER (WHERE result->>'join_state'='pending_approval') AS pending_joins,
                   count(*) FILTER (WHERE result->>'reply_state'='confirmed') AS confirmed_replies,
                   count(*) FILTER (WHERE result->>'reply_state'='confirmed' AND input->'reply'->>'promote'='true') AS confirmed_promotions
            FROM lead.tasks WHERE agent_id=%s AND tenant_id=%s AND type='community.engage'
            AND started_at BETWEEN %s AND %s""", (aid, a["tenant_id"], since, until))
        from .growth_experiments import enabled, report
        if business and enabled(a):
            sample["growth_trials"]=report(c,a,business,now)
    sample["services"] = service_states()
    sample["source_hashes"] = {
        name: hashlib.sha256(
            (ROOT / "services/api/lead_app" / name).read_bytes()
        ).hexdigest()
        for name in SOURCE_FILES
    }
    from .telegram_group_board import execution
    with tx(aid) as c:
        c.execute("SET TRANSACTION READ ONLY")
        sample["community_execution"] = execution(c, aid, since, until)
    sample["host"] = {"cpu_times":psutil.cpu_times()._asdict(),
                      "memory_percent":psutil.virtual_memory().percent,
                      "load_average":list(os.getloadavg()),
                      "snapshot_seconds":round(time.monotonic()-started, 3)}
    return sample


def create(aid, hours=24, *, cohort="baseline"):
    if hours != 24:
        raise ValueError("正式验收窗口固定为连续 24 小时")
    with tx(aid) as c:
        a = require_agent(c, aid)
        if a["desired_state"] != "running":
            raise ValueError("请先恢复 Agent 自主运营")
        tenant = str(a["tenant_id"])
    root = STATE / "acceptance"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Repeated requests resume the same unfinished window, never reset evidence.
    for path in root.glob("*/manifest.json"):
        old = json.loads(path.read_text())
        if old["agent_id"] == aid and old["state"] == "active" and old.get("cohort","baseline")==cohort:
            return old
    now = utc()
    run = {
        "id": uid(),
        "cohort":cohort,
        "agent_id": aid,
        "tenant_id": tenant,
        "state": "active",
        "starts_at": now.isoformat(),
        "ends_at": (now + timedelta(hours=hours)).isoformat(),
        "interval_seconds": INTERVAL_SECONDS,
        "sample_count": 0,
        "max_gap_seconds": 0,
        "continuity_issues": [],
        "recorder_errors": [],
    }
    folder = root / run["id"]
    folder.mkdir(mode=0o700)
    atomic(folder / "manifest.json", run)
    sample_run(folder)
    return json.loads((folder / "manifest.json").read_text())


def summarize(run, sample):
    confirmed = [
        p
        for p in sample["publications"]
        if p["state"] == "confirmed" and p["external_id"]
    ]
    from .publication_delivery import is_public

    formal = [
        p
        for p in confirmed
        if not p["verification"]
        and is_public(p.get("platform"), p.get("delivery"))
        and (
            not (p.get("delivery") or {}).get("public_at")
            or datetime.fromisoformat(p["delivery"]["public_at"])
            <= datetime.fromisoformat(run["ends_at"])
        )
    ]
    uncertainty = sum(
        r["n"] for r in sample["reservations"] if r["state"] in ("reserved", "unknown")
    )
    decisions = sample["decisions"]
    business_tasks = [
        t
        for t in sample["tasks"]
        if t["type"]
        in {
            "content.prepare",
            "operating.journey",
            "operating.opportunities",
            "operating.validate",
            "operating.strategy",
        }
        or t["type"].endswith(".publish")
    ]
    checks = {
        "continuous_24h_sample_coverage": not run["continuity_issues"]
        and not run["recorder_errors"]
        and run["sample_count"] >= 2
        and datetime.fromisoformat(sample["at"])
        >= datetime.fromisoformat(run["ends_at"]),
        "all_decisions_linked_to_plans": decisions["total"] > 0
        and decisions["linked"] == decisions["total"],
        "business_execution_observed": decisions["business_selected"] > 0,
        "executed_business_tasks_bound_to_plans": sum(
            t["executed_in_window"] for t in business_tasks
        )
        > 0
        and all(t["executed_in_window"] == t["executed_plan_bound"] for t in business_tasks),
        "formal_publication_confirmed": bool(formal),
        "usage_reconciled": uncertainty == 0,
        "traffic_measurement_covered": bool(
            (sample.get("measurement") or {}).get("clicks", {}).get("coverage_verified")
        ),
    }
    return {
        "run_id": run["id"],
        "starts_at": run["starts_at"],
        "ends_at": run["ends_at"],
        "sample_count": run["sample_count"],
        "max_gap_seconds": run["max_gap_seconds"],
        "finalization_delay_seconds": run.get("finalization_delay_seconds", 0),
        "checks": checks,
        "verdict": "passed" if all(checks.values()) else "needs_review",
        "usage": sample["usage"],
        "unsettled_reservations": uncertainty,
        "formal_publications": formal,
        "verification_count": sum(p["verification"] for p in confirmed),
        "submitted_formal_publications": [
            p for p in confirmed if not p["verification"]
        ],
        "decisions": decisions,
        "tasks": sample["tasks"],
        "measurement": sample["measurement"],
        "community_execution": sample.get("community_execution"),
        "continuity_issues": run["continuity_issues"],
        "recorder_errors": run["recorder_errors"],
        "limits": "1 分钟采样不等于逐秒可用性证明；无数据覆盖时不推断引流、转化或效率。验证发布不计业务推广成果。",
    }


def sample_run(folder, now=None):
    now = now or utc()
    with (folder / ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        path = folder / "manifest.json"
        run = json.loads(path.read_text())
        if run["state"] != "active":
            return
        previous = datetime.fromisoformat(run.get("last_sample_at") or run["starts_at"])
        if (
            run["sample_count"]
            and (now - previous).total_seconds() < INTERVAL_SECONDS
            and now < datetime.fromisoformat(run["ends_at"])
        ):
            return
        try:
            with tenant_scope(run["tenant_id"], None), service_scope():
                sample = snapshot(
                    run["agent_id"],
                    run["starts_at"],
                    now,
                    min(now, datetime.fromisoformat(run["ends_at"])),
                )
            coverage_at = min(now, datetime.fromisoformat(run["ends_at"]))
            gap = (coverage_at - previous).total_seconds()
            run["max_gap_seconds"] = max(run["max_gap_seconds"], round(gap, 2))
            issues = []
            if gap > INTERVAL_SECONDS * 3:
                issues.append("sampling_gap")
            if sample["agent"]["desired_state"] != "running":
                issues.append("agent_not_running")
            if not all(s["alive"] for s in sample["services"].values()):
                issues.append("service_unavailable")
            if sample.get("overdue_publish_targets"):
                issues.append("publication_check_overdue")
            if issues:
                run["continuity_issues"].append(
                    {"at": now.isoformat(), "issues": issues}
                )
            with (folder / "samples.jsonl").open("a") as stream:
                stream.write(json.dumps(sample, ensure_ascii=False, default=str) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            atomic(folder / "latest.json", sample)
            run.update(
                sample_count=run["sample_count"] + 1, last_sample_at=now.isoformat()
            )
            if now >= datetime.fromisoformat(run["ends_at"]):
                run["finalization_delay_seconds"] = max(
                    0,
                    round(
                        (now - datetime.fromisoformat(run["ends_at"])).total_seconds(),
                        2,
                    ),
                )
                run["state"] = "finished"
                result = summarize(run, sample)
                atomic(folder / "result.json", result)
                (folder / "result.md").write_text(
                    f"# Agent 24 小时实跑验收\n\n窗口：{run['starts_at']} 至 {run['ends_at']}\n\n判定：{result['verdict']}\n\n模型结算：{sample['usage']['tokens']} token；正式发布：{len(result['formal_publications'])}；验证发布：{result['verification_count']}。\n\n计划关联：{sample['decisions']['linked']}/{sample['decisions']['total']} 次决定。\n\n{result['limits']}\n\n完整证据见同目录 result.json 与 samples.jsonl。\n"
                )
        except Exception as error:
            run["recorder_errors"].append(
                {"at": now.isoformat(), "error_type": type(error).__name__}
            )
        atomic(path, run)


def sample_active():
    for path in (STATE / "acceptance").glob("*/manifest.json"):
        sample_run(path.parent)


def status(run_id):
    folder = STATE / "acceptance" / str(__import__("uuid").UUID(run_id))
    run = json.loads((folder / "manifest.json").read_text())
    sample = (
        json.loads((folder / "latest.json").read_text())
        if (folder / "latest.json").exists()
        else None
    )
    return {
        "run": run,
        "latest": sample,
        "result": json.loads((folder / "result.json").read_text())
        if (folder / "result.json").exists()
        else None,
    }
