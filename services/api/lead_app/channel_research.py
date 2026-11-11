"""Bounded source discovery and validation, separate from a channel's delivery clock."""

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import activity

from .admin_operations import managed_execution
from .cadence_events import wait_result, wake_clocks
from .channel_delivery import current
from .config import env
from .db import all_rows, j, one, tx
from .scope import scoped_activity


def source_access(platform):
    return platform in {
        value.strip()
        for value in env().get("LEAD_APPROVED_DISCOVERY_CHANNELS", "").split(",")
    }


def unbound_source(c, platform, agent, business):
    if platform not in ("reddit", "facebook"):
        return None
    from .channel_authority import driver_accounts
    allowed_ids = [str(row["id"]) for row in driver_accounts(c,str(agent["id"]),platform)]
    if not allowed_ids:
        return None
    return one(
        c,
        """SELECT t.id AS evidence_id,s.id AS account_id,s.binding_version,
        t.input->>'candidate_url' AS url
        FROM lead.tasks t JOIN lead.social_accounts s ON s.platform=t.input->>'platform'
        WHERE t.type='operating.validate' AND t.state='succeeded' AND t.result->>'status'='observed'
          AND t.result->>'channel_fit'='viable' AND t.result#>>'{cold_entry,source_url}'=t.input->>'candidate_url' AND t.result->'source_urls' ? (t.input->>'candidate_url')
          AND t.completed_at>now()-interval '14 days' AND t.input->>'business_profile_id'=%s
          AND t.input->>'config_revision'=%s AND s.platform=%s AND s.id::text=ANY(%s) AND s.state='connected' AND s.automation_enabled
          AND (NOT coalesce(t.result->'account_inspections','{}') ? (s.id::text||':'||s.binding_version::text)
               OR (t.result#>>ARRAY['account_inspections',s.id::text||':'||s.binding_version::text,'checked_at'])::timestamptz<now()-interval '1 day')
          AND NOT EXISTS (SELECT FROM lead.account_targets target WHERE target.account_id=s.id
                           AND rtrim(target.canonical_url,'/')=rtrim(t.input->>'candidate_url','/')
                           AND (target.verified_permissions->>'access_mode' IS DISTINCT FROM 'ordinary_member'
                                OR (target.verified_permissions#>>'{rules,checked_at}')::timestamptz>now()-interval '23 hours'))
          AND EXISTS (SELECT FROM lead.platform_capabilities p WHERE p.platform=s.platform AND p.action_type='publish'
                      AND p.permission_state='approved')
        ORDER BY t.completed_at DESC,s.created_at,s.id LIMIT 1""",
        (str(business["id"]), str(agent["config_revision"]), platform, allowed_ids),
    )


async def inspect_source(args, candidate):
    import httpx

    from .activities import browser_rpc
    from .channels import store_target

    aid = args["agent_id"]
    try:
        target = await browser_rpc(
            f"/agents/{aid}/target/inspect",
            {"account_id": str(candidate["account_id"]), "url": candidate["url"]},
        )
    except httpx.HTTPStatusError as error:
        if (
            error.response.status_code != 409
            or error.response.json().get("code") == "browser_busy"
        ):
            raise
        # Unavailable access is not evidence that this account manages the target.
        reason = "target_management_unverified"
    else:
        if target["canonical_url"].rstrip("/") != candidate["url"].rstrip("/"):
            reason = "target_management_unverified"
        else:
            suitable = True
            if target.get("access_mode") == "ordinary_member":
                from .reddit_member import prepare_first

                draft = await prepare_first(aid, candidate, target)
                suitable = draft["suitable"]
                target["first_publication"] = draft
                target["evidence_task_id"] = str(candidate["evidence_id"])
            if suitable:
                binding = await asyncio.to_thread(
                    store_target, aid, str(candidate["account_id"]), target,
                    expected_binding=candidate["binding_version"], channel_clock=args,
                )
                reason = "target_permissions_review" if binding["card_id"] else "target_verification_queued"
            else:
                reason = "community_rules_do_not_fit"
    with tx(aid) as c:
        if not current(c, aid, args):
            return {"stop": True}
        key = f"{candidate['account_id']}:{candidate['binding_version']}"
        c.execute(
            """UPDATE lead.tasks SET result=jsonb_set(result,'{account_inspections}',
                  coalesce(result->'account_inspections','{}') || %s) WHERE id=%s""",
            (j({key: {"reason": reason, "checked_at": datetime.now(timezone.utc).isoformat()}}), candidate["evidence_id"]),
        )
    return {"delay": 1, "reason": reason}


def select(args):
    from .operating_admission import assess, context
    from .operating_feedback import enqueue
    from .operating_followups import reconcile
    from .operating_kernel import Candidate
    from .resource_pressure import sample

    aid = args["agent_id"]
    with tx(aid) as c:
        clock = current(c, aid, args)
        if not clock:
            return {"stop": True}, None
        platform = clock["input"]["platform"]
        state = clock["result"] or {}
        if state.get("last_tick") == args["tick"]:
            return {
                "delay": max(
                    1,
                    (
                        clock["scheduled_at"] - datetime.now(timezone.utc)
                    ).total_seconds(),
                ),
                "reason": state.get("reason"),
                "recorded": True,
                **({"execution_cadence": state["execution_cadence"]} if state.get("execution_cadence") else {}),
            }, None
        agent = one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
        business = one(c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1")
        if business and business["description"].strip():
            from .atomic_dispatch import enqueue_connected
            enqueue_connected(c,aid,agent,business)
        if not source_access(platform):
            return wait_result(c, aid, clock, {"delay": 1800, "reason": "source_permission_required"}), None
        if sample()["defer_optional"]:
            return {"delay": 120, "reason": "host_pressure"}, None
        if not business or not business["description"].strip():
            return wait_result(c, aid, clock, {"delay": 300, "reason": "business_required"}), None
        ctx = context(c, aid, business)
        if not ctx["models"]["tools"]["public_web"]:
            return wait_result(c, aid, clock, {"delay": 900, "reason": "public_web_disabled"}), None
        pending = (
            state.get("pending") if state.get("pending_tick") == args["tick"] else None
        )
        if pending:
            work = one(
                c,
                "SELECT state,result FROM lead.tasks WHERE id=%s",
                (pending["task_id"],),
            )
            if (
                not work
                or work["state"] in ("succeeded", "canceled", "failed")
                or (
                    pending["read_source"]
                    and (work["result"] or {}).get("observation_checkpoint")
                )
            ):
                return {"delay": 1, "reason": "research_step_recorded"}, None
            return {}, Candidate(**pending)
        unbound = unbound_source(c, platform, agent, business)
        if unbound:
            return {"inspect": unbound}, None
        if not reconcile(c, aid, agent, business, None, platform, revisit=True):
            enqueue(
                c,
                aid,
                agent,
                business,
                "opportunities",
                "寻找与产品场景相符的公开需求及参与规则。",
                platform=platform,
                work_key="public-discovery",
            )
        rows = all_rows(
            c,
            """SELECT * FROM lead.tasks WHERE type IN ('operating.validate','operating.opportunities')
            AND input->>'platform'=%s AND input->>'business_profile_id'=%s AND input->>'config_revision'=%s
            AND input->>'control_version'=%s AND NOT (input ? 'experiment_id')
            AND (state IN ('planned','failed') OR (state='running' AND started_at<now()-interval '5 minutes'))
            AND coalesce((result->>'attempts')::int,0)<2 AND scheduled_at<=now()
            ORDER BY (type='operating.validate') DESC,scheduled_at,id LIMIT 32""",
            (
                platform,
                str(business["id"]),
                str(agent["config_revision"]),
                str(agent["control_version"]),
            ),
        )
        reason = "awaiting_new_evidence"
        for row in rows:
            read = row["type"] == "operating.validate" and not (
                row["result"] or {}
            ).get("observation_checkpoint")
            rating = assess(
                ctx,
                row["type"],
                76,
                row,
                paid_step=not read,
                request_units=0 if read else None,
            )
            if rating["allowed"]:
                candidate = Candidate(
                    str(row["id"]),
                    row["type"],
                    rating["priority"],
                    "渠道独立研究",
                    task_id=str(row["id"]),
                    estimated_token_units=rating["estimated_token_units"],
                    read_source=read,
                )
                c.execute(
                    "UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s",
                    (
                        j({"pending": vars(candidate), "pending_tick": args["tick"]}),
                        clock["id"],
                    ),
                )
                return {}, candidate
            reason = rating["reason"]
        return {"delay": 300, "reason": reason}, None


@activity.defn
@scoped_activity
@managed_execution("channel_research")
async def channel_research_tick(args: dict):
    from .operating_kernel import run_work

    result, candidate = await asyncio.to_thread(select, args)
    if result.get("inspect"):
        result = await inspect_source(args, result["inspect"])
    elif candidate:
        result = await run_work(args["agent_id"], candidate, channel_clock=args)
    if not result.get("stop") and not result.get("recorded"):
        delay = max(1, int(result.get("delay", 300)))
        with tx(args["agent_id"]) as c:
            clock = current(c, args["agent_id"], args)
            if not clock:
                return {"stop": True}
            c.execute(
                """UPDATE lead.tasks SET scheduled_at=now()+%s,result=(coalesce(result,'{}')-'pending'-'pending_tick'-'execution_cadence')||%s WHERE id=%s""",
                (
                    timedelta(seconds=delay),
                    j(
                        {
                            **({"execution_cadence": result["execution_cadence"]} if result.get("execution_cadence") else {}),
                            "reason": result.get("reason"),
                            "last_tick": args["tick"],
                            "last_work": candidate.task_id if candidate else None,
                        }
                    ),
                    clock["id"],
                ),
            )
            if candidate or result.get("reason") in {"target_verification_queued", "target_permissions_review"}:
                wake_clocks(c, args["agent_id"], platforms={clock["input"]["platform"]}, lane="delivery")
    return result
