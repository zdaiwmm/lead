"""Durable work-hour gates, bounded parallelism and scoped failure backoff."""

import asyncio
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from temporalio import activity

from .admin_settings import effective
from .db import all_rows, emit, j, one, record, require_agent, tx


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
        if kind == "brand":
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
                from .admin_settings import effective
                # Serialize opt-out with experience insertion on the same
                # project row; a late extraction cannot restore sharing.
                one(c, "SELECT id FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (agent_id,))
                model_settings, _ = effective(c, "models", agent_id)
                if not model_settings.get("tools", {}).get("share_public_research", True):
                    c.execute("UPDATE lead.experiences SET public_projection=NULL,version=version+1 WHERE agent_id=%s AND public_projection IS NOT NULL", (agent_id,))
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
        require_agent(c, aid, True)
        policy, _ = effective(c, "runtime", aid)
        row = one(
            c,
            """INSERT INTO lead.admin_runtime_state(tenant_id,agent_id,category,failures,last_error,next_retry_at)
            VALUES(lead.ctx_tenant(),%s,%s,1,%s,now()+make_interval(secs=>%s))
            ON CONFLICT(tenant_id,agent_id,category) DO UPDATE SET failures=lead.admin_runtime_state.failures+1,
            last_error=excluded.last_error,next_retry_at=excluded.next_retry_at,updated_at=now() RETURNING failures""",
            (aid, category, error_code[:80], policy["retry_interval_seconds"]),
        )
        from .runtime_issues import report

        report(c, aid, "runtime:" + category, error_code,
               "本次对话未完成，可以在对话中重试或检查模型连接。" if category == "chat" else "这类工作暂时失败，正在等待重新检查。",
               retry_seconds=policy["retry_interval_seconds"], needs_user=category == "chat")
        return {"paused": False, "scope": category, "failures": row["failures"],
                "delay": policy["retry_interval_seconds"]}



def success(aid, category):
    from .runtime_issues import resolve
    with tx(aid) as c:
        resolve(c, aid, "runtime:" + category)
        c.execute(
            "UPDATE lead.admin_runtime_state SET failures=0,last_error=NULL,next_retry_at=NULL,updated_at=now() WHERE category=%s",
            (category,),
        )


def reset_failures(c, aid):
    c.execute("UPDATE lead.runtime_issues SET resolved_at=now(),retry_at=NULL WHERE agent_id=%s AND scope LIKE 'runtime:%%' AND resolved_at IS NULL", (aid,))
    c.execute(
        "UPDATE lead.admin_runtime_state SET failures=0,last_error=NULL,next_retry_at=NULL,updated_at=now() WHERE agent_id=%s",
        (aid,),
    )


def admission(aid, category, *, lane=None):
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
            (aid, category),
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
            "SELECT agent_id,category FROM lead.admin_runtime_leases WHERE released_at IS NULL AND expires_at>now()",
        )
        agents = {str(row["agent_id"]) for row in leases}
        occupied = {row["category"] for row in leases if str(row["agent_id"]) == aid}
        if ((lane or category) in occupied or
            (aid not in agents and len(agents) >= min(policy["max_concurrent_agents"], global_policy["max_concurrent_agents"]))):
            return {"allowed": False, "delay": 10, "reason": "concurrency_limit"}
        lease = one(
            c,
            "INSERT INTO lead.admin_runtime_leases(tenant_id,agent_id,category,expires_at) VALUES(lead.ctx_tenant(),%s,%s,now()+interval '4 minutes') RETURNING id",
            (aid, lane or category),
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
                    c, "SELECT desired_state,onboarding_state FROM lead.agents WHERE id=%s", (aid,)
                )
            if not agent or agent["desired_state"] == "archived":
                return (
                    {"state": "canceled"}
                    if category == "publish"
                    else {"delay": 86400, "archived": True, "idle": True}
                )
            lane = None
            channel_clock = category in ("channel_research","channel_delivery")
            if channel_clock:
                from .channel_delivery import current
                with tx(aid) as c:
                    clock = current(c, aid, args)
                    if not clock:
                        return {"stop":True}
                    lane = "channel:" + category + ":" + clock["input"]["platform"]
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
                    from .atomic_dispatch import channel_pilot
                    if task and (channel_pilot(aid) or agent["onboarding_state"].get("reddit_member_delivery") is True):
                        profile = one(c, """SELECT p.id FROM lead.tasks task JOIN lead.account_targets t ON t.id::text=task.input->>'target_id'
                            JOIN lead.social_accounts s ON s.id=t.account_id JOIN lead.browser_profiles p ON p.id=s.profile_id
                            WHERE task.id=%s AND (task.input ? 'channel_clock' OR task.input->>'member_contribution'='true')
                            AND p.slot<>'default'""", (args["task_id"],))
                        if profile:
                            lane = "channel:browser:" + str(profile["id"])
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
            gate = admission(aid, category, lane=lane) if lane else admission(aid, category)
            if not gate["allowed"]:
                deferred = {
                    "delay": gate["delay"],
                    "reason": gate["reason"],
                    **({"state": "deferred"} if category == "publish" else {}),
                }
                if channel_clock:
                    with tx(aid) as c:
                        if not current(c, aid, args):
                            return {"stop":True}
                        c.execute("UPDATE lead.tasks SET scheduled_at=now()+make_interval(secs=>%s),result=coalesce(result,'{}')||%s WHERE id=%s",
                                  (gate["delay"], j({"reason":gate["reason"]}),args["task_id"]))
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
