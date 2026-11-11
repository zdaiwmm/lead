"""Provenance-gated cadence activation and immutable read successors."""

import hashlib
import json

from .db import all_rows, audit, emit, j, one, require_agent
from .execution_cadence import Decision
from .scope import current_tenant, trusted_execution

PARAMETERS = ("combined_per_24h", "joins_per_24h", "replies_per_24h", "reply_interval_minutes")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _bindings(c, aid):
    rows = all_rows(c, """SELECT id,binding_version,external_account_id FROM lead.social_accounts
        WHERE agent_id=%s AND platform='telegram' AND state='connected' AND automation_enabled ORDER BY id""", (aid,))
    return {str(row["id"]): {key: row[key] for key in ("binding_version", "external_account_id")} for row in rows}


def preview_migration(c, aid: str, *, expected_control: int) -> dict:
    from .community_participation import ParticipationPolicy

    agent = require_agent(c, aid)
    if agent["control_version"] != expected_control:
        raise ValueError("项目状态已变化，请重新核对节奏预览")
    growth = agent["onboarding_state"].get("community_growth", {})
    values = ParticipationPolicy.model_validate(growth.get("participation_policy", {})).model_dump()
    history = all_rows(c, """SELECT id,type,payload FROM lead.audit_events WHERE agent_id=%s
        AND type IN ('cadence.parameter_origins_attested','community.strategy_updated')
        ORDER BY occurred_at DESC,id DESC LIMIT 100""", (aid,))
    items = []
    for parameter in PARAMETERS:
        origin, ref = "unresolved", None
        for event in history:
            body = event["payload"]
            if event["type"] == "community.strategy_updated":
                if parameter in body.get("_explicit_policy_fields", []):
                    origin, ref = "owner_explicit", str(event["id"])
                break
            witness = body.get("origins", {}).get(parameter, {})
            if (body.get("config_revision") == agent["config_revision"] and body.get("config_snapshot") == growth
                    and witness.get("value") == values[parameter] and witness.get("evidence_ref")
                    and witness.get("origin") in {"verified_system_default", "owner_explicit", "platform_observed", "engineering"}):
                origin, ref = witness["origin"], str(event["id"])
                break
        items.append({"parameter": parameter, "value": values[parameter], "origin": origin,
                      "evidence_ref": ref, "disposition": "replace_on_activation" if origin == "verified_system_default" else "preserve"})
    bindings = _bindings(c, aid)
    revision = _digest({"control": expected_control, "config": agent["config_revision"], "growth": growth,
                        "authorization": agent["authorization_version"], "bindings": bindings})
    result = {"source_revision": revision, "items": items, "account_bindings": bindings,
              "config_revision": agent["config_revision"], "control_version": expected_control,
              "strategy_digest": _digest(growth)}
    return {**result, "preview_digest": _digest(result)}


def activate(c, aid: str, preview_digest: str, expected_control: int) -> dict:
    if not trusted_execution():
        raise PermissionError("节奏激活仅供已授权维护入口使用")
    agent = require_agent(c, aid, True)
    preview = preview_migration(c, aid, expected_control=expected_control)
    if preview["preview_digest"] != preview_digest:
        raise ValueError("节奏依据或账号绑定已变化")
    prior = agent["onboarding_state"].get("execution_cadence", {})
    if prior.get("mode") == "active" and prior.get("preview_digest") == preview_digest:
        return prior
    version = int(prior.get("policy_version", 0)) + 1
    device = agent["onboarding_state"].get("telegram_executor", {})
    if (not preview["account_bindings"] or device.get("execution_cadence_protocol") != 1
            or device.get("cadence_policy_version") != version):
        raise ValueError("尚未核对同一执行器协议和节奏版本")
    state = {**preview, "mode": "active", "policy_version": version,
             "parameter_origins": {item["parameter"]: item for item in preview["items"]}}
    c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{execution_cadence}',%s) WHERE id=%s", (j(state), aid))
    audit(c, aid, "cadence.activated", {"preview_digest": preview_digest, "policy_version": version})
    emit(c, aid, "supervisor.wake")
    return state


def participation_due(c, aid, operation, attempts, now=None, *, account_id=None):
    from .community_participation import quota_due

    agent = require_agent(c, aid)
    growth = agent["onboarding_state"].get("community_growth", {})
    state = agent["onboarding_state"].get("execution_cadence", {})
    if operation not in {"join", "reply"}:
        return None
    if state.get("mode") == "paused":
        return {"reason": "cadence_paused", "next_eligible_at": None}
    waived = frozenset()
    if state.get("mode") == "active":
        device = agent["onboarding_state"].get("telegram_executor", {})
        relevant = {key: value for key, value in _bindings(c, aid).items() if account_id is None or key == str(account_id)}
        if (len(relevant) != 1 or device.get("execution_cadence_protocol") != 1
                or device.get("cadence_policy_version") != state["policy_version"]
                or state.get("config_revision") != agent["config_revision"]
                or state.get("strategy_digest") != _digest(growth)
                or any(state.get("account_bindings", {}).get(key) != value for key, value in relevant.items())):
            return {"reason": "cadence_version_mismatch", "next_eligible_at": None}
        waived = frozenset(key for key, item in state.get("parameter_origins", {}).items()
                           if item.get("origin") == "verified_system_default")
    return quota_due(growth.get("participation_policy", {}), operation, attempts, now, waived_defaults=waived)


def record_decision(c, aid: str, task_id: str, decision: Decision) -> None:
    c.execute("UPDATE lead.tasks SET result=coalesce(result,'{}') || %s WHERE agent_id=%s AND id=%s",
              (j({"execution_cadence": decision.model_dump(mode="json")}), aid, task_id))


def create_read_task(c, aid, payload, workflow_id):
    """Shared constructor; the caller holds control lock and revalidates scope."""
    row = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,started_at,scheduled_at,workflow_id)
        VALUES(%s,%s,'community.engage','running',%s,now(),now(),%s)
        ON CONFLICT(workflow_id) DO NOTHING RETURNING *""", (current_tenant(), aid, j(payload), workflow_id))
    row = row or one(c, "SELECT * FROM lead.tasks WHERE agent_id=%s AND workflow_id=%s", (aid, workflow_id))
    if not row:
        raise ValueError("读取任务的操作标识不属于当前项目")
    return row


def resume_eligible(c, aid: str, *, policy_version: int) -> list[str]:
    agent = require_agent(c, aid, True)
    state = agent["onboarding_state"].get("execution_cadence", {})
    if (agent["desired_state"] != "running" or state.get("mode") != "active"
            or state.get("policy_version") != policy_version
            or state.get("parameter_origins", {}).get("combined_per_24h", {}).get("origin") != "verified_system_default"):
        return []
    if one(c, "SELECT 1 FROM lead.actions WHERE agent_id=%s AND state IN ('dispatching','unknown') LIMIT 1", (aid,)):
        return []
    rows = all_rows(c, """SELECT DISTINCT ON (t.input->>'account_id',lower(t.input#>>'{candidate,telegram_url}')) t.*,s.binding_version
        FROM lead.tasks t JOIN lead.social_accounts s ON s.id::text=t.input->>'account_id' AND s.agent_id=t.agent_id
        JOIN lead.interaction_cards card ON card.id::text=t.input->>'delegation_id' AND card.agent_id=t.agent_id
        JOIN lead.tasks d ON d.id::text=t.input#>>'{candidate,discovery_task_id}' AND d.agent_id=t.agent_id
        WHERE t.agent_id=%s AND t.type='community.engage' AND t.state IN ('succeeded','failed')
          AND t.result#>>'{outcome,reason}'='daily_participation_limit'
          AND s.state='connected' AND s.automation_enabled AND s.platform='telegram'
          AND s.external_account_id=t.input->>'external_account_id'
          AND t.input->>'config_revision'=%s
          AND NOT (t.result ?| ARRAY['join_dispatched_at','reply_dispatched_at','reply_state','join_state','join_submitted','leave_state'])
          AND card.state='accepted' AND card.expires_at>now() AND card.type='manage_community'
          AND card.payload->>'account_id'=s.id::text AND card.payload->>'external_account_id'=s.external_account_id
          AND card.payload->'allowed_actions' ? 'read_recent_history'
          AND card.payload->>'purpose'='bounded_candidate_participation'
          AND d.type='community.discover' AND d.state='succeeded' AND d.completed_at>now()-interval '7 days'
          AND d.input->>'account_id'=s.id::text AND d.input->>'external_account_id'=s.external_account_id
          AND d.input->>'config_revision'=%s
          AND (card.payload->>'discovery_task'=d.id::text OR (card.payload->>'continue_discovery'='true'
               AND EXISTS(SELECT FROM lead.tasks anchor WHERE anchor.id::text=card.payload->>'discovery_task'
                   AND anchor.agent_id=t.agent_id AND anchor.type='community.discover' AND anchor.state='succeeded'
                   AND anchor.input->>'account_id'=s.id::text AND anchor.input->>'external_account_id'=s.external_account_id
                   AND anchor.input->>'config_revision'=t.input->>'config_revision' AND d.created_at>=anchor.created_at)))
          AND EXISTS(SELECT FROM jsonb_array_elements(d.result->'candidates') item
               WHERE lower(item->>'telegram_url')=lower(t.input#>>'{candidate,telegram_url}'))
          AND NOT EXISTS(SELECT FROM lead.tasks pending WHERE pending.agent_id=t.agent_id
               AND (pending.input->>'account_id'=s.id::text OR pending.input->>'account_id' IS NULL) AND (pending.result->>'reply_state' IN ('dispatching','unknown')
                    OR pending.result->>'join_state' IN ('dispatching','unknown','pending_approval')))
        ORDER BY t.input->>'account_id',lower(t.input#>>'{candidate,telegram_url}'),t.created_at DESC,t.id""",
                    (aid, str(agent["config_revision"]), str(agent["config_revision"])))
    ids = []
    for row in rows:
        if participation_due(c, aid, "join", [], account_id=row["input"]["account_id"]) is not None:
            continue
        captured = state.get("account_bindings", {}).get(row["input"]["account_id"], {})
        if captured.get("binding_version") != row["binding_version"]:
            continue
        payload = {key: row["input"][key] for key in ("candidate", "account_id", "external_account_id", "delegation_id")}
        payload.update(control_version=agent["control_version"], config_revision=agent["config_revision"],
                       cadence_resume_parent=str(row["id"]), cadence_policy_version=policy_version)
        successor = create_read_task(c, aid, payload, f"cadence-resume:{row['id']}:{policy_version}")
        ids.append(str(successor["id"]))
    if ids:
        emit(c, aid, "supervisor.wake")
    return ids
