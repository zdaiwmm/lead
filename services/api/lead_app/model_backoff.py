"""Share provider backoff across background work, without stopping local recovery."""

import hashlib
import hmac
import math
from datetime import datetime, timedelta, timezone

from .db import j, require_agent, tx
from .scope import current_tenant

SAFE_CODES = {
    "rate_limit_exceeded",
    "slow_down",
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
    "project_usage_limit_exceeded",
    "invalid_api_key",
    "model_not_found",
}


def credential_revision(reference):
    from .config import CONFIG, resolve_model

    key = resolve_model(reference)["key"]
    return (
        hmac.new(
            CONFIG["session_secret"].encode(), key.encode(), hashlib.sha256
        ).hexdigest()
        if key
        else None
    )


def active(state, version, references):
    from .config import model_reference, resolve_model

    if state.get("model_settings_version") != version:
        return False
    reference = state.get("model_reference")
    references = [model_reference(resolve_model(r)) for r in references if r]
    if reference and reference not in references:
        return False
    if reference and state.get("credential_revision") != credential_revision(reference):
        return False
    return bool(
        state.get("requires_owner_action")
        or state.get("retry_at")
        and datetime.fromisoformat(state["retry_at"]) > datetime.now(timezone.utc)
    )


def diagnostic(error):
    code = getattr(error, "provider_error_code", None)
    body = getattr(error, "body", None)
    if not code and isinstance(body, dict):
        code = (
            (body.get("error") or body).get("code")
            if isinstance(body.get("error") or body, dict)
            else None
        )
    code = code if code in SAFE_CODES else None
    status = getattr(error, "status_code", None)
    kind = (
        "quota"
        if status == 402
        or code
        in SAFE_CODES
        - {"rate_limit_exceeded", "slow_down", "invalid_api_key", "model_not_found"}
        else "authentication"
        if status == 401 or code == "invalid_api_key"
        else "model_unavailable"
        if status == 404 or code == "model_not_found"
        else "rate_limit"
    )
    return {
        "kind": kind,
        "provider_error_code": code,
        "status_code": status if isinstance(status, int) else None,
        "requires_owner_action": kind != "rate_limit",
    }


def rate_limited(aid, version, error=None, reference=None):
    with tx(aid) as c:
        a = require_agent(c, aid, True)
        old = a["onboarding_state"].get("model_backoff", {})
        failures = (
            old.get("failures", 0) + 1
            if old.get("model_settings_version") == version
            and old.get("model_reference") == reference
            else 1
        )
        detail = (
            diagnostic(error)
            if error
            else {"kind": "rate_limit", "requires_owner_action": False}
        )
        delay = min(3600, 60 * 2 ** min(failures, 6))
        retry_after = getattr(error, "retry_after", None)
        if (
            isinstance(retry_after, (int, float))
            and math.isfinite(retry_after)
            and 0 <= retry_after <= 365 * 86400
        ):
            delay = max(delay, retry_after)
        state = {
            **detail,
            "failures": failures,
            "model_settings_version": version,
            "model_reference": reference,
            "credential_revision": credential_revision(reference)
            if reference
            else None,
            "retry_at": (
                datetime.now(timezone.utc) + timedelta(seconds=delay)
            ).isoformat(),
        }
        c.execute(
            "UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{model_backoff}',%s) WHERE id=%s",
            (j(state), aid),
        )
        c.execute(
            "INSERT INTO lead.memory_entries(tenant_id,agent_id,kind,body,source_refs,confirmation) VALUES(%s,%s,'observation',%s,'[]','system_observed')",
            (
                current_tenant(),
                aid,
                j(
                    {
                        "type": "model_service_failure",
                        "model_reference": reference,
                        "configuration_version": version,
                        "at": datetime.now(timezone.utc).isoformat(),
                        **detail,
                    }
                ),
            ),
        )


def recovered(aid, reference=None):
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state-'model_backoff' WHERE id=%s AND onboarding_state ? 'model_backoff' AND (%s::text IS NULL OR onboarding_state->'model_backoff'->>'model_reference' IS NULL OR onboarding_state->'model_backoff'->>'model_reference'=%s)",
            (aid, reference, reference),
        )
