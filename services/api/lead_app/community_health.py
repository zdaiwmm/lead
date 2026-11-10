"""Durable, account/peer-scoped circuit breakers. A new task is not a new attempt.

Stored in the existing tenant-isolated observation ledger, so rollout does not
need a schema migration. No message text or credentials are stored here.
"""

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from .db import all_rows, j, one
from .scope import current_tenant

TYPE = "community_target_health"
TRANSIENT = {"navigation_timeout", "target_mismatch", "menu_intercepted", "browser_closed", "transport_error", "stalled_checkpoint"}


def utcnow():
    return datetime.now(timezone.utc)


def canonical_url(value):
    parts = urlsplit(str(value or "").replace("http://", "https://"))
    # Invite hashes are case sensitive; public usernames are not.
    path = parts.path.rstrip("/")
    if not path.startswith(("/+", "/joinchat/")):
        path = path.casefold()
    return "https://t.me" + path if parts.hostname in ("t.me", "telegram.me") else str(value or "").rstrip("/")


def fault(error, stage, *, uncertain=False, evidence_ref=None):
    """Normalize the RPC envelope; support old browser nodes during rollout."""
    detail = str(error)
    response = getattr(error, "response", None)
    if response is not None:
        try:
            detail = response.json().get("detail", detail)
        except (ValueError, AttributeError):
            pass
    if isinstance(detail, dict) and detail.get("error_code"):
        return {**detail, "write_may_have_happened": bool(uncertain or detail.get("write_may_have_happened"))}
    message = str(detail)
    text = (type(error).__name__ + " " + message).casefold()
    status = getattr(response, "status_code", None)
    retry_after = None
    if response is not None:
        header = response.headers.get("retry-after", "")
        if header.isdigit():
            retry_after = int(header)
    flood = re.search(r"flood_wait[_ :]+(\d+)", text)
    if flood:
        retry_after = int(flood.group(1))
    code, scope, retryable = "browser_error", "target", False
    if "intercepts pointer" in text and "menu" in text:
        code, retryable = "menu_intercepted", True
    elif "targetclosed" in text or "browser has been closed" in text:
        code, scope, retryable = "browser_closed", "account", True
    elif "targetmismatch" in text:
        code, retryable = "target_mismatch", True
    elif "timeout" in text:
        code, retryable = "navigation_timeout", True
    elif status in {500, 502, 503, 504} or any(word in text for word in ("connecterror", "connectionerror", "readerror", "connection reset")):
        code, scope, retryable = "transport_error", "account", True
    elif status == 429 or "429" in text or "flood_wait" in text:
        code, scope, retryable = "rate_limited", "account", True
    elif any(word in text for word in ("unauthorized", "登录失效", "captcha", "验证码")):
        code, scope = "authentication_required", "account"
    return {"error_code": code, "stage": stage, "scope": scope, "retryable": retryable,
            "write_may_have_happened": uncertain, "retry_after": retry_after,
            "evidence_ref": evidence_ref, "message": message[:1500]}


def permission_evidence(observed):
    # Message churn and a different draft must never count as repaired permission.
    return {key: observed.get(key) for key in ("joined", "can_reply", "verification_required", "membership_state")}


def transition(previous, event, now=None):
    now = now or utcnow()
    saved = dict(previous or {})
    code = event["error_code"]
    same = saved.get("error_code") == code and saved.get("state") != "healthy"
    count = int(saved.get("failure_count", 0)) + 1 if same else 1
    seconds = max(120 if count == 1 else 7200, int(event.get("retry_after") or 0))
    return {**saved, "state": "blocked", "error_code": code, "failure_count": count,
            "first_failure_at": saved.get("first_failure_at") if same else now.isoformat(),
            "last_failure_at": now.isoformat(), "read_after": (now + timedelta(seconds=max(min(seconds, 21600), int(event.get("retry_after") or 0)))).isoformat(),
            "write_after": (now + timedelta(seconds=seconds)).isoformat(),
            "requires_changed_evidence": code not in TRANSIENT,
            "fault": event, "recovery_actions": saved.get("recovery_actions", {}) if same else {},
            "next_action": "read_original_target" if event.get("retryable", True) else "verify_permission_or_authorization",
            "updated_at": now.isoformat()}


def recover(previous, observed, now=None):
    now = now or utcnow()
    saved = dict(previous)
    if saved.get("state") == "healthy":
        return saved
    evidence = permission_evidence(observed)
    previous = saved.get("last_permission_evidence") or saved.get("permission_evidence") or {}
    changed = any(previous.get(key) is not None and value is not None and previous[key] != value
                  for key, value in evidence.items())
    due = not saved.get("write_after") or datetime.fromisoformat(saved["write_after"]) <= now
    writable = observed.get("joined") and observed.get("can_reply") and not observed.get("verification_required")
    # A completed, identity-bound read proves navigation recovery. An explicit
    # send failure needs changed permissions (or an owner action), not time alone.
    repaired = saved.get("error_code") in TRANSIENT or (due and changed and writable) or (
        saved.get("error_code") == "authentication_required" and observed.get("identity_verified") is True) or (
        due and saved.get("error_code") == "rate_limited" and observed.get("identity_verified") is True)
    return {**saved, "state": "healthy" if repaired else "blocked",
            "last_permission_evidence": evidence,
            "last_read_at": now.isoformat(), "read_after": (now + timedelta(hours=6)).isoformat(),
            "recovered_at": now.isoformat() if repaired else saved.get("recovered_at"),
            "next_action": "resume" if repaired else "wait_for_changed_evidence"}


def identity(task):
    payload, result = task.get("input") or {}, task.get("result") or {}
    return str(payload.get("external_account_id") or ""), str(result.get("target_id") or ""), canonical_url(payload.get("candidate", {}).get("telegram_url"))


def _row(c, aid, account, peer, url):
    # Serializes alias promotion and failure updates for this account only.
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (f"community-health:{current_tenant()}:{aid}:{account}",))
    rows = all_rows(c, """SELECT id,body FROM lead.memory_entries WHERE agent_id=%s
        AND kind='observation' AND body->>'type'=%s AND body->>'account'=%s
        AND (body->>'peer'=%s AND %s<>'' OR body->'urls' ? %s
             AND (%s='' OR coalesce(body->>'peer','')='' OR body->>'peer'=%s))
        ORDER BY (body->>'state'='blocked') DESC,body->>'last_failure_at' DESC FOR UPDATE""",
        (aid, TYPE, account, peer, peer, url, peer, peer))
    if not rows:
        return None
    row = rows[0]
    body = row["body"]
    before = dict(body)
    body["urls"] = sorted({url, *(u for r in rows for u in r["body"].get("urls", []))} - {""})
    if peer:
        body["peer"] = peer
    # Keep the stricter state, and every alias, when a URL acquires its peer ID.
    for duplicate in rows[1:]:
        c.execute("DELETE FROM lead.memory_entries WHERE id=%s", (duplicate["id"],))
    if body != before or len(rows) > 1:
        _save(c, aid, row["id"], body)
    return row


def _save(c, aid, key, body):
    c.execute("""INSERT INTO lead.memory_entries(id,tenant_id,agent_id,kind,body,source_refs,confirmation)
        VALUES(%s,%s,%s,'observation',%s,%s,'system_observed')
        ON CONFLICT(id) DO UPDATE SET body=excluded.body,source_refs=excluded.source_refs""",
        (key, current_tenant(), aid, j(body), j([body["task_id"]] if body.get("task_id") else [])))


def get(c, aid, task):
    account, peer, url = identity(task)
    row = _row(c, aid, account, peer, url)
    return row["body"] if row else None


def failed(c, aid, task, event, *, occurred_at=None):
    account, peer, url = identity(task)
    if not account or not (peer or url):
        return
    row = _row(c, aid, account, peer, url)
    prior = row["body"] if row else {}
    attempt = str(task["id"]) + ":" + str(event.get("evidence_ref") or event["error_code"])
    if prior.get("last_attempt") == attempt:
        return prior
    state = transition(prior, event, occurred_at)
    state.update(type=TYPE, account=account, peer=peer or prior.get("peer", ""),
                 urls=sorted({url, *prior.get("urls", [])} - {""}), task_id=str(task["id"]),
                 last_attempt=attempt, permission_evidence=permission_evidence((task.get("result") or {}).get("history_observation") or {}))
    state["last_permission_evidence"] = state["permission_evidence"]
    key = row["id"] if row else uuid5(NAMESPACE_URL, f"{current_tenant()}:{aid}:{TYPE}:{account}:{peer or url}")
    _save(c, aid, key, state)
    peers = one(c, """SELECT count(*) AS n FROM lead.memory_entries WHERE agent_id=%s
        AND body->>'type'=%s AND body->>'account'=%s AND body->>'peer'<>'*'
        AND body->>'error_code'=%s AND (body->>'last_failure_at')::timestamptz>now()-interval '15 minutes'""",
        (aid, TYPE, account, event["error_code"]))["n"]
    if peers >= 3 or event.get("scope") == "account":
        account_key = uuid5(NAMESPACE_URL, f"{current_tenant()}:{aid}:{TYPE}:{account}:*")
        platform_wait = int(event.get("retry_after") or 0)
        _save(c, aid, account_key, {**state, "peer": "*", "urls": [], "write_after": (utcnow()+timedelta(seconds=max(7200, platform_wait))).isoformat(),
                                  "read_after": (utcnow()+timedelta(seconds=max(300, platform_wait))).isoformat()})
    return state


def observed(c, aid, task, observation):
    # Only called after the gateway completed an account- and peer-bound read.
    observation = {**observation, "identity_verified": True}
    account, peer, url = identity(task)
    row = _row(c, aid, account, peer, url)
    if row:
        _save(c, aid, row["id"], recover(row["body"], observation))
    # Account navigation failures may be cleared by a successful bound read;
    # permission/authentication faults are never cleared by browser restart.
    account_row = _row(c, aid, account, "*", "")
    if account_row:
        state = recover(account_row["body"], observation)
        due = datetime.fromisoformat(state["write_after"]) <= utcnow()
        if due and observation.get("joined") and observation.get("can_reply") and not observation.get("verification_required"):
            # A successful half-open account probe releases other targets; each
            # failed target keeps its own changed-evidence barrier.
            state.update(state="healthy", recovered_at=utcnow().isoformat(), next_action="resume")
        _save(c, aid, account_row["id"], state)


def blocked(c, aid, task, *, write=True):
    account, peer, url = identity(task)
    target = get(c, aid, task)
    account_row = _row(c, aid, account, "*", "")
    for state in (target, account_row["body"] if account_row else None):
        if state and state.get("state") == "blocked":
            if write or datetime.fromisoformat(state["read_after"]) > utcnow():
                return state
    return None


def current_binding_sql(alias="t", *, legacy_receipts=False):
    """Match the Telegram identity that owned the task, even after a slot is reused."""
    if alias not in {"t", "recent", "old", "lead.tasks"}:
        raise ValueError("unsupported task alias")
    bound = f"""EXISTS (SELECT FROM lead.social_accounts bound
        WHERE bound.agent_id={alias}.agent_id AND bound.id::text={alias}.input->>'account_id'
          AND bound.external_account_id={alias}.input->>'external_account_id'
          AND bound.platform='telegram' AND bound.state='connected' AND bound.automation_enabled)"""
    if not legacy_receipts:
        return bound
    # Older unresolved submissions may lack identity fields. Keep their original
    # receipt-only path and backoff. The browser verifies identity before access.
    return f"""({bound} OR (
        ({alias}.input->>'account_id' IS NULL OR {alias}.input->>'external_account_id' IS NULL)
        AND ({alias}.state='reconciling' OR ({alias}.state='running' AND
            ({alias}.result->>'join_state' IN ('dispatching','unknown','pending_approval')
             OR {alias}.result->>'reply_state' IN ('dispatching','unknown'))))))"""


def eligible_sql(alias="t", *, legacy_receipts=False):
    """Read scheduling gate. All dynamic SQL names are internal constants."""
    return current_binding_sql(alias, legacy_receipts=legacy_receipts) + f""" AND NOT EXISTS (SELECT 1 FROM lead.memory_entries health
        WHERE health.agent_id={alias}.agent_id AND health.body->>'type'='{TYPE}'
        AND health.body->>'account'={alias}.input->>'external_account_id'
        AND health.body->>'state'='blocked' AND (health.body->>'read_after')::timestamptz>now()
        AND (health.body->>'peer'='*' OR health.body->>'peer'={alias}.result->>'target_id'
             OR health.body->'urls' ? rtrim(lower({alias}.input#>>'{{candidate,telegram_url}}'),'/')
                AND (coalesce({alias}.result->>'target_id','')='' OR coalesce(health.body->>'peer','')='')))"""


def claim_recovery(c, aid, task, action):
    account, peer, url = identity(task)
    row = _row(c, aid, account, peer, url)
    if not row or row["body"].get("recovery_actions", {}).get(action):
        return False
    state = row["body"]
    state.setdefault("recovery_actions", {})[action] = utcnow().isoformat()
    _save(c, aid, row["id"], state)
    return True


def evidence_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def bootstrap(c, aid):
    """Import unresolved explicit failures once; rollout must protect old tasks."""
    agent = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (aid,))
    if agent["onboarding_state"].get("community_health_version") == 1:
        return
    rows = all_rows(c, """SELECT t.* FROM lead.tasks t WHERE t.agent_id=%s AND t.type='community.engage'
        AND t.result->>'reply_state'='failed' AND NOT EXISTS (
          SELECT 1 FROM lead.tasks newer WHERE newer.agent_id=t.agent_id AND newer.type=t.type
            AND newer.input->>'external_account_id'=t.input->>'external_account_id'
            AND (newer.result->>'target_id'=t.result->>'target_id'
                 OR lower(newer.input#>>'{candidate,telegram_url}')=lower(t.input#>>'{candidate,telegram_url}'))
            AND newer.created_at>t.created_at AND newer.result->>'reply_state'='confirmed')
        ORDER BY t.created_at""", (aid,))
    for task in rows:
        saved = task["result"] or {}
        at = datetime.fromisoformat(saved["reply_dispatched_at"]) if saved.get("reply_dispatched_at") else task["completed_at"] or task["created_at"]
        failed(c, aid, task, {"error_code": "send_failed", "stage": "reply", "scope": "target", "retryable": True, "write_may_have_happened": False}, occurred_at=at)
    c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s", (j({"community_health_version": 1}), aid))
