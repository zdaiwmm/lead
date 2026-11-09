"""Choose a concrete operation before pricing it; executor honors this choice."""

from datetime import datetime, timedelta, timezone

from . import community_health
from .db import all_rows, one
from .growth_experiments import chamber_campaign, first_users


def recovery_share(c, account_id):
    row = one(c, """SELECT
        coalesce(sum((step->>'duration_ms')::float) FILTER (WHERE step->>'recovery'='true' OR step->>'phase' IN ('browser_receipt','browser_scan_pending') OR (step->>'phase'='browser_observe' AND step->>'state'='failed')),0) AS receipt,
        coalesce(sum((step->>'duration_ms')::float),0) AS total
        FROM lead.tasks t CROSS JOIN LATERAL jsonb_array_elements(coalesce(t.result->'browser_usage',t.result->'execution_steps','[]')) step
        WHERE t.type='community.engage' AND (step->>'at')::timestamptz>now()-interval '1 hour'
          AND t.input->>'account_id'=%s AND step->>'phase' LIKE 'browser_%%'""", (account_id,))
    return row["receipt"] / row["total"] if row["total"] else 0


def next_readonly_visit(c, excluded_accounts=(), *, schedule=False):
    """A write hold still permits a visit backed by a current delegation and confirmed membership."""
    sql = """WITH members AS (
        SELECT DISTINCT ON (t.input->>'external_account_id',t.result->>'target_id') t.*
        FROM lead.tasks t WHERE t.type='community.engage' AND t.result->>'join_state'='confirmed'
          AND (t.result->>'join_submitted'='true' OR t.result->>'membership_origin'='existing')
          AND t.result->>'target_id' ~ '^-[0-9]+$'
        ORDER BY t.input->>'external_account_id',t.result->>'target_id',t.created_at DESC,t.id DESC),
        probing AS (
            SELECT DISTINCT agent_id,body->>'account' AS account FROM lead.memory_entries
            WHERE kind='observation' AND body->>'type'='community_target_health'
              AND body->>'peer'='*' AND body->>'state'='blocked'
              AND body->>'error_code' IN ('browser_error','navigation_timeout')
              AND body#>>'{fault,stage}' IN ('observe','receipt')
              AND coalesce(body#>>'{fault,retry_after}','0')='0'
              AND (body->>'read_after')::timestamptz<=now()
              AND (body->>'last_failure_at')::timestamptz >=
                  coalesce((body->>'last_read_at')::timestamptz,'-infinity'))
        SELECT t.*,t.id AS membership_id,
            coalesce(probing.account IS NOT NULL AND v.state='succeeded'
              AND v.result#>>'{history_observation,joined}'='true'
              AND coalesce(v.result#>>'{history_observation,verification_required}','false')='false'
              AND (v.result->>'history_observed_at')::timestamptz BETWEEN now()-interval '24 hours'
                  AND now()-interval '5 minutes',false) AS account_probe,
            greatest(coalesce((v.result->>'history_observed_at')::timestamptz +
               CASE WHEN v.result#>>'{history_observation,new_messages}'='0' THEN interval '24 hours'
                    WHEN (v.result#>>'{history_observation,new_messages}')::int >= 6 THEN interval '15 minutes'
                    WHEN (v.result#>>'{history_observation,new_messages}')::int > 0 THEN interval '30 minutes'
                    ELSE interval '1 hour' END, t.created_at),
                CASE WHEN v.state='failed' THEN coalesce((v.result->>'retry_after')::timestamptz,
                    v.created_at + interval '15 minutes') END) AS due
        FROM members t JOIN lead.social_accounts a ON a.id::text=t.input->>'account_id'
          AND a.agent_id=t.agent_id AND a.external_account_id=t.input->>'external_account_id'
          AND a.state='connected' AND a.automation_enabled
        JOIN lead.interaction_cards card ON card.id::text=t.input->>'delegation_id'
          AND card.state='accepted' AND card.type='manage_community' AND card.expires_at>now()
          AND card.payload->>'external_account_id'=a.external_account_id
        LEFT JOIN probing ON probing.agent_id=t.agent_id AND probing.account=a.external_account_id
        LEFT JOIN LATERAL (SELECT v.result,v.state,v.created_at FROM lead.tasks v
          WHERE v.agent_id=t.agent_id AND v.type=t.type
            AND v.input->>'external_account_id'=a.external_account_id
            AND (v.result->>'target_id'=t.result->>'target_id' OR v.input->>'membership_task_id'=t.id::text)
          ORDER BY v.created_at DESC,v.id DESC LIMIT 1) v ON true
        WHERE NOT (a.id::text=ANY(%s::text[])) AND """ + community_health.eligible_sql("t") + """
          AND coalesce(t.result->>'leave_state','') NOT IN ('confirmed','dispatching','unconfirmed')
          AND coalesce(v.result->>'leave_state','') NOT IN ('confirmed','dispatching','unconfirmed')
          AND coalesce(v.result#>>'{decision,automation_forbidden}','false')<>'true'
        ORDER BY account_probe DESC,due,t.id"""
    if schedule:
        return all_rows(c, "SELECT result->>'target_id' AS peer,due FROM ("+sql+") AS eligible ORDER BY due,peer",
                        (list(excluded_accounts),))
    return one(c, sql+" LIMIT 1", (list(excluded_accounts),))


def choose(c, agent, config, now=None, *, allow_candidates=True, allow_voice_edits=True, read_only=False):
    from .community_runtime import membership_review, next_candidate, next_voice_edit
    from .community_schedule import next_join_check, next_pending_inventory
    from .operating_kernel import blocked

    now = now or datetime.now(timezone.utc)
    if agent["desired_state"] != "running" or not config.enabled or config.mode != "contextual_outreach":
        return None
    aid = str(agent["id"])
    community_health.bootstrap(c, aid)
    fences = blocked(c, community_worker=True)
    if fences["unknown_write_scope"]:
        return None
    if read_only:
        # A successful bound read can leave the legacy six-hour probe interval
        # on the entire account. Keep its write gate and each group's own hold.
        c.execute("""UPDATE lead.memory_entries
            SET body=jsonb_set(body,'{read_after}',body->'last_read_at')
            WHERE agent_id=%s AND kind='observation' AND body->>'type'='community_target_health'
              AND body->>'peer'='*' AND body->>'state'='blocked' AND body->>'error_code'='browser_error'
              AND body#>>'{fault,error_code}'='browser_error'
              AND coalesce(body#>>'{fault,retry_after}','0')='0'
              AND coalesce(body#>>'{fault,write_may_have_happened}','false')='false'
              AND body#>>'{last_permission_evidence,joined}'='true'
              AND body#>>'{last_permission_evidence,can_reply}'='true'
              AND coalesce(body#>>'{last_permission_evidence,verification_required}','false')='false'
              AND (body->>'last_read_at')::timestamptz > (body->>'last_failure_at')::timestamptz
              AND (body->>'last_read_at')::timestamptz <= now()
              AND (body->>'read_after')::timestamptz = (body->>'last_read_at')::timestamptz + interval '6 hours'
              AND (body->>'read_after')::timestamptz > now()""", (aid,))
    diagnostic = one(c, """SELECT t.* FROM lead.memory_entries h JOIN lead.tasks t ON t.id::text=h.body->>'task_id'
        WHERE h.body->>'type'='community_target_health' AND h.body->>'state'='blocked'
        AND (h.body->>'read_after')::timestamptz<=now()
        AND coalesce((t.result->>'diagnostic_after')::timestamptz,'-infinity')<=now()
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
    resumed = None
    for task in active:
        saved = task["result"] or {}
        is_receipt = task["state"] == "reconciling" or saved.get("reply_state") in {"unknown", "dispatching"} or saved.get("join_state") in {"unknown", "dispatching", "pending_approval"}
        if is_receipt:
            receipts.append({"kind": "receipt", "task_id": str(task["id"]), "account_id": task["input"]["account_id"], "paid": False})
        elif not resumed and task["input"].get("observation_only"):
            resumed = {"kind": "resume", "task_id": str(task["id"]), "paid": False, "read_only": True}
        elif not read_only and not resumed and saved.get("reply_state") != "failed" and not community_health.blocked(c, aid, task):
            resumed = {"kind": "resume", "task_id": str(task["id"]), "paid": not bool(task["input"].get("reply"))}
    pending = next_pending_inventory(c) if chamber_campaign(agent) else next_join_check(c)
    if pending and pending["due"] <= now:
        pending_task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (pending["id"],))
        if pending_task["input"].get("account_id") not in fences["accounts"] and not community_health.blocked(c, aid, pending_task, write=False):
            receipts.append({"kind": "inventory" if chamber_campaign(agent) else "receipt", "task_id": str(pending["id"]), "account_id": pending_task["input"]["account_id"], "paid": False})
    if read_only:
        review = next_readonly_visit(c, fences["accounts"])
        if review and review["due"] > now and not review["account_probe"]:
            review = None
    else:
        review = membership_review(c, focus=first_users(agent), exit_restricted=config.allow_leave_restricted,
                                   proactive=config.allow_proactive_post, excluded_accounts=fences["accounts"])
    account_probe = bool(read_only and review and review["account_probe"])
    urgent_review = bool(review and (account_probe or review["due"] <= now - timedelta(minutes=30)))
    opportunity = resumed
    if review and (urgent_review or not resumed):
        opportunity = {"kind": "review", "task_id": str(review["id"]), "membership_id": str(review["membership_id"]),
                       "paid": not read_only, "overdue": urgent_review, "read_only": read_only}
        if account_probe:
            # Use a recently working group for the first bound read. Retrying
            # the timed-out receipt first can renew the whole account's hold.
            return opportunity
    if not opportunity and allow_voice_edits and not read_only:
        revision = next_voice_edit(c, agent)
        if revision:
            opportunity = {"kind": "correct_voice", "task_id": str(revision["id"]), "paid": False}
    if not opportunity and allow_candidates and not read_only:
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
    recovery = receipts[0] if receipts else None
    if not recovery and diagnostic:
        recovery = {"kind": "diagnose", "task_id": str(diagnostic["id"]),
                    "account_id": diagnostic["input"]["account_id"], "paid": False}
    if recovery:
        account = recovery["account_id"]
        if not opportunity or recovery_share(c, account) < 0.25:
            return recovery
        if recovery["kind"] in ("receipt", "inventory"):
            # Reserve one bounded check per account/hour even during a long backlog.
            last_check = one(c, """SELECT max((result->>'receipt_attempted_at')::timestamptz) AS at
                FROM lead.tasks WHERE type='community.engage' AND input->>'account_id'=%s""", (account,))["at"]
            if not last_check or last_check <= now - timedelta(hours=1):
                return recovery
    if opportunity:
        return opportunity
    if read_only:
        return None
    retry = one(c, """SELECT * FROM lead.tasks t WHERE type='community.engage' AND state='failed'
        AND """ + community_health.eligible_sql() + """
        AND NOT (input->>'account_id'=ANY(%s)) AND result->>'reply_state' IS NULL
        AND coalesce(result->>'join_state','confirmed')='confirmed'
        AND result ? 'retry_after' AND (result->>'retry_after')::timestamptz<=now()
        AND (t.input ? 'reply' OR NOT EXISTS (
            SELECT 1 FROM lead.tasks newer WHERE newer.agent_id=t.agent_id AND newer.type=t.type
              AND newer.created_at>t.created_at AND newer.state='succeeded' AND newer.result ? 'decision'
              AND newer.input->>'external_account_id'=t.input->>'external_account_id'
              AND newer.input->>'config_revision'=t.input->>'config_revision'
              AND (newer.result->>'target_id'=t.result->>'target_id' OR
                   lower(newer.input#>>'{candidate,telegram_url}')=lower(t.input#>>'{candidate,telegram_url}'))
              AND coalesce(newer.result#>>'{history_observation,joined}','false')='true'
        ))
        ORDER BY (result->>'retry_after')::timestamptz,id LIMIT 1""", (fences["accounts"],))
    return {"kind": "resume", "task_id": str(retry["id"]), "paid": not bool(retry["input"].get("reply"))} if retry else None
