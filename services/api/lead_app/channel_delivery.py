"""Independent delivery clocks for enabled channels; platform grants remain authoritative."""

import asyncio
from datetime import datetime, timedelta, timezone

from temporalio import activity

from . import growth_experiments
from .admin_operations import managed_execution
from .atomic_dispatch import channel_pilot
from .cadence_events import wait_result
from .db import emit, j, one, tx
from .operating_playbooks import channel_for
from .scope import current_tenant, scoped_activity


def owns(agent, platform, *, account_id=None, connection=None):
    from .channel_authority import driver_accounts, has_driver
    managed = has_driver(agent, platform)
    if managed and account_id is not None:
        return bool(connection is not None and any(str(row["id"]) == str(account_id) for row in driver_accounts(connection, str(agent["id"]), platform)))
    member = platform == "reddit" and agent["onboarding_state"].get("reddit_member_delivery") is True
    # Bounded experiments keep their own budget admission. Chamber's existing
    # campaign has no experiment pool; explicitly delegated Reddit work uses
    # these clocks, and the shared kernel skips the same channel via owns().
    return bool(
        (channel_pilot(agent["id"]) or member or managed)
        and channel_for(platform)
        and (
            not growth_experiments.enabled(agent)
            or managed
            or (member and growth_experiments.chamber_campaign(agent))
        )
    )


def enroll(c, aid, agent, platform):
    if not owns(agent, platform):
        return
    for lane in ("delivery", "research"):
        enroll_lane(c, aid, agent, platform, lane)


def enroll_lane(c, aid, agent, platform, lane):
    control = agent["control_version"]
    key = (
        f"channel-clock:{aid}:{platform}:{control}"
        if lane == "delivery"
        else f"channel-research:{aid}:{platform}:{control}"
    )
    row = one(
        c,
        """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,scheduled_at,result)
        VALUES(%s,%s,'channel.clock','planned',%s,%s,now(),'{}')
        ON CONFLICT(workflow_id) DO UPDATE SET state='planned',version=lead.tasks.version+1,
            result='{}',completed_at=NULL,scheduled_at=now()
        WHERE lead.tasks.state='canceled' RETURNING id,version""",
        (
            current_tenant(),
            aid,
            key,
            j({"platform": platform, "control_version": control, "lane": lane}),
        ),
    )
    if row:
        emit(
            c,
            aid,
            "channel.clock",
            {"task_id": str(row["id"]), "generation": row["version"], "lane": lane},
        )


def current(c, aid, args):
    from .channel_authority import driver_accounts
    c.execute("SET LOCAL lock_timeout='500ms'")
    agent = one(c, "SELECT * FROM lead.agents WHERE id=%s FOR SHARE", (aid,))
    clock = one(
        c,
        """SELECT * FROM lead.tasks WHERE id=%s AND type='channel.clock'
        AND state='planned' AND version=%s FOR UPDATE""",
        (args["task_id"], args["generation"]),
    )
    if not agent or not clock:
        return None
    platform = clock["input"]["platform"]
    if (
        agent["desired_state"] != "running"
        or agent["control_version"] != clock["input"]["control_version"]
        or not owns(agent, platform)
        or not driver_accounts(c, aid, platform)
    ):
        c.execute(
            "UPDATE lead.tasks SET state='canceled',completed_at=now() WHERE id=%s",
            (clock["id"],),
        )
        return None
    return clock


def opportunity(c, target_id):
    """Only attach observed, validated evidence for the exact authorized destination."""
    from .operating_feedback import fingerprint

    row = one(
        c,
        """WITH latest AS (
            SELECT task.id,task.result,target.canonical_url
            FROM lead.tasks task JOIN lead.account_targets target ON target.id=%s
            WHERE task.type='operating.validate' AND task.state='succeeded'
              AND task.completed_at>now()-interval '14 days' AND task.result->>'status'='observed'
              AND task.input->>'business_profile_id'=(SELECT id::text FROM lead.business_profiles ORDER BY revision DESC LIMIT 1)
              AND task.input->>'config_revision'=(SELECT config_revision::text FROM lead.agents WHERE id=task.agent_id)
              AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(task.result->'source_urls') u
                          WHERE rtrim(u,'/')=rtrim(target.canonical_url,'/'))
            ORDER BY task.completed_at DESC,task.id DESC LIMIT 1
        ) SELECT id,result->'cold_entry' AS finding FROM latest
          WHERE result->>'channel_fit'='viable' AND rtrim(result#>>'{cold_entry,source_url}','/')=rtrim(canonical_url,'/')""",
        (target_id,),
    )
    if row:
        finding = row["finding"]
        key = fingerprint([finding["source_url"].rstrip("/"), finding["observation"]])
        consumed = one(
            c,
            """SELECT 1 FROM lead.tasks WHERE (type LIKE '%%.publish' OR input->>'member_contribution'='true')
            AND input->>'target_id'=%s AND input#>>'{operating_plan,channel_opportunity,source_key}'=%s
            AND state<>'canceled' LIMIT 1""",
            (str(target_id), key),
        )
        if not consumed:
            return {
                "evidence_task_id": str(row["id"]),
                "source_key": key,
                "source_url": finding["source_url"],
                "observation": finding["observation"],
                "sharing_rule": finding["sharing_rule"],
            }
    return None


def next_target(args):
    from .execution_cadence import Decision, Scope, WaitEvidence, decide
    from .operating_kernel import blocked, eligible_publish_targets

    aid = args["agent_id"]
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        clock = current(c, aid, args)
        if not clock:
            return {"stop": True}
        state = clock["result"] or {}
        if state.get("last_tick") == args["tick"]:
            return {"delay": max(1, (clock["scheduled_at"] - now).total_seconds()),
                    **({"execution_cadence": state["execution_cadence"]} if state.get("execution_cadence") else {})}
        if state.get("pending_tick") == args["tick"]:
            return {
                "target_id": state["pending_target"],
                "opportunity": state.get("opportunity"),
            }
        state.pop("execution_cadence", None)
        from .channel_authority import driver_accounts
        accounts = {str(row["id"]): row for row in driver_accounts(c, aid, clock["input"]["platform"])}
        allowed_accounts = set(accounts)
        targets = [target for target in eligible_publish_targets(c, blocked(c)) if str(target["account_id"]) in allowed_accounts]
        deadlines = state.get("target_deadlines", {})
        version = max(1, one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (aid,))["onboarding_state"].get("execution_cadence", {}).get("policy_version", 0))
        waiting = {}
        delay = 300
        reason = "no_available_verified_target"
        for target in sorted(targets, key=lambda row: str(row["target_id"])):
            if target["platform"] != clock["input"]["platform"]:
                continue
            target_id = str(target["target_id"])
            scope = Scope(agent_id=str(aid), account_id=str(target["account_id"]),
                binding_version=accounts[str(target["account_id"])]["binding_version"], target_id=target_id, operation="publish")
            constraints = []
            def constraint(reason, **fields):
                return WaitEvidence(scope=scope, source="capability", reason_code=reason,
                    evidence_ref="task:"+str(clock["id"]), observed_at=now, policy_version=version, **fields)
            deadline = deadlines.get(target_id)
            if deadline:
                try:
                    deadline = datetime.fromisoformat(deadline)
                    if deadline.tzinfo is None or deadline.utcoffset() is None:
                        raise ValueError("timezone required")
                except (ValueError, TypeError):
                    constraints.append(constraint("target_schedule_unverified", resume_event="configuration_changed"))
                else:
                    if deadline > now:
                        constraints.append(constraint("legacy_unverified_target_window", not_before=deadline))
            evidence = opportunity(c, target_id)
            if clock["input"]["platform"] in ("reddit", "facebook") and not evidence:
                constraints.append(constraint("awaiting_validated_opportunity", resume_event="candidate_validated"))
            decision = decide(constraints, now, subject=scope)
            if decision.state != "ready":
                waiting[target_id] = decision
                continue
            state["waiting_targets"] = {key: value.model_dump(mode="json") for key, value in waiting.items()}
            state.update(
                pending_tick=args["tick"],
                pending_target=target_id,
                opportunity=evidence,
            )
            c.execute(
                "UPDATE lead.tasks SET result=%s WHERE id=%s", (j(state), clock["id"])
            )
            return {"target_id": target_id, "opportunity": evidence}
        state["waiting_targets"] = {key: value.model_dump(mode="json") for key, value in waiting.items()}
        timed = [item for item in waiting.values() if item.state == "waiting_time"]
        decision = None
        if timed:
            decision = min(timed, key=lambda item: item.next_at)
            delay = max(1, (decision.next_at-now).total_seconds())
            reason = "awaiting_target_window"
        elif waiting:
            constraints = tuple(item for value in waiting.values() for item in value.constraints)
            decision = Decision(state="waiting_event", constraints=constraints,
                resume_events=tuple(sorted({event for value in waiting.values() for event in value.resume_events})))
            reason = constraints[0].reason_code
        c.execute(
            """UPDATE lead.tasks SET scheduled_at=now()+make_interval(secs=>%s),
            result=%s WHERE id=%s""",
            (
                delay,
                j({**state, "reason": reason, "last_tick": args["tick"]}),
                clock["id"],
            ),
        )
        if decision and decision.next_at:
            c.execute("UPDATE lead.tasks SET scheduled_at=%s WHERE id=%s", (decision.next_at, clock["id"]))
        return wait_result(c, aid, clock, {"delay": delay, "reason": reason}, decision=decision)


def finish(args, target_id, result):
    now = datetime.now(timezone.utc)
    delay = max(1, int(result.get("delay", 300)))
    with tx(args["agent_id"]) as c:
        clock = current(c, args["agent_id"], args)
        if not clock:
            return {"stop": True}
        state = clock["result"] or {}
        if state.get("pending_tick") != args["tick"]:
            return {"delay": 1}
        state.pop("pending_tick")
        state.pop("pending_target")
        deadlines = state.setdefault("target_deadlines", {})
        deadlines[target_id] = (now + timedelta(seconds=delay)).isoformat()
        state.update(
            last_tick=args["tick"],
            reason=result.get("reason", "plan_recorded"),
            last_target=target_id,
        )
        c.execute(
            "UPDATE lead.tasks SET result=%s,scheduled_at=now()+interval '1 second' WHERE id=%s",
            (j(state), clock["id"]),
        )
    # Give another immediately eligible target a turn before sleeping on this one.
    return {"delay": 1}


@activity.defn
@scoped_activity
@managed_execution("channel_delivery")
async def channel_delivery_tick(args: dict):
    from .activities import _publication_cycle

    selection = await asyncio.to_thread(next_target, args)
    if "target_id" not in selection:
        return selection
    target_id = selection["target_id"]
    result = await _publication_cycle(
        {
            "agent_id": args["agent_id"],
            "cycle_id": args["tick"],
            "target_id": target_id,
            "operating_plan": {"channel_opportunity": selection.get("opportunity")},
            "channel_clock": {
                "task_id": args["task_id"],
                "generation": args["generation"],
            },
        }
    )
    return await asyncio.to_thread(finish, args, target_id, result)


@activity.defn
@scoped_activity
async def park_channel_target(args: dict):
    """A poison target may yield to its siblings; never replace an uncertain send."""

    def park():
        with tx(args["agent_id"]) as c:
            clock = current(c, args["agent_id"], args)
            if not clock:
                return {"stop": True}
            state = clock["result"] or {}
            target = state.get("pending_target")
            if not target or state.get("pending_tick") != args["tick"]:
                return {"delay": 300}
        return finish(
            args, target, {"delay": 3600, "reason": "execution_attempts_exhausted"}
        )

    return await asyncio.to_thread(park)
