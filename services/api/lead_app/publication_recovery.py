"""Fence the identity that made an uncertain write, never its replacement."""

from datetime import datetime, timedelta, timezone

from .db import all_rows, emit, j, one, uid


def stale_identity_tasks(c):
    return {
        str(r["task_id"])
        for r in all_rows(
            c,
            """SELECT a.task_id FROM lead.actions a
      JOIN lead.account_targets t ON t.id=a.target_id JOIN lead.social_accounts s ON s.id=t.account_id
      LEFT JOIN LATERAL (SELECT observed_account_id FROM lead.action_attempts
        WHERE action_id=a.id ORDER BY attempt DESC LIMIT 1) p ON true
      WHERE a.state IN ('dispatching','unknown') AND (s.state='disconnected' OR p.observed_account_id<>s.external_account_id)""",
        )
    }


def pending_writes(c):
    stale = stale_identity_tasks(c)
    return [
        r
        for r in all_rows(
            c,
            """SELECT id,type,input FROM lead.tasks
      WHERE state IN ('queued','running','reconciling') AND
      (type='account.prepare' OR type='connection.verify_publish' OR type LIKE '%%.publish' OR (type='community.engage' AND state<>'reconciling'))""",
        )
        if str(r["id"]) not in stale
    ]


def due(c, now=None):
    now = now or datetime.now(timezone.utc)
    stale = stale_identity_tasks(c)
    tasks = all_rows(
        c,
        """SELECT t.id,t.result FROM lead.tasks t JOIN lead.actions a ON a.task_id=t.id
      WHERE t.state='reconciling' AND a.state IN ('unknown','dispatching') ORDER BY t.created_at""",
    )
    return [
        t
        for t in tasks
        if str(t["id"]) not in stale
        and (
            not (t["result"] or {}).get("recovery_next_at")
            or datetime.fromisoformat(t["result"]["recovery_next_at"]) <= now
        )
    ]


def request(c, aid, task_id):
    task = one(c, "SELECT result FROM lead.tasks WHERE id=%s FOR UPDATE", (task_id,))
    result = task["result"] or {}
    attempts = result.get("recovery_attempts", 0) + 1
    delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
    result.update(
        recovery_attempts=attempts,
        recovery_next_at=(
            datetime.now(timezone.utc) + timedelta(seconds=delay)
        ).isoformat(),
    )
    c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j(result), task_id))
    emit(c, aid, "task.reconcile", {"task_id": str(task_id), "recovery_id": uid()})
    return {
        "delay": delay,
        "reason": "reconciling_original_publication",
        "task_id": str(task_id),
    }
