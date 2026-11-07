"""Committed stage boundaries and receipt counters, independent of RPC success."""

from datetime import datetime, timedelta, timezone

from .db import j, one, record, tx


def begin(aid, task_id, stage):
    now = datetime.now(timezone.utc).isoformat()
    with tx(aid) as c:
        row = one(c, "SELECT result FROM lead.tasks WHERE id=%s FOR UPDATE", (task_id,))
        saved = row["result"] or {}
        patch = {"checkpoint": {"stage": stage, "state": "started", "at": now}}
        if stage == "receipt":
            # Commit before calling the gateway: crashes and timeouts count too.
            patch.update(receipt_checks=int(saved.get("receipt_checks", 0))+1, receipt_attempted_at=now,
                         receipt_prior_state=saved.get("reply_state") or saved.get("join_state") or "unknown")
            sent = saved.get("reply_dispatched_at") or saved.get("join_dispatched_at")
            if sent and datetime.fromisoformat(now)-datetime.fromisoformat(sent) >= timedelta(days=3) and patch["receipt_prior_state"] in {"dispatching", "unknown"}:
                patch["receipt_quarantined"] = True
                if not saved.get("receipt_alerted_at"):
                    patch["receipt_alerted_at"] = now
                    record(c, aid, "原动作仍缺少回执", "原提交已隔离，每 6 小时只读核对；其他目标继续。", "warning", task_id=task_id)
        c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s", (j(patch), task_id))
    return {**saved, **patch}


def finish_receipt(c, aid, task_id, outcome):
    now = datetime.now(timezone.utc)
    saved = one(c, "SELECT result FROM lead.tasks WHERE id=%s FOR UPDATE", (task_id,))["result"] or {}
    previous = saved.get("receipt_last_state") or saved.get("receipt_prior_state")
    state = outcome["state"]
    patch = {"receipt_reads": int(saved.get("receipt_reads", 0))+1, "receipt_last_state": state,
             "checkpoint": {"stage": "receipt", "state": "completed", "at": now.isoformat()}}
    if previous != state:
        patch.update(receipt_state_changes=int(saved.get("receipt_state_changes", 0))+1, receipt_changed_at=now.isoformat())
    sent = saved.get("reply_dispatched_at") or saved.get("join_dispatched_at")
    if state == "unknown" and sent and now-datetime.fromisoformat(sent) >= timedelta(days=3):
        patch["receipt_quarantined"] = True
        if not saved.get("receipt_alerted_at"):
            patch["receipt_alerted_at"] = now.isoformat()
            record(c, aid, "原动作仍缺少回执", "已隔离原提交，降为每 6 小时只读核对；其他目标继续。", "warning", task_id=task_id)
    c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s", (j(patch), task_id))
    return {**saved, **patch}
