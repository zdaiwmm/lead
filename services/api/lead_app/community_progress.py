"""Business evidence and an independent, bounded stale-work watchdog."""

from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

from .db import all_rows, emit, j, one, record, tx
from .scope import current_tenant, service_scope, tenant_scope


def capture(c, aid, agent):
    from .community_health import canonical_url

    progress = dict(agent["onboarding_state"].get("community_progress", {}))
    events = []
    rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type IN ('community.engage','community.discover')
        AND created_at>now()-interval '30 days' ORDER BY created_at DESC LIMIT 300""")
    for task in rows:
        saved, payload = task["result"] or {}, task["input"]
        at = task["completed_at"] or task["created_at"]
        if task["type"] == "community.discover":
            for item in saved.get("candidates", []):
                events.append(("qualified_candidate", canonical_url(item["telegram_url"]), at, str(task["id"])))
        else:
            account = payload.get("external_account_id", "")
            peer = saved.get("target_id") or canonical_url(payload.get("candidate", {}).get("telegram_url"))
            if saved.get("join_state") == "confirmed" and saved.get("join_confirmed_at"):
                events.append(("membership_confirmed", account+":"+peer, datetime.fromisoformat(saved["join_confirmed_at"]), str(task["id"])))
            if saved.get("reply_state") == "confirmed" and saved.get("reply_external_id"):
                events.append(("message_confirmed", account+":"+saved["reply_external_id"], datetime.fromisoformat(saved.get("reply_confirmed_at") or saved.get("reply_dispatched_at") or at.isoformat()), str(task["id"])))
            window = saved.get("click_windows", {}).get("24h", {})
            if window.get("clicks"):
                events.append(("filtered_click", str(payload.get("reply", {}).get("tracking_link_id"))+":"+str(window["clicks"]), datetime.fromisoformat(window["until"]), str(task["id"])))
    keyed = {}
    for kind, key, at, ref in sorted(events, key=lambda item: item[2]):
        eid = uuid5(NAMESPACE_URL, f"{current_tenant()}:{aid}:community-progress:{kind}:{key}")
        keyed.setdefault(eid, (kind, at, ref))
    existing = {row["id"] for row in all_rows(c, "SELECT id FROM lead.memory_entries WHERE id=ANY(%s::uuid[])", (list(keyed),))} if keyed else set()
    for eid, (kind, at, ref) in keyed.items():
        if eid in existing:
            continue
        new = one(c, """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
            VALUES(%s,%s,%s,'observation',%s,%s,'system_observed') ON CONFLICT(id) DO NOTHING RETURNING id""",
            (eid, current_tenant(), aid, j({"type": "community_progress_evidence", "progress_kind": kind, "at": at.isoformat()}), j([ref])))
        if new and (not progress.get("last_progress_at") or at > datetime.fromisoformat(progress["last_progress_at"])):
            progress.update(last_progress_at=at.isoformat(), progress_kind=kind, evidence_ref=ref)
    blocked = all_rows(c, """SELECT body FROM lead.memory_entries WHERE body->>'type'='community_target_health'
        AND body->>'state'='blocked' ORDER BY body->>'read_after' LIMIT 20""")
    progress["blocked_scopes"] = [{k: row["body"].get(k) for k in ("account", "peer", "error_code", "next_action", "read_after", "write_after")} for row in blocked]
    if progress != agent["onboarding_state"].get("community_progress", {}):
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s", (j({"community_progress": progress}), aid))
    return progress


def inspect_stalled(c, aid, now=None):
    from .community_health import failed

    now = now or datetime.now(timezone.utc)
    # Waits, quarantined receipts and active leases are not failures. Never
    # duplicate a live Activity; a dead worker's lease expires independently.
    if one(c, "SELECT 1 FROM lead.admin_runtime_leases WHERE agent_id=%s AND released_at IS NULL AND expires_at>now()", (aid,)):
        return 0
    rows = all_rows(c, """SELECT * FROM lead.tasks WHERE type IN ('community.engage','community.discover')
        AND state IN ('running','reconciling') AND coalesce(result->>'receipt_quarantined','false')<>'true'
        AND coalesce((result->>'receipt_review_after')::timestamptz,(result->>'retry_after')::timestamptz,scheduled_at,created_at)<%s
        ORDER BY created_at LIMIT 20 FOR UPDATE""", (now-timedelta(seconds=60),))
    wakes = 0
    for task in rows:
        saved = task["result"] or {}
        watch = dict(saved.get("watchdog", {}))
        checkpoint = saved.get("checkpoint") or {}
        # An episode resets only on a completed stage, not a new heartbeat.
        episode = checkpoint.get("at") if checkpoint.get("state") == "completed" else watch.get("episode") or task["created_at"].isoformat()
        if watch.get("episode") != episode:
            watch = {"episode": episode}
        age = now-datetime.fromisoformat(episode)
        patch = {}
        if age >= timedelta(hours=2) and not watch.get("isolated_at"):
            if task["type"] == "community.engage":
                failed(c, aid, task, {"error_code": "stalled_checkpoint", "stage": checkpoint.get("stage", "observe"), "scope": "target", "retryable": True,
                                      "write_may_have_happened": saved.get("reply_state") in {"dispatching", "unknown"}, "evidence_ref": episode, "retry_after": 21600})
            watch["isolated_at"] = now.isoformat()
            patch["receipt_review_after"] = (now+timedelta(hours=6)).isoformat()
            record(c, aid, "任务长时间没有推进", "已保留原动作与检查点，隔离受影响目标并继续其他工作。", "warning", task_id=task["id"])
        elif age >= timedelta(minutes=30) and not watch.get("recovery_at"):
            watch["recovery_at"] = now.isoformat()
            # Resume the original intent, never a new write task.
            if saved.get("reply_state") in {"unknown", "dispatching"} or saved.get("join_state") in {"unknown", "dispatching"}:
                c.execute("UPDATE lead.tasks SET state='reconciling' WHERE id=%s", (task["id"],))
            patch["receipt_review_after"] = now.isoformat()
            emit(c, aid, "supervisor.wake")
            wakes += 1
        elif not watch.get("wake_at"):
            watch["wake_at"] = now.isoformat()
            emit(c, aid, "supervisor.wake")
            wakes += 1
        c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s", (j({**patch, "watchdog": watch}), task["id"]))
    return wakes


def maintain():
    from .identity_store import execution_tenants

    with service_scope():
        for tenant in execution_tenants():
            with tenant_scope(tenant, None), tx() as c:
                agents = all_rows(c, "SELECT id FROM lead.agents WHERE desired_state='running'")
            for agent in agents:
                with tenant_scope(tenant, None), tx(str(agent["id"])) as c:
                    inspect_stalled(c, str(agent["id"]))
