"""Revalidate immutable, unsubmitted rule assessments under current authority.

Control changes never revive old task payloads. The caller creates a new read
with current versions and checks authority again before any external action.
"""

from . import community_health
from .db import all_rows


def next_rule_review(c, aid, account, revision):
    if not aid or not account:
        return None
    # One newest observation per bound account/target, regardless of discovery
    # batch. A later attempt, including a failed or interrupted one, supersedes
    # older receipts. Attempts are bounded; explicit restrictions stay terminal.
    due = c.execute(
        """WITH history AS (
        SELECT t.*,lower(input#>>'{candidate,telegram_url}') AS url,
               row_number() OVER (PARTITION BY lower(input#>>'{candidate,telegram_url}')
                                  ORDER BY created_at DESC,id DESC) AS url_n,
               count(*) FILTER (WHERE input#>>'{candidate,rule_review_parent}' IS NOT NULL)
                 OVER (PARTITION BY coalesce(result->>'target_id',lower(input#>>'{candidate,telegram_url}'))) AS attempts,
               bool_or(result ?| ARRAY['join_dispatched_at','reply_dispatched_at','leave_state']
                   OR result->>'join_state' IN ('unknown','dispatching','pending_approval')
                   OR result->>'reply_state' IN ('unknown','dispatching'))
                 OVER (PARTITION BY coalesce(result->>'target_id',lower(input#>>'{candidate,telegram_url}'))) AS external_intent,
               row_number() OVER (PARTITION BY coalesce(result->>'target_id',lower(input#>>'{candidate,telegram_url}'))
                                 ORDER BY created_at DESC,id DESC) AS n
        FROM lead.tasks t WHERE agent_id=%s AND type='community.engage'
          AND input->>'external_account_id'=%s AND input->>'account_id'=%s)
        SELECT recent.* FROM history recent
        JOIN lead.agents a ON a.id=recent.agent_id
        JOIN lead.interaction_cards card ON card.id::text=recent.input->>'delegation_id'
          AND card.agent_id=recent.agent_id AND card.type='manage_community'
          AND card.state='accepted' AND card.expires_at>now()
          AND card.payload->>'purpose'='bounded_candidate_participation'
          AND card.payload->>'account_id'=recent.input->>'account_id'
          AND card.payload->>'external_account_id'=recent.input->>'external_account_id'
          AND card.payload->'allowed_actions' ?& ARRAY['join_public_group','read_recent_history']
        JOIN lead.tasks d ON d.id::text=recent.input#>>'{candidate,discovery_task_id}' AND d.agent_id=a.id
          AND d.type='community.discover' AND (d.state='succeeded' OR (d.state='running' AND recent.input#>>'{candidate,handoff_id}' IS NOT NULL))
          AND d.input->>'account_id'=recent.input->>'account_id'
          AND d.input->>'external_account_id'=recent.input->>'external_account_id'
        JOIN lead.tasks anchor ON anchor.id::text=card.payload->>'discovery_task' AND anchor.agent_id=a.id
          AND anchor.type='community.discover' AND anchor.state='succeeded'
          AND anchor.input->>'account_id'=recent.input->>'account_id'
          AND anchor.input->>'external_account_id'=recent.input->>'external_account_id'
        WHERE n=1 AND url_n=1 AND NOT coalesce(external_intent,false) AND NOT recent.input ? 'reply' AND attempts<3 AND recent.state='succeeded'
          AND a.desired_state='running' AND a.config_revision::text=%s
          AND recent.input->>'config_revision'=a.config_revision::text
          AND recent.result#>>'{outcome,reason}' IN ('仍有未读完整的置顶内容或外部群规，暂缓','群规指向另一个入口，尚未取得完整条款',
          '群资料仅有名称或链接，尚无可核对的参与说明','群规含禁广告、禁推广或其他参与限制，跳过')
          AND (d.id=anchor.id OR (card.payload->>'continue_discovery'='true' AND d.created_at>=anchor.created_at))
          AND EXISTS(SELECT FROM jsonb_array_elements(d.result->'candidates') cand WHERE lower(cand->>'telegram_url')=url)
          AND NOT EXISTS(SELECT FROM history prior WHERE (prior.url=recent.url OR prior.result->>'target_id'=recent.result->>'target_id')
               AND (prior.input ? 'reply' OR prior.state IN ('running','queued','planned','reconciling')
                 OR prior.result ?| ARRAY['join_dispatched_at','reply_dispatched_at','leave_state']
                 OR prior.result->>'join_submitted'='true'
                 OR prior.result->>'join_state' IN ('unknown','dispatching','pending_approval')
                 OR prior.result->>'reply_state' IN ('unknown','dispatching','confirmed')
                 OR prior.result#>>'{decision,automation_forbidden}'='true'
             OR prior.result#>>'{outcome,reason}'='群规明确禁止自动化参与，跳过'))
          AND recent.completed_at + CASE attempts WHEN 0 THEN interval '30 minutes' WHEN 1 THEN interval '6 hours'
                                  ELSE interval '24 hours' END <=now()
          AND """
        + community_health.eligible_sql("recent")
        + """
        ORDER BY recent.completed_at,recent.id""",
        (aid, account["external_account_id"], str(account["id"]), str(revision)),
    )
    for row in due:
        if row["input"].get("candidate", {}).get("handoff_id"):
            from .community_handoff import valid_handoff
            if not valid_handoff(c, aid, account, revision, row["input"]["candidate"]):
                continue
        # The read timer can expire while the stricter write gate remains.
        # A held group must not hide another group's due rule check.
        if not community_health.blocked(c, aid, row, write=False):
            return {
                **row["input"]["candidate"],
                "rule_review_parent": str(row["id"]),
                "rule_review_control_version": row["input"].get("control_version"),
            }
    return None


def discovery_pressure(c, agent, config, now):
    """Prefer eligible bounded work, with a small periodic exploration window."""
    if (
        config.mode != "contextual_outreach"
        or not config.allow_join
        or agent["desired_state"] != "running"
    ):
        return None
    # Keep one two-minute exploration window per quarter hour; a stalled
    # participation lane must not permanently suppress independent discovery.
    if now.minute % 15 < 2:
        return None
    aid = str(agent["id"])
    accounts = all_rows(
        c,
        """SELECT DISTINCT s.id,s.external_account_id FROM lead.social_accounts s
        JOIN lead.interaction_cards card ON card.agent_id=s.agent_id AND card.type='manage_community'
          AND card.state='accepted' AND card.expires_at>now()
          AND card.payload->>'purpose'='bounded_candidate_participation'
          AND card.payload->>'account_id'=s.id::text AND card.payload->>'external_account_id'=s.external_account_id
          AND card.payload->'allowed_actions' ?& ARRAY['join_public_group','read_recent_history']
        WHERE s.agent_id=%s AND s.platform='telegram' AND s.state='connected' AND s.automation_enabled""",
        (aid,),
    )
    for account in accounts:
        if next_rule_review(c, aid, account, agent["config_revision"]):
            return {"reason": "rule_review_ready", "ready": 1, "delay": 120}
        rows = all_rows(
            c,
            """SELECT DISTINCT ON (lower(item->>'telegram_url')) item FROM lead.tasks d
            CROSS JOIN LATERAL jsonb_array_elements(d.result->'candidates') item
            JOIN lead.interaction_cards card ON card.agent_id=d.agent_id AND card.type='manage_community'
              AND card.state='accepted' AND card.expires_at>now()
              AND card.payload->>'purpose'='bounded_candidate_participation'
              AND card.payload->>'account_id'=d.input->>'account_id'
              AND card.payload->>'external_account_id'=d.input->>'external_account_id'
              AND card.payload->'allowed_actions' ?& ARRAY['join_public_group','read_recent_history']
            JOIN lead.tasks anchor ON anchor.id::text=card.payload->>'discovery_task' AND anchor.agent_id=d.agent_id
            WHERE d.agent_id=%s AND d.type='community.discover' AND d.state='succeeded'
              AND d.input->>'account_id'=%s AND d.input->>'external_account_id'=%s
              AND d.input->>'config_revision'=%s AND d.completed_at>now()-interval '7 days'
              AND (d.id=anchor.id OR (card.payload->>'continue_discovery'='true' AND d.created_at>=anchor.created_at))
              AND NOT EXISTS(SELECT FROM lead.tasks t WHERE t.agent_id=d.agent_id AND t.type='community.engage'
                AND t.input->>'external_account_id'=d.input->>'external_account_id'
                AND lower(t.input#>>'{candidate,telegram_url}')=lower(item->>'telegram_url'))
            ORDER BY lower(item->>'telegram_url') LIMIT 100""",
            (
                aid,
                str(account["id"]),
                account["external_account_id"],
                str(agent["config_revision"]),
            ),
        )
        ready = sum(
            not community_health.blocked(
                c,
                aid,
                {
                    "input": {
                        "external_account_id": account["external_account_id"],
                        "candidate": row["item"],
                    }
                },
                write=False,
            )
            for row in rows
        )
        if ready >= config.participation_policy.ready_backlog:
            return {"reason": "candidate_backlog", "ready": ready, "delay": 120}
    return None
