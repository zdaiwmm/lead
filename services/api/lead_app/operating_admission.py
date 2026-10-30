"""Rank useful work against real token headroom and scoped execution risk."""

import json
import math
import re
from urllib.parse import urlsplit

from .admin_settings import brand_context, effective
from .budget import available
from .db import all_rows


def context(c, aid, business):
    models, version = effective(c, "models", aid)
    material = {
        "business": business,
        "brand": brand_context(c, aid),
        "recent_work": all_rows(
            c,
            "SELECT input,result FROM lead.tasks WHERE type LIKE 'operating.%%' AND type<>'operating.decision' AND state='succeeded' ORDER BY completed_at DESC LIMIT 8",
        ),
        "preferences": all_rows(
            c,
            "SELECT body,source_refs FROM lead.memory_entries WHERE kind='preference' AND deleted_at IS NULL ORDER BY created_at DESC LIMIT 15",
        ),
    }
    # Estimates guide selection; model.reserve remains the atomic authority.
    return {
        "budget": available(c, aid),
        "models": models,
        "model_settings_version": version,
        "material_bytes": len(
            json.dumps(material, ensure_ascii=False, default=str).encode()
        ),
        "blocked_hosts": all_rows(
            c,
            "SELECT result->>'blocked_host' host,result->>'risk_retry_at' retry_at FROM lead.tasks WHERE state='failed' AND result->>'blocked_host' IS NOT NULL AND (result->>'risk_retry_at')::timestamptz>now()",
        ),
    }


def assess(ctx, kind, value, task=None):
    name = kind.removeprefix("operating.")
    free = name in ("measurement", "kits", "account", "community_progress")
    attempts = ((task or {}).get("result") or {}).get("attempts", 0)
    risk = {
        "measurement": 0,
        "kits": 0,
        "strategy": 1,
        "account": 6,
        # Only already verified, authorized targets reach this residual score.
        "publish": 3,
        "community_progress": 6,
    }.get(name, 2) + attempts * 4
    output = min(1800, ctx["models"]["max_output_tokens"])
    # Public observations and two-stage research need additional context/calls.
    evidence_bytes = (
        12000
        if name in ("journey", "validate", "community_discovery")
        else 24000
        if name == "opportunities"
        else 0
    )
    estimate = (
        0
        if free
        else 2 * (output + (ctx["material_bytes"] + evidence_bytes) // 2 + 1000)
    )
    if name == "opportunities":
        estimate *= 2
    estimate = max(
        estimate, ((task or {}).get("result") or {}).get("required_token_units") or 0
    )
    headroom = ctx["budget"]["available_units"]
    penalty = min(25, math.ceil(25 * estimate / max(1, headroom))) if estimate else 0
    score = value - risk - penalty
    payload = (task or {}).get("input", {})
    host = urlsplit(payload.get("candidate_url") or "").hostname
    if host in ("www.google.com", "google.com"):
        hint = re.search(r"https?://([^\s/›]+)", payload.get("candidate_label", ""))
        host = hint[1] if hint else host
    blocked_host = next(
        (r for r in ctx.get("blocked_hosts", []) if r["host"] == host), None
    )
    return {
        "allowed": estimate <= headroom and score > 0 and not blocked_host,
        "estimated_token_units": estimate,
        "value": value,
        "risk_penalty": risk,
        "budget_penalty": penalty,
        "priority": score,
        "reason": "public_host_risk_cooldown"
        if blocked_host
        else "token_budget_insufficient"
        if estimate > headroom
        else "no_positive_value"
        if score <= 0
        else "within_budget_and_scope",
        "retry_at": blocked_host["retry_at"] if blocked_host else None,
    }
