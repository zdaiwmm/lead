"""Community deadlines belong to the product, independently of channel cadence."""

import math
from datetime import datetime, timedelta, timezone

from .community_growth import GrowthConfig
from .db import j, one, record, require_agent, tx

HEARTBEAT_SECONDS = 300
REVIEW_SECONDS = 3600


def deadlines(c, agent, now=None):
    now = now or datetime.now(timezone.utc)
    config = GrowthConfig.model_validate(
        agent["onboarding_state"].get("community_growth", {})
    )
    if not config.enabled or agent["desired_state"] != "running":
        return {}
    from .admin_settings import effective

    tools, _ = effective(c, "models", str(agent["id"]))
    if not tools["tools"]["community_research"] or not tools["tools"]["public_web"]:
        return {}
    latest = one(
        c,
        "SELECT state,coalesce(completed_at,created_at) AS checked_at FROM lead.tasks WHERE type='community.discover' AND coalesce(input->>'research_request','')=%s ORDER BY created_at DESC LIMIT 1",
        (agent["onboarding_state"].get("community_research_request", ""),),
    )
    discovery = (
        latest["checked_at"] + timedelta(hours=config.discovery_interval_hours)
        if latest and latest["state"] == "succeeded"
        else latest["checked_at"] + timedelta(minutes=10)
        if latest and latest["state"] == "failed"
        else latest["checked_at"]
        if latest
        else now
    )
    review = None
    if config.mode == "contextual_outreach":
        row = one(
            c,
            """WITH latest AS (
              SELECT DISTINCT ON (input->'candidate'->>'telegram_url') *
              FROM lead.tasks WHERE type='community.engage'
              ORDER BY input->'candidate'->>'telegram_url',created_at DESC
            ) SELECT min(coalesce(recent.completed_at,recent.created_at)+interval '1 hour') AS due
            FROM latest recent WHERE recent.state='succeeded'
            AND recent.result->'history_observation'->>'joined'='true'
            AND coalesce(recent.result->'decision'->>'automation_forbidden','false')<>'true'
            AND EXISTS(SELECT 1 FROM lead.tasks membership WHERE membership.type='community.engage'
              AND membership.input->'candidate'->>'telegram_url'=recent.input->'candidate'->>'telegram_url'
              AND membership.input->>'external_account_id'=recent.input->>'external_account_id'
              AND membership.result->>'join_submitted'='true' AND membership.result->>'join_state'='confirmed')""",
        )
        review = row["due"]
    retry = one(
        c,
        "SELECT min((result->>'retry_after')::timestamptz) AS due FROM lead.tasks WHERE type='community.engage' AND state='failed' AND result ? 'retry_after' AND result->>'reply_state' IS NULL AND (result->>'join_submitted' IS NULL OR result->>'join_state'='confirmed')",
    )["due"]
    pending_join = one(
        c,
        "SELECT min((result->>'join_checked_at')::timestamptz+interval '15 minutes') AS due FROM lead.tasks WHERE type='community.engage' AND state IN ('succeeded','failed') AND (result->>'join_state'='pending_approval' OR (result->>'join_mode'='approval_request' AND result->>'join_state' IN ('dispatching','unknown')))",
    )["due"]
    return {
        "next_discovery_at": discovery,
        "next_review_at": review,
        "next_retry_at": retry,
        "next_join_approval_check_at": pending_join,
    }


def started(aid):
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET heartbeat_at=now() WHERE id=%s AND desired_state='running'",
            (aid,),
        )


def completed(aid, result, now=None, *, respect_delay=False):
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
            schedule = {
                "checked_at": now.isoformat(),
                "next_check_at": (now + timedelta(seconds=delay)).isoformat(),
                **{
                    key: value.isoformat() if value else None
                    for key, value in due.items()
                },
                "discovery_interval_hours": GrowthConfig.model_validate(
                    agent["onboarding_state"].get("community_growth", {})
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
        "delay": delay,
        **({"community_next_check_at": schedule["next_check_at"]} if due else {}),
    }
