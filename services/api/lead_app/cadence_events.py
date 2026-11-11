"""Condition events wake existing clocks; they never grant external access."""
from datetime import datetime, timezone

from .db import all_rows, emit, j, one, tx
from .execution_cadence import Scope, WaitEvidence, decide

EVENT_REASONS = {
    "source_permission_required": "source_config_changed",
    "public_web_disabled": "configuration_changed",
    "business_required": "configuration_changed",
    "no_available_verified_target": "target_verified",
    "awaiting_validated_opportunity": "candidate_validated",
}


def wake_clocks(c, aid, *, platforms=None, lane=None):
    agent = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (aid,))
    if not agent:
        return
    version = max(1, agent["onboarding_state"].get("execution_cadence", {}).get("policy_version", 0))
    for clock in all_rows(c, "SELECT id,version,input FROM lead.tasks WHERE agent_id=%s AND type='channel.clock' AND state='planned'", (aid,)):
        if platforms is not None and clock["input"]["platform"] not in platforms:
            continue
        if lane and clock["input"].get("lane", "delivery") != lane:
            continue
        emit(c, aid, "channel.clock.wake", {"task_id": str(clock["id"]), "generation": clock["version"], "policy_version": version})


def wait_result(c, aid, clock, result, *, decision=None):
    """Only known condition gaps park indefinitely; engineering retries keep timers."""
    reasons = list(dict.fromkeys([result.get("reason"), *result.get("blocking_reasons", [])]))
    reasons = [reason for reason in reasons if reason in EVENT_REASONS]
    if decision is None and not reasons:
        return result
    agent = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (aid,))
    version = max(1, agent["onboarding_state"].get("execution_cadence", {}).get("policy_version", 0))
    scope = Scope(agent_id=str(aid), target_id="platform:"+clock["input"]["platform"], operation=clock["input"].get("lane", "delivery"))
    now = datetime.now(timezone.utc)
    if decision is None:
        evidence = [WaitEvidence(scope=scope, source="capability", reason_code=reason,
            evidence_ref="task:"+str(clock["id"]), observed_at=now, resume_event=EVENT_REASONS[reason], policy_version=version)
            for reason in reasons]
        decision = decide(evidence, now, subject=scope)
    result = {**result, "execution_cadence": decision.model_dump(mode="json")}
    c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE id=%s AND agent_id=%s",
              (j({"execution_cadence": result["execution_cadence"]}), clock["id"], aid))
    return result


def reconcile_source_config():
    """Durable nonsecret fingerprint; a restarted publisher does not spam signals."""
    from .config import env
    configured = sorted({p.strip() for p in env().get("LEAD_APPROVED_DISCOVERY_CHANNELS", "").split(",") if p.strip()})
    with tx() as c:
        ids = all_rows(c, """SELECT DISTINCT t.agent_id FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id
            WHERE t.type='channel.clock' AND t.state='planned'
              AND coalesce(a.onboarding_state->'channel_source_config','[]') IS DISTINCT FROM %s""", (j(configured),))
    for row in ids:
        aid = str(row["agent_id"])
        with tx(aid) as c:
            agent = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s FOR UPDATE", (aid,))
            prior = agent["onboarding_state"].get("channel_source_config", [])
            if prior == configured:
                continue
            c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s", (j({"channel_source_config": configured}), aid))
            wake_clocks(c, aid, platforms=set(prior)^set(configured))


def probe_rule_gap(task, *, now, source_allowed, read_after, reader):
    if not source_allowed:
        scope = Scope(agent_id=str(task.get("agent_id") or "unresolved"), operation="read_rules")
        evidence = WaitEvidence(scope=scope, source="capability", reason_code="source_permission_required",
            evidence_ref="task:"+str(task["id"]), observed_at=now, resume_event="source_config_changed", policy_version=1)
        return {"execution_cadence": decide([evidence], now, subject=scope).model_dump(mode="json")}
    if read_after is None or read_after > now:
        return None
    return reader(task)
