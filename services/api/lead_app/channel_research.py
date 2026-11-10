"""Bounded source discovery and validation, separate from a channel's delivery clock."""

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import activity

from .channel_delivery import current
from .config import env
from .db import all_rows, j, one, tx
from .scope import scoped_activity


def source_access(platform):
    return platform in {
        value.strip()
        for value in env().get("LEAD_APPROVED_DISCOVERY_CHANNELS", "").split(",")
    }


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
            }, None
        if not source_access(platform):
            return {"delay": 1800, "reason": "source_permission_required"}, None
        if sample()["defer_optional"]:
            return {"delay": 120, "reason": "host_pressure"}, None
        agent = one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
        business = one(
            c, "SELECT * FROM lead.business_profiles ORDER BY revision DESC LIMIT 1"
        )
        if not business or not business["description"].strip():
            return {"delay": 300, "reason": "business_required"}, None
        ctx = context(c, aid, business)
        if not ctx["models"]["tools"]["public_web"]:
            return {"delay": 900, "reason": "public_web_disabled"}, None
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
        if not reconcile(c, aid, agent, business, None, platform):
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
async def channel_research_tick(args: dict):
    from .operating_kernel import run_work

    result, candidate = await asyncio.to_thread(select, args)
    if candidate:
        result = await run_work(args["agent_id"], candidate, channel_clock=args)
    if not result.get("stop") and not result.get("recorded"):
        delay = max(1, int(result.get("delay", 300)))
        with tx(args["agent_id"]) as c:
            clock = current(c, args["agent_id"], args)
            if not clock:
                return {"stop": True}
            c.execute(
                """UPDATE lead.tasks SET scheduled_at=now()+%s,result=(coalesce(result,'{}')-'pending'-'pending_tick')||%s WHERE id=%s""",
                (
                    timedelta(seconds=delay),
                    j(
                        {
                            "reason": result.get("reason"),
                            "last_tick": args["tick"],
                            "last_work": candidate.task_id if candidate else None,
                        }
                    ),
                    clock["id"],
                ),
            )
    return result
