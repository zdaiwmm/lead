"""Durable human-verification handoff; never solve or discard a challenge."""

from datetime import datetime, timezone

from .db import (
    all_rows,
    append_message,
    audit,
    emit,
    j,
    one,
    record,
    require_agent,
    tx,
    uid,
)

KEY = "browser_challenges"


def all_pending(c):
    row = one(
        c,
        "SELECT onboarding_state->'browser_challenges' AS challenges FROM lead.agents",
    )
    return [
        v
        for v in ((row or {}).get("challenges") or {}).values()
        if v.get("state") == "waiting_user"
    ]


def pending(c, profile_id=None, *, account_id=None):
    return next(
        (
            v
            for v in all_pending(c)
            if (profile_id is None or v["profile_id"] == str(profile_id))
            and (account_id is None or v["account_id"] == str(account_id))
        ),
        None,
    )


def for_task(c, task):
    row = one(
        c,
        """SELECT s.profile_id FROM lead.social_accounts s
        JOIN lead.account_targets t ON t.account_id=s.id
        WHERE t.id=(SELECT target_id FROM lead.actions WHERE task_id=%s)
           OR t.id::text=%s LIMIT 1""",
        (task["id"], task["input"].get("target_id")),
    )
    if not row:
        row = one(
            c, "SELECT id AS profile_id FROM lead.browser_profiles WHERE slot='default'"
        )
    return pending(c, row["profile_id"]) if row else None


def save(c, aid, value):
    c.execute(
        """UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,
        '{browser_challenges}', coalesce(onboarding_state->'browser_challenges','{}') || %s),
        version=version+1 WHERE id=%s""",
        (j({value["profile_id"]: value}), aid),
    )


def last_for(c, profile_id):
    row = one(
        c,
        "SELECT onboarding_state->'browser_challenges'->%s AS challenge FROM lead.agents",
        (str(profile_id),),
    )
    return (row or {}).get("challenge") or {}


def waiting_result(challenge):
    return {
        "state": "needs_verification",
        "error": "HUMAN_VERIFICATION_REQUIRED",
        "challenge_id": challenge["id"],
        "platform": challenge["platform"],
        "reason": "平台要求真人验证，任务已暂停。请在社媒账号中打开原窗口完成验证。",
    }


def block(actor, platform, action_id=None):
    """Caller owns actor.lock. Keep account grants and action IDs unchanged."""
    with tx(actor.aid) as c:
        agent = require_agent(c, actor.aid, True)
        profile_id = getattr(actor, "profile", {}).get("id")
        challenge = pending(c, profile_id)
        if not challenge:
            account = one(
                c,
                "SELECT * FROM lead.social_accounts WHERE platform=%s AND (%s::uuid IS NULL OR profile_id=%s) ORDER BY created_at DESC LIMIT 1",
                (platform, profile_id, profile_id),
            )
            if not account:
                raise ValueError("验证页面没有对应的账号连接，请先连接账号")
            challenge = {
                "id": uid(),
                "state": "waiting_user",
                "platform": platform,
                "account_id": str(account["id"]),
                "profile_id": str(account["profile_id"]),
                "binding_version": account["binding_version"],
                "external_account_id": account["external_account_id"],
                "session_id": str(actor.session),
                "control_version": agent["control_version"],
                "config_revision": agent["config_revision"],
                "authorization_version": agent["authorization_version"],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            save(c, actor.aid, challenge)
            c.execute(
                "UPDATE lead.agents SET health_state='needs_attention' WHERE id=%s",
                (actor.aid,),
            )
            append_message(
                c,
                actor.aid,
                "assistant",
                waiting_result(challenge)["reason"],
                cards=["connect"],
            )
            record(c, actor.aid, "等待真人验证", platform + "：原页面与任务已保留")
            audit(
                c,
                actor.aid,
                "browser.challenge.detected",
                {"challenge_id": challenge["id"], "platform": platform},
            )
        result = waiting_result(challenge)
        if action_id:
            c.execute(
                "UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'",
                (action_id,),
            )
            c.execute(
                """UPDATE lead.tasks SET state='waiting_user',result=coalesce(result,'{}') || %s
                WHERE id=(SELECT task_id FROM lead.actions WHERE id=%s)
                  AND state NOT IN ('succeeded','canceled')""",
                (j(result), action_id),
            )
        return result


def assert_session(challenge, actor):
    if (
        not challenge
        or challenge["session_id"] != str(actor.session)
        or actor.disconnected
    ):
        raise ValueError("原验证窗口已失效，任务保持暂停；请恢复原浏览器会话后重试")


def finish(actor, challenge_id, observed):
    """Called only after checking the original page and authenticated account."""
    with tx(actor.aid) as c:
        agent = require_agent(c, actor.aid, True)
        challenge = pending(c, getattr(actor, "profile", {}).get("id"))
        assert_session(challenge, actor)
        if challenge["id"] != challenge_id:
            raise ValueError("验证请求已变化，请重新打开原窗口")
        account = one(
            c,
            "SELECT * FROM lead.social_accounts WHERE id=%s FOR UPDATE",
            (challenge["account_id"],),
        )
        if (
            not account
            or account["state"] == "disconnected"
            or account["external_account_id"] != challenge["external_account_id"]
            or account["binding_version"] != challenge["binding_version"]
        ):
            raise ValueError("账号绑定已改变，不能恢复原任务")
        if (
            challenge["external_account_id"]
            and observed["external_account_id"] != challenge["external_account_id"]
        ):
            raise ValueError("当前登录账号与原任务不同，请切回原账号完成验证")
        unchanged = all(
            agent[key] == challenge[key]
            for key in ("control_version", "config_revision", "authorization_version")
        )
        resume = (
            unchanged
            and agent["desired_state"] == "running"
            and account["automation_enabled"]
            and account["state"] == "connected"
        )
        tasks = all_rows(
            c,
            """SELECT t.id,a.state AS action_state FROM lead.tasks t
            LEFT JOIN lead.actions a ON a.task_id=t.id
            WHERE t.state='waiting_user' AND t.result->>'challenge_id'=%s
              AND (a.id IS NULL OR a.state IN ('prepared','unknown','dispatching'))""",
            (challenge_id,),
        )
        resumed = 0
        if resume:
            for task in tasks:
                # A new workflow invocation, but the same task/action/operation key.
                state = (
                    "reconciling"
                    if task["action_state"] in ("unknown", "dispatching")
                    else "queued"
                )
                c.execute(
                    "UPDATE lead.tasks SET state=%s,result=(coalesce(result,'{}')-'error'-'reason') || %s WHERE id=%s",
                    (
                        state,
                        j({"state": state, "challenge_resolved": True}),
                        task["id"],
                    ),
                )
                emit(
                    c,
                    actor.aid,
                    "task.reconcile",
                    {"task_id": str(task["id"]), "recovery_id": challenge_id},
                )
                resumed += 1
        resolved = {
            **challenge,
            "state": "resolved",
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resumed_tasks": resumed,
            "finish_login": not challenge["external_account_id"]
            or account["state"] != "connected",
        }
        save(c, actor.aid, resolved)
        if not resolved["finish_login"]:
            # Initial isolated login still needs its live takeover for account binding.
            c.execute(
                "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE browser_session_id=%s AND state IN ('active','requested','ending')",
                (actor.session,),
            )
        record(
            c,
            actor.aid,
            "真人验证已通过",
            "原任务继续执行，已提交动作先核对回执"
            if resume
            else "保留当前暂停与授权状态，未自动启动任务",
        )
        audit(
            c,
            actor.aid,
            "browser.challenge.resolved",
            {"challenge_id": challenge_id, "resumed_tasks": resumed},
        )
        if resume:
            emit(c, actor.aid, "supervisor.wake")
        return {
            "ok": True,
            "resumed_tasks": resumed,
            "account_id": challenge["account_id"],
            "finish_login": resolved["finish_login"],
        }
