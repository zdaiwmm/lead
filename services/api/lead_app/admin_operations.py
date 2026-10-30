"""Durable work-hour gates, bounded parallelism and automatic failure stops."""

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from temporalio import activity

from .admin_settings import effective
from .db import all_rows, audit, emit, one, record, require_agent, tx


def work_window(hours, now=None):
    """Return the next allowed instant, including overnight and DST windows."""
    now = now or datetime.now(timezone.utc)
    if not hours["enabled"]:
        return {"allowed": True, "delay": 0, "next_at": now.isoformat()}
    local = now.astimezone(ZoneInfo(hours["timezone"]))
    start_hour, start_minute = map(int, hours["start"].split(":"))
    end_hour, end_minute = map(int, hours["end"].split(":"))
    upcoming = []
    for offset in range(-1, 9):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in hours["weekdays"]:
            continue
        start = datetime(
            day.year, day.month, day.day, start_hour, start_minute, tzinfo=local.tzinfo
        )
        end_day = day + timedelta(days=hours["end"] <= hours["start"])
        end = datetime(
            end_day.year,
            end_day.month,
            end_day.day,
            end_hour,
            end_minute,
            tzinfo=local.tzinfo,
        )
        if start <= local < end:
            return {"allowed": True, "delay": 0, "next_at": now.isoformat()}
        if start > local:
            upcoming.append(start.astimezone(timezone.utc))
    due = min(upcoming)
    return {
        "allowed": False,
        "delay": max(1, int((due - now).total_seconds()) + 1),
        "next_at": due.isoformat(),
    }


def configuration_published(c, kind, aid):
    """Brand changes fence stale content using the existing Agent revision guard."""
    from .domain import snapshot_preferences

    previous = one(c, "SELECT current_setting('app.agent_id',true) AS aid")["aid"] or ""
    agents = all_rows(
        c,
        "SELECT id FROM lead.agents WHERE desired_state<>'archived' AND (%s::uuid IS NULL OR id=%s) ORDER BY id",
        (aid, aid),
    )
    for agent in agents:
        agent_id = str(agent["id"])
        override = one(
            c,
            "SELECT body FROM lead.admin_configs WHERE config_key=%s",
            (f"{kind}/{agent_id}",),
        )
        if not aid and override and override["body"] is not None:
            continue
        if kind in {"brand", "attribution"}:
            c.execute("SELECT set_config('app.agent_id',%s,true)", (agent_id,))
            snapshot_preferences(c, agent_id)
            record(
                c,
                agent_id,
                "品牌与知识资料已更新" if kind == "brand" else "数据与归因规则已更新",
                "后续工作使用新资料，旧方向的排队内容已撤回。",
            )
            c.execute("SELECT set_config('app.agent_id',%s,true)", (previous,))
        else:
            emit(c, agent_id, "supervisor.wake")
            if kind == "models":
                for job in all_rows(
                    c,
                    """UPDATE lead.chat_jobs SET status='pending',error_code=NULL
                  WHERE agent_id=%s AND status IN ('blocked','failed') AND
                  (error_code IN ('MODEL_NOT_FOUND','MODEL_NOT_CONFIGURED') OR error_context->>'error_type'='ModelNotFoundError')
                  RETURNING id,message_id""",
                    (agent_id,),
                ):
                    c.execute(
                        "UPDATE lead.conversation_messages SET status='pending' WHERE id=%s",
                        (job["message_id"],),
                    )
                    emit(c, agent_id, "chat.enqueue", {"job_id": str(job["id"])})


def failure(aid, category, error_code):
    """Count real failures, preserve uncertain actions and fence future writes."""
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        policy, _ = effective(c, "runtime", aid)
        row = one(
            c,
            """INSERT INTO lead.admin_runtime_state(tenant_id,agent_id,category,failures,last_error,next_retry_at)
            VALUES(lead.ctx_tenant(),%s,%s,1,%s,now()+make_interval(secs=>%s))
            ON CONFLICT(tenant_id,agent_id,category) DO UPDATE SET failures=lead.admin_runtime_state.failures+1,
            last_error=excluded.last_error,next_retry_at=excluded.next_retry_at,updated_at=now() RETURNING failures""",
            (aid, category, error_code[:80], policy["retry_interval_seconds"]),
        )
        stopped = row["failures"] >= policy["pause_after_failures"]
        if stopped and agent["desired_state"] == "running":
            c.execute(
                "UPDATE lead.agents SET desired_state='paused',health_state='needs_attention',control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
            record(
                c,
                aid,
                "连续失败，已自动暂停运营",
                "请查看异常记录，处理后可在工作台恢复。",
                "warning",
            )
            audit(
                c,
                aid,
                "runtime.automatic_pause",
                {
                    "category": category,
                    "error_code": error_code[:80],
                    "failures": row["failures"],
                },
            )
        return {
            "paused": stopped,
            "failures": row["failures"],
            "delay": policy["retry_interval_seconds"],
        }


def success(aid, category):
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.admin_runtime_state SET failures=0,last_error=NULL,next_retry_at=NULL,updated_at=now() WHERE category=%s",
            (category,),
        )


def reset_failures(c, aid):
    c.execute(
        "UPDATE lead.admin_runtime_state SET failures=0,last_error=NULL,next_retry_at=NULL,updated_at=now() WHERE agent_id=%s",
        (aid,),
    )


def admission(aid, category):
    # A tenant transaction is needed to count all live leases in that workspace.
    with tx() as c:
        agent = require_agent(c, aid)
        policy, version = effective(c, "runtime", aid)
        global_policy, _ = effective(c, "runtime")
        if agent["desired_state"] != "running":
            return {"allowed": False, "delay": 300, "reason": "agent_paused"}
        timing = work_window(policy["work_hours"])
        if not timing["allowed"]:
            return {**timing, "reason": "work_hours"}
        backoff = one(
            c,
            "SELECT max(next_retry_at) AS due FROM lead.admin_runtime_state WHERE agent_id=%s AND category=%s",
            (aid, "planning" if category == "autonomous" else category),
        )
        if backoff["due"] and backoff["due"] > datetime.now(timezone.utc):
            return {
                "allowed": False,
                "delay": max(
                    1,
                    int((backoff["due"] - datetime.now(timezone.utc)).total_seconds())
                    + 1,
                ),
                "reason": "failure_backoff",
            }
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(lead.ctx_tenant()::text,91))"
        )
        leases = all_rows(
            c,
            "SELECT agent_id FROM lead.admin_runtime_leases WHERE released_at IS NULL AND expires_at>now()",
        )
        if any(str(row["agent_id"]) == aid for row in leases) or len(leases) >= min(
            policy["max_concurrent_agents"], global_policy["max_concurrent_agents"]
        ):
            return {"allowed": False, "delay": 10, "reason": "concurrency_limit"}
        lease = one(
            c,
            "INSERT INTO lead.admin_runtime_leases(tenant_id,agent_id,category,expires_at) VALUES(lead.ctx_tenant(),%s,%s,now()+interval '4 minutes') RETURNING id",
            (aid, category),
        )
        return {
            "allowed": True,
            "lease_id": str(lease["id"]),
            "policy_version": version,
        }


def managed_execution(category):
    def decorate(fn):
        @wraps(fn)
        async def run(args):
            aid = args["agent_id"]
            with tx(aid) as c:
                agent = one(
                    c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,)
                )
            if not agent or agent["desired_state"] == "archived":
                return (
                    {"state": "canceled"}
                    if category == "publish"
                    else {"delay": 86400, "archived": True, "idle": True}
                )
            if category == "publish":
                with tx(aid) as c:
                    task = one(
                        c, "SELECT type FROM lead.tasks WHERE id=%s", (args["task_id"],)
                    )
                    action = one(
                        c,
                        "SELECT state FROM lead.actions WHERE task_id=%s",
                        (args["task_id"],),
                    )
                if task and task["type"] == "lab.publish":
                    return await fn(args)
                if action and action["state"] in {
                    "dispatching",
                    "unknown",
                    "confirmed",
                }:
                    return await fn(
                        args
                    )  # Reconciliation is allowed outside work hours.
                if agent["desired_state"] == "paused":
                    return await fn(args)
            gate = admission(aid, category)
            if not gate["allowed"]:
                deferred = {
                    "delay": gate["delay"],
                    "reason": gate["reason"],
                    **({"state": "deferred"} if category == "publish" else {}),
                }
                if category == "autonomous":
                    from .community_schedule import completed

                    return completed(aid, deferred, respect_delay=True)
                return deferred

            async def renew_lease():
                while True:
                    await asyncio.sleep(30)

                    def renew():
                        with tx(aid) as c:
                            c.execute(
                                "UPDATE lead.admin_runtime_leases SET expires_at=now()+interval '4 minutes' WHERE id=%s AND released_at IS NULL",
                                (gate["lease_id"],),
                            )

                    await asyncio.to_thread(renew)

            renewal = asyncio.create_task(renew_lease())
            try:
                result = await fn(args)
                if category == "publish" and result.get("state") in {
                    "succeeded",
                    "confirmed",
                }:
                    success(aid, "publish")
                return result
            finally:
                renewal.cancel()
                await asyncio.gather(renewal, return_exceptions=True)
                with tx(aid) as c:
                    c.execute(
                        "UPDATE lead.admin_runtime_leases SET released_at=now() WHERE id=%s",
                        (gate["lease_id"],),
                    )

        return run

    return decorate


@activity.defn
async def execution_policy(args: dict):
    from .identity_store import agent_tenant
    from .scope import tenant_scope

    aid = args["agent_id"]
    with tenant_scope(agent_tenant(aid), None), tx(aid) as c:
        policy, version = effective(c, "runtime", aid)
        return {"version": version, **policy}
