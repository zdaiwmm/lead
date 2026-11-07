"""Choose a concrete operation before pricing it; executor honors this choice."""

from datetime import datetime, timezone

from . import community_health
from .db import all_rows, one
from .growth_experiments import chamber_campaign, first_users


def receipt_share(c):
    row = one(c, """SELECT
        coalesce(sum((step->>'duration_ms')::float) FILTER (WHERE step->>'phase' IN ('browser_receipt','browser_scan_pending')),0) AS receipt,
        coalesce(sum((step->>'duration_ms')::float),0) AS total
        FROM lead.tasks t CROSS JOIN LATERAL jsonb_array_elements(coalesce(t.result->'execution_steps','[]')) step
        WHERE t.type='community.engage' AND (step->>'at')::timestamptz>now()-interval '1 hour'
          AND step->>'phase' LIKE 'browser_%%'""")
    return row["receipt"] / row["total"] if row["total"] else 0


def choose(c, agent, config, now=None):
    from .community_runtime import membership_review, next_candidate, next_voice_edit
    from .community_schedule import next_join_check, next_pending_inventory
    from .operating_kernel import blocked

    now = now or datetime.now(timezone.utc)
    if agent["desired_state"] != "running" or not config.enabled or config.mode != "contextual_outreach":
        return None
    aid = str(agent["id"])
    community_health.bootstrap(c, aid)
    revision = next_voice_edit(c, agent)
    if revision:
        return {"kind": "correct_voice", "task_id": str(revision["id"]), "paid": False}
    fences = blocked(c, community_worker=True)
    if fences["unknown_write_scope"]:
        return None
    diagnostic = one(c, """SELECT t.* FROM lead.memory_entries h JOIN lead.tasks t ON t.id::text=h.body->>'task_id'
        WHERE h.body->>'type'='community_target_health' AND h.body->>'state'='blocked'
        AND (h.body->>'read_after')::timestamptz<=now()
        AND coalesce((t.result->>'receipt_review_after')::timestamptz,(t.result->>'retry_after')::timestamptz,now())<=now()
        AND """ + community_health.eligible_sql() + """ AND NOT(t.input->>'account_id'=ANY(%s))
        AND coalesce(t.result->>'reply_state','') NOT IN ('dispatching','unknown')
        AND coalesce(t.result->>'join_state','') NOT IN ('dispatching','unknown','pending_approval')
        ORDER BY h.body->>'read_after' LIMIT 1""", (fences["accounts"],))
    active = all_rows(c, """SELECT * FROM lead.tasks t WHERE type='community.engage'
        AND state IN ('running','reconciling') AND """ + community_health.eligible_sql() + """
        AND NOT (input->>'account_id'=ANY(%s))
        AND coalesce((result->>'receipt_review_after')::timestamptz,created_at)<=now()
        ORDER BY CASE WHEN input ? 'reply' AND result->>'reply_state' IS NULL THEN 0 ELSE 1 END,
          coalesce((result->>'receipt_review_after')::timestamptz,created_at),id""", (fences["accounts"],))
    receipts = []
    for task in active:
        saved = task["result"] or {}
        is_receipt = task["state"] == "reconciling" or saved.get("reply_state") in {"unknown", "dispatching"} or saved.get("join_state") in {"unknown", "dispatching", "pending_approval"}
        if is_receipt:
            receipts.append({"kind": "receipt", "task_id": str(task["id"]), "paid": False})
        else:
            return {"kind": "resume", "task_id": str(task["id"]), "paid": not bool(task["input"].get("reply"))}
    pending = next_pending_inventory(c) if chamber_campaign(agent) else next_join_check(c)
    if pending and pending["due"] <= now:
        pending_task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (pending["id"],))
        if pending_task["input"].get("account_id") not in fences["accounts"] and not community_health.blocked(c, aid, pending_task, write=False):
            receipts.append({"kind": "inventory" if chamber_campaign(agent) else "receipt", "task_id": str(pending["id"]), "paid": False})
    opportunity = None
    review = membership_review(c, focus=first_users(agent), exit_restricted=config.allow_leave_restricted, proactive=config.allow_proactive_post)
    if review and review["input"].get("account_id") not in fences["accounts"]:
        health = community_health.blocked(c, aid, review)
        opportunity = {"kind": "diagnose" if health else "review", "task_id": str(review["id"]), "membership_id": str(review["membership_id"]),
                       "paid": not bool(health)}
    if not opportunity:
        accounts = all_rows(c, """SELECT s.id,s.external_account_id FROM lead.social_accounts s
            JOIN lead.interaction_cards card ON card.type='manage_community' AND card.state='accepted'
            AND card.expires_at>now() AND card.payload->>'account_id'=s.id::text
            AND card.payload->>'external_account_id'=s.external_account_id
            WHERE s.platform='telegram' AND s.state='connected' AND s.automation_enabled
            AND NOT(s.id::text=ANY(%s)) ORDER BY s.id""", (fences["accounts"],))
        for account in accounts:
            candidate = next_candidate(c, agent["config_revision"], first_users(agent), chamber=chamber_campaign(agent),
                                       proactive=config.allow_proactive_post, account=account, aid=aid)
            if candidate:
                opportunity = {"kind": "candidate", "candidate": candidate, "account_id": str(account["id"]), "paid": True}
                break
    # Time spent includes failed reads. Borrow the whole lane if no fresh work.
    if receipts and (not opportunity or receipt_share(c) < 0.25):
        return receipts[0]
    if opportunity:
        return opportunity
    if diagnostic:
        return {"kind": "diagnose", "task_id": str(diagnostic["id"]), "paid": False}
    retry = one(c, """SELECT * FROM lead.tasks t WHERE type='community.engage' AND state='failed'
        AND """ + community_health.eligible_sql() + """
        AND NOT (input->>'account_id'=ANY(%s)) AND result->>'reply_state' IS NULL
        AND coalesce(result->>'join_state','confirmed')='confirmed'
        AND result ? 'retry_after' AND (result->>'retry_after')::timestamptz<=now()
        ORDER BY (result->>'retry_after')::timestamptz,id LIMIT 1""", (fences["accounts"],))
    return {"kind": "resume", "task_id": str(retry["id"]), "paid": not bool(retry["input"].get("reply"))} if retry else None
