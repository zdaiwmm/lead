"""Community deadlines belong to the product, independently of channel cadence."""

import math
import time
from datetime import datetime, timedelta, timezone

import psutil

from .community_growth import discovery_config, search_interval
from .db import j, one, record, require_agent, tx
from .growth_experiments import first_users

HEARTBEAT_SECONDS = 300
REVIEW_SECONDS = 3600


def receipt_wait_minutes(result, checks, now=None):
    """Direct joins get an early read; unknown sends retain their own backoff."""
    now = now or datetime.now(timezone.utc)
    last_change = result.get("receipt_changed_at") or result.get("reply_dispatched_at") or result.get("join_dispatched_at")
    if last_change and now - datetime.fromisoformat(last_change) >= timedelta(hours=6):
        return 360
    direct_join = result.get("join_mode") == "direct" and not result.get("reply_state")
    waits = (2, 10, 30) if direct_join else (10, 30, 60)
    return waits[min(2, max(0, checks))]



def approval_review_at(result, now):
    sent = datetime.fromisoformat(result.get("join_dispatched_at") or now.isoformat())
    age = now - sent
    minutes = 15 if age < timedelta(hours=1) else 120 if age < timedelta(days=1) else 1440
    return now + timedelta(minutes=minutes)


def next_join_check(c):
    """One clock for scheduling and selecting pending Telegram receipts."""
    return one(
        c,
        """WITH pending AS (
            SELECT id, (result->>'approval_review_after')::timestamptz AS explicit_due, coalesce((result->>'join_checked_at')::timestamptz, completed_at, created_at) AS checked,
                   coalesce((result->>'join_dispatched_at')::timestamptz, created_at) AS sent
            FROM lead.tasks WHERE type='community.engage' AND state IN ('succeeded','failed')
              AND (result->>'join_state'='pending_approval' OR (result->>'join_mode'='approval_request'
                   AND result->>'join_state' IN ('dispatching','unknown')))
        ) SELECT id, coalesce(explicit_due, checked + CASE
            WHEN checked < sent + interval '1 hour' THEN interval '15 minutes'
            WHEN checked < sent + interval '1 day' THEN interval '2 hours'
            ELSE interval '24 hours' END) AS due
        FROM pending ORDER BY due,id LIMIT 1""",
    )


def next_pending_inventory(c):
    # Scan all visible pending peers once per account per hour. A rotating
    # single-peer check covers groups outside Telegram's virtualized sidebar.
    return one(c, """SELECT t.id, CASE WHEN (t.result->>'inventory_seen_at')::timestamptz>
            coalesce((t.result->>'join_checked_at')::timestamptz,t.created_at)
          THEN greatest((t.result->>'inventory_seen_at')::timestamptz,
              coalesce(last.handled,'-infinity')+interval '2 minutes')
          ELSE coalesce((t.result->>'approval_review_after')::timestamptz, greatest(coalesce(last.checked,'-infinity')+interval '1 hour',
          coalesce((t.result->>'join_checked_at')::timestamptz,t.created_at)+interval '10 minutes')) END AS due
        FROM lead.tasks t LEFT JOIN LATERAL (
          SELECT max((s.result->>'inventory_checked_at')::timestamptz) AS checked,
            max((s.result->>'join_checked_at')::timestamptz) AS handled FROM lead.tasks s
          WHERE s.type='community.engage' AND s.input->>'external_account_id'=t.input->>'external_account_id'
          AND s.result ? 'inventory_checked_at'
        ) last ON true
        WHERE t.type='community.engage' AND t.state IN ('succeeded','failed') AND t.input ? 'account_id'
          AND t.result->>'join_submitted'='true' AND t.result->>'join_mode'='approval_request'
          AND t.result->>'join_state' IN ('unknown','dispatching','pending_approval')
        ORDER BY due,coalesce((t.result->>'join_checked_at')::timestamptz,t.created_at),t.id LIMIT 1""")


def next_membership_review(c, *, repair_legacy=False, focus=False, exit_restricted=False, proactive=False, excluded_accounts=()):
    """Schedule a fresh visit only with confirmed membership and no pending write."""
    from .community_health import eligible_sql

    return one(c, """WITH history AS (
        SELECT t.*,coalesce(t.result->>'target_id',m.result->>'target_id',lower(t.input#>>'{candidate,telegram_url}')) AS peer
        FROM lead.tasks t LEFT JOIN lead.tasks m ON m.id::text=t.input->>'membership_task_id'
        WHERE t.type='community.engage'
    ), latest AS (
        SELECT DISTINCT ON (input->>'external_account_id',peer) * FROM history
        ORDER BY input->>'external_account_id',peer,created_at DESC,id DESC
    ) SELECT recent.*, membership.id AS membership_id,
        CASE WHEN %s AND recent.state='failed' THEN greatest(coalesce((recent.result->>'retry_after')::timestamptz,'-infinity'),coalesce(recent.completed_at,recent.created_at)+
              CASE WHEN recent.input->>'read_recovery'='true' THEN interval '1 hour' ELSE interval '15 minutes' END)
             WHEN %s AND recent.result#>>'{history_observation,joined}'='true' AND recent.result#>>'{history_observation,can_reply}'='true'
                  AND (NOT (coalesce(recent.result->'decision','{}') ? 'proactive_reviewed')
                   OR ((coalesce(recent.result#>>'{decision,ad_feed_revision}','0')<>'2'
                         OR (recent.result#>>'{outcome,reason}'='本群最近已有投放，继续观察效果' AND recent.input#>>'{reply,placement}'='advertising_group' AND NOT (recent.result ? 'reply_state'))) AND EXISTS (
                       SELECT 1 FROM lead.tasks sent WHERE sent.type='community.engage'
                         AND sent.input->>'external_account_id'=recent.input->>'external_account_id'
                         AND sent.input#>>'{candidate,telegram_url}'=recent.input#>>'{candidate,telegram_url}'
                         AND sent.input#>>'{reply,placement}'='advertising_group'
                         AND sent.result->>'reply_state'='confirmed' AND coalesce(sent.result#>>'{voice_edit,state}','') NOT IN ('unavailable','waiting','submitted')
                         AND (sent.result->>'reply_dispatched_at')::timestamptz BETWEEN now()-interval '24 hours' AND now()-interval '2 hours')) ) THEN coalesce(recent.completed_at,recent.created_at)
             WHEN %s AND recent.result#>>'{history_observation,joined}'='true'
                  AND recent.result#>>'{history_observation,can_reply}'='false'
             THEN coalesce(recent.completed_at,recent.created_at) + CASE
               WHEN recent.result->>'leave_state'='deferred' AND coalesce(recent.result->>'exit_ui_version','0') IN ('0','2','3')
                    OR recent.result->>'leave_reason'='退出确认框含其他操作，等待只读核对' THEN interval '0 minutes'
               WHEN recent.result ? 'leave_state' THEN interval '6 hours' ELSE interval '30 minutes' END
             WHEN %s AND recent.state='succeeded' AND coalesce(recent.input->>'history_adapter_version','1')::integer<4
             THEN recent.created_at
             WHEN %s AND recent.state='succeeded' AND recent.result#>>'{history_observation,source}'='recent'
             AND (recent.result#>>'{history_observation,count}'='0' OR EXISTS (
                 SELECT 1 FROM lead.tasks prev WHERE prev.type='community.engage'
                 AND prev.id<>recent.id AND prev.input->>'external_account_id'=recent.input->>'external_account_id'
                 AND lower(prev.input#>>'{candidate,telegram_url}')=lower(recent.input#>>'{candidate,telegram_url}')
                 AND prev.result#>>'{history_observation,source}'='recent'
                 AND prev.result#>>'{history_observation,content_hash}'=recent.result#>>'{history_observation,content_hash}'
             )) THEN coalesce(recent.completed_at,recent.created_at)+interval '1 day'
             WHEN %s AND recent.result#>>'{history_observation,source}'='recent' THEN
               coalesce(recent.completed_at,recent.created_at) + CASE
                 WHEN (recent.result#>>'{history_observation,new_messages}')::int>=6 THEN interval '15 minutes'
                 WHEN (recent.result#>>'{history_observation,new_messages}')::int>0 THEN interval '30 minutes'
                 ELSE interval '1 hour' END
             ELSE coalesce(recent.completed_at,recent.created_at)+interval '1 hour' END AS due
        FROM latest recent JOIN LATERAL (
            SELECT id FROM lead.tasks m WHERE m.type='community.engage'
            AND (m.result->>'target_id'=recent.peer OR lower(m.input#>>'{candidate,telegram_url}')=lower(recent.input#>>'{candidate,telegram_url}'))
            AND m.input->>'external_account_id'=recent.input->>'external_account_id'
            AND (m.result->>'join_submitted'='true' OR m.result->>'membership_origin'='existing') AND m.result->>'join_state'='confirmed'
            ORDER BY m.created_at DESC LIMIT 1
        ) membership ON true
        WHERE """ + eligible_sql("recent", write=True) + """ AND coalesce(recent.result->>'leave_state','')<>'confirmed'
          AND NOT (coalesce(recent.input->>'account_id','')=ANY(%s::text[]))
          AND NOT EXISTS (SELECT 1 FROM lead.tasks orig WHERE orig.id=membership.id AND orig.result->>'leave_state'='confirmed')
          AND coalesce(recent.result->'decision'->>'automation_forbidden','false')<>'true'
          AND NOT EXISTS (SELECT 1 FROM history h WHERE h.peer=recent.peer
              AND h.input->>'external_account_id'=recent.input->>'external_account_id'
              AND (h.result->>'leave_state'='confirmed'
                  OR h.result->>'reply_state' IN ('unknown','dispatching'))
              )
          AND (((recent.state='succeeded' OR recent.result->>'reply_state'='failed') AND recent.result->'history_observation'->>'joined'='true')
            OR (recent.state='failed' AND recent.input->>'membership_task_id'=membership.id::text
                AND NOT (recent.input ? 'reply' OR recent.result ?| ARRAY['reply_state','join_state','join_submitted'])
                AND coalesce(recent.result#>>'{history_observation,joined}','true')<>'false'
                AND NOT (recent.result ? 'leave_state')))
        ORDER BY due,recent.id LIMIT 1""", (focus, proactive, exit_restricted, repair_legacy, focus, focus, list(excluded_accounts)))


def deadlines(c, agent, now=None):
    now = now or datetime.now(timezone.utc)
    config = discovery_config(c, agent)
    if not config.enabled or agent["desired_state"] != "running":
        return {}
    from .admin_settings import effective
    from .community_discovery import continuation_phase

    tools, _ = effective(c, "models", str(agent["id"]))
    if not tools["tools"]["community_research"] or not tools["tools"]["public_web"]:
        return {}
    latest = one(
        c,
        "SELECT state,input,result,coalesce(completed_at,created_at) AS checked_at FROM lead.tasks WHERE type='community.discover' AND coalesce(input->>'research_request','')=%s ORDER BY created_at DESC LIMIT 1",
        (agent["onboarding_state"].get("community_research_request", ""),),
    )
    discovery = (
        latest["checked_at"] + timedelta(hours=search_interval(c, agent, config))
        if latest and latest["state"] == "succeeded"
        else latest["checked_at"] + timedelta(minutes=10)
        if latest and latest["state"] == "failed"
        else latest["checked_at"]
        if latest
        else now
    )
    if latest and latest["state"] == "succeeded" and continuation_phase(latest, agent, config):
        discovery = latest["checked_at"]
    review = None
    if config.mode == "contextual_outreach":
        row = next_membership_review(c, focus=first_users(agent), exit_restricted=config.allow_leave_restricted, proactive=config.allow_proactive_post)
        review = row["due"] if row else None
    retry = one(
        c,
        "SELECT min((result->>'retry_after')::timestamptz) AS due FROM lead.tasks WHERE type='community.engage' AND state='failed' AND result ? 'retry_after' AND result->>'reply_state' IS NULL AND (result->>'join_submitted' IS NULL OR result->>'join_state'='confirmed')",
    )["due"]
    from .growth_experiments import chamber_campaign
    pending_join = next_pending_inventory(c) if chamber_campaign(agent) else None if first_users(agent) else next_join_check(c)
    return {
        "next_discovery_at": discovery,
        "next_review_at": review,
        "next_retry_at": retry,
        "next_join_approval_check_at": pending_join["due"] if pending_join else None,
    }


def started(aid):
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET heartbeat_at=now() WHERE id=%s AND desired_state='running'",
            (aid,),
        )


def cpu_sample():
    """Cumulative host counters. Linux guest ticks are already included in user/nice."""
    try:
        times = psutil.cpu_times()
    except (OSError, NotImplementedError):
        return None
    if not hasattr(times, "steal"):
        return None
    total = sum(times) - times.guest - times.guest_nice
    return total, times.steal, time.monotonic()


def pressure_delay(before, after):
    if before is None or after is None or after[0] <= before[0]:
        return 0
    steal = (after[1] - before[1]) / (after[0] - before[0])
    # Recover in proportion to the wall time lost this cycle. Short reads
    # should reuse a warm browser even when they briefly encounter contention.
    lost_seconds = max(0, after[2] - before[2]) * min(1, steal)
    if steal >= 0.50:
        return min(480, max(15, math.ceil(2 * lost_seconds)))
    if steal >= 0.20:
        return min(120, max(5, math.ceil(lost_seconds)))
    return 0


def completed(aid, result, now=None, *, respect_delay=False, resource_delay=0):
    now = now or datetime.now(timezone.utc)
    with tx(aid) as c:
        agent = require_agent(c, aid, True)
        due = deadlines(c, agent, now)
        delay = result["delay"]
        from .content_backlog import next_due

        content_due = next_due(c, agent, now)
        if content_due and not respect_delay:
            delay = min(delay, max(5, math.ceil((content_due - now).total_seconds())))
        if due:
            # Community steps retain their retry/reconciliation backoff.
            if not respect_delay and not result.get("reason", "").startswith(
                "community_"
            ):
                pending = [time for time in due.values() if time]
                delay = min(
                    delay, max(5, math.ceil((min(pending) - now).total_seconds()))
                )
            delay = min(delay, HEARTBEAT_SECONDS)
        delay = max(delay, resource_delay)
        if due:
            schedule = {
                "checked_at": now.isoformat(),
                "next_check_at": (now + timedelta(seconds=delay)).isoformat(),
                **{
                    key: value.isoformat() if value else None
                    for key, value in due.items()
                },
                "discovery_interval_hours": discovery_config(
                    c, agent
                ).discovery_interval_hours,
                "review_interval_seconds": REVIEW_SECONDS,
                **({"deferred_reason": result.get("reason")} if respect_delay else {}),
            }
            prior = agent["onboarding_state"].get("community_schedule", {})
            if any(
                prior.get(key) != schedule[key]
                for key in ("next_discovery_at", "next_review_at")
            ):
                record(
                    c,
                    aid,
                    "已安排持续社群运营",
                    "每日找群与群内回访按各自到期时间执行，下一次检查已交由产品后台定时触发。",
                )
            c.execute(
                "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                (j({"community_schedule": schedule}), aid),
            )
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (
                j(
                    {
                        "operating_schedule": {
                            "checked_at": now.isoformat(),
                            "next_check_at": (
                                now + timedelta(seconds=delay)
                            ).isoformat(),
                            "host_backoff_seconds": resource_delay,
                            "next_content_at": content_due.isoformat()
                            if content_due
                            else None,
                            "reason": result.get("reason"),
                        }
                    }
                ),
                aid,
            ),
        )
        c.execute(
            """UPDATE lead.agents SET heartbeat_at=now(),health_state=CASE
              WHEN desired_state<>'running' THEN health_state
              WHEN health_state IN ('queued','working','ready') THEN CASE
                WHEN EXISTS(SELECT 1 FROM lead.tasks WHERE state IN ('queued','running','reconciling')) THEN 'working'
                ELSE 'ready' END ELSE health_state END WHERE id=%s""",
            (aid,),
        )
    return {
        **result,
        "cadence_version": 1,
        "delay": delay,
        **({"community_next_check_at": schedule["next_check_at"]} if due else {}),
    }
