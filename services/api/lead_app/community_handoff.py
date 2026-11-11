"""Immutable qualified checkpoints; partial batches need the original delegation."""
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from .db import all_rows, emit, j, one, require_agent
from .scope import current_tenant
from .community_health import canonical_url

TYPE = "community_candidate_handoff"


def digest(candidate):
    return hashlib.sha256(json.dumps(candidate, sort_keys=True, default=str).encode()).hexdigest()


def publish_candidate(c, aid: str, discovery_id: str, candidate: dict, snapshot: dict) -> str:
    agent = require_agent(c, aid, True)
    parent = one(c, "SELECT * FROM lead.tasks WHERE agent_id=%s AND id=%s AND type='community.discover' AND state IN ('running','succeeded')", (aid, discovery_id))
    account = one(c, "SELECT * FROM lead.social_accounts WHERE agent_id=%s AND id=%s AND platform='telegram' AND state='connected' AND automation_enabled", (aid, snapshot["account_id"]))
    if (not parent or not account or agent["desired_state"] != "running"
            or snapshot.get("control_version") != agent["control_version"]
            or snapshot.get("config_revision") != agent["config_revision"]
            or snapshot.get("binding_version") != account["binding_version"]
            or snapshot.get("external_account_id") != account["external_account_id"]
            or any(str(parent["input"].get(key)) != str(snapshot.get(key)) for key in ("account_id", "external_account_id", "config_revision", "control_version"))
            or not candidate.get("evidence_id") or not candidate.get("reading_need")
            or candidate not in (parent["result"] or {}).get("candidates", [])):
        raise ValueError("候选缺少已核验的当前发现检查点")
    url = canonical_url(candidate["telegram_url"])
    hid = str(uuid5(NAMESPACE_URL, f"{current_tenant()}:{aid}:{discovery_id}:{url}:{snapshot['binding_version']}:{snapshot['config_revision']}"))
    body = {"type": TYPE, "discovery_task_id": str(discovery_id), "candidate": candidate,
            "assessment_digest": digest(candidate), "snapshot": snapshot, "canonical_url": url}
    inserted = one(c, """INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation,expires_at)
        VALUES(%s,%s,%s,'observation',%s,%s,'observed',now()+interval '7 days') ON CONFLICT(id) DO NOTHING RETURNING id""",
        (hid, current_tenant(), aid, j(body), j([str(discovery_id), candidate["evidence_id"]])))
    if inserted:
        emit(c, aid, "supervisor.wake")
    return hid


def read_authority(c, aid, account, revision, discovery_id, *, card_id=None):
    """Return current card only; a signal or metadata row cannot create authority."""
    return one(c, """SELECT card.id FROM lead.interaction_cards card
        JOIN lead.tasks anchor ON anchor.id::text=card.payload->>'discovery_task' AND anchor.agent_id=card.agent_id
        JOIN lead.tasks d ON d.id=%s AND d.agent_id=card.agent_id
        JOIN lead.agents a ON a.id=d.agent_id
        JOIN lead.social_accounts s ON s.id=%s AND s.agent_id=d.agent_id
        WHERE card.agent_id=%s AND (%s::text IS NULL OR card.id::text=%s) AND card.type='manage_community' AND card.state='accepted' AND card.expires_at>now()
          AND card.payload->>'purpose'='bounded_candidate_participation'
          AND card.payload->>'account_id'=s.id::text AND card.payload->>'external_account_id'=s.external_account_id
          AND card.payload->'allowed_actions' ?& ARRAY['read_recent_history','join_public_group']
          AND a.desired_state='running' AND a.config_revision=%s
          AND s.state='connected' AND s.automation_enabled AND s.platform='telegram'
          AND d.type='community.discover' AND d.state IN ('running','succeeded') AND d.created_at>now()-interval '7 days'
          AND d.input->>'account_id'=s.id::text AND d.input->>'external_account_id'=s.external_account_id
          AND d.input->>'config_revision'=a.config_revision::text AND d.input->>'control_version'=a.control_version::text
          AND anchor.type='community.discover' AND anchor.state='succeeded'
          AND anchor.input->>'account_id'=s.id::text AND anchor.input->>'external_account_id'=s.external_account_id
          AND anchor.input->>'config_revision'=a.config_revision::text
          AND ((d.id=anchor.id AND d.state='succeeded') OR (card.payload->>'continue_discovery'='true' AND d.created_at>=anchor.created_at))
        ORDER BY card.created_at DESC LIMIT 1""", (discovery_id, account["id"], aid, card_id, card_id, revision))


def next_handoff(c, aid: str, account: dict, revision: int) -> dict | None:
    if not aid or not account:
        return None
    rows = all_rows(c, """SELECT h.id,h.body,s.binding_version,s.external_account_id,d.result
        FROM lead.memory_entries h JOIN lead.tasks d ON d.id::text=h.body->>'discovery_task_id' AND d.agent_id=h.agent_id
        JOIN lead.social_accounts s ON s.id::text=h.body#>>'{snapshot,account_id}' AND s.agent_id=h.agent_id
        WHERE h.agent_id=%s AND h.body->>'type'=%s AND h.deleted_at IS NULL AND h.expires_at>now()
          AND s.id=%s AND s.binding_version::text=h.body#>>'{snapshot,binding_version}'
          AND s.external_account_id=h.body#>>'{snapshot,external_account_id}'
          AND h.body#>>'{snapshot,config_revision}'=%s
          AND NOT EXISTS(SELECT FROM lead.tasks used WHERE used.agent_id=h.agent_id AND used.type='community.engage'
            AND used.input->>'account_id'=s.id::text AND used.input->>'external_account_id'=s.external_account_id
            AND rtrim(lower(used.input#>>'{candidate,telegram_url}'),'/')=h.body->>'canonical_url' AND used.state<>'canceled')
        ORDER BY h.created_at,h.id""", (aid, TYPE, account["id"], str(revision)))
    for row in rows:
        body = row["body"]
        candidate = body["candidate"]
        if not any(digest(item) == body["assessment_digest"] for item in (row["result"] or {}).get("candidates", [])):
            continue
        authority = read_authority(c, aid, account, revision, body["discovery_task_id"])
        if authority:
            return {**candidate, "handoff_id": str(row["id"]), "discovery_task_id": body["discovery_task_id"], "handoff_delegation_id": str(authority["id"])}
    return None


def valid_handoff(c, aid, account, revision, candidate):
    row = one(c, """SELECT h.body,d.result,s.binding_version,s.external_account_id
        FROM lead.memory_entries h JOIN lead.tasks d ON d.id::text=h.body->>'discovery_task_id' AND d.agent_id=h.agent_id
        JOIN lead.social_accounts s ON s.id::text=h.body#>>'{snapshot,account_id}' AND s.agent_id=h.agent_id
        WHERE h.agent_id=%s AND h.id::text=%s AND h.body->>'type'=%s AND s.id=%s
          AND h.deleted_at IS NULL AND h.expires_at>now()""", (aid, candidate.get("handoff_id"), TYPE, account["id"]))
    if not row:
        return False
    body = row["body"]
    return bool(body["snapshot"].get("binding_version") == row["binding_version"]
        and body["snapshot"].get("external_account_id") == row["external_account_id"]
        and body["snapshot"].get("config_revision") == revision
        and body["discovery_task_id"] == candidate.get("discovery_task_id")
        and all(candidate.get(key) == value for key,value in body["candidate"].items())
        and any(digest(item)==body["assessment_digest"] for item in (row["result"] or {}).get("candidates", []))
        and read_authority(c, aid, account, revision, body["discovery_task_id"], card_id=candidate.get("handoff_delegation_id")))
