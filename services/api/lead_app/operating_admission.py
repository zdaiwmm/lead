"""Rank useful work against real token headroom and scoped execution risk."""

import json
import math
import re
from urllib.parse import urlsplit

from .admin_settings import brand_context, effective
from .budget import available
from .db import all_rows, one


def model_readiness(models):
    from .config import resolve_model

    result = {}
    for purpose in ("planning", "research"):
        reference = models["task_models"].get(purpose) or models["default_model"]
        config = resolve_model(reference)
        result[purpose] = {
            "model": reference,
            "provider": config["provider"],
            "configured": bool(config["key"]),
        }
    return result


def context(c, aid, business):
    from .operating_progress import compact_work

    models, version = effective(c, "models", aid)
    material = {
        "business": business,
        "brand": brand_context(c, aid),
        "recent_work": compact_work(
            all_rows(
                c,
                "SELECT id,input,result FROM lead.tasks WHERE type LIKE 'operating.%%' AND type<>'operating.decision' AND state='succeeded' ORDER BY completed_at DESC LIMIT 8",
            )
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
        "model_readiness": model_readiness(models),
        "model_backoff": (
            one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (aid,)) or {}
        )
        .get("onboarding_state", {})
        .get("model_backoff", {}),
        "model_settings_version": version,
        "material_bytes": len(
            json.dumps(material, ensure_ascii=False, default=str).encode()
        ),
        "blocked_hosts": all_rows(
            c,
            "SELECT result->>'blocked_host' host,result->>'risk_retry_at' retry_at FROM lead.tasks WHERE state='failed' AND result->>'blocked_host' IS NOT NULL AND (result->>'risk_retry_at')::timestamptz>now()",
        ),
    }


def assess(ctx, kind, value, task=None, *, paid_step=None, request_units=None):
    name = kind.removeprefix("operating.")
    free = name in (
        "measurement",
        "audience",
        "kits",
        "account",
        "community_progress",
        "publication_reconcile",
        "publish",
    )
    if paid_step is not None:
        free = not paid_step
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
    if request_units is not None:
        # A freshly measured request supersedes the size of an older, larger prompt.
        estimate = request_units
    headroom = ctx["budget"]["available_units"]
    if headroom is None:
        headroom = float("inf")
    penalty = min(25, math.ceil(25 * estimate / max(1, headroom))) if estimate else 0
    if ctx.get("first_users"):
        penalty = 0
    score = value - risk - penalty
    backoff = ctx.get("model_backoff", {})
    purposes = (
        ("planning", "research")
        if name in ("opportunities", "community_discovery")
        else ("planning",)
        if name in ("content", "strategy", "publish")
        else ("research",)
    )
    missing = not free and any(
        not ctx.get("model_readiness", {}).get(p, {"configured": True})["configured"]
        for p in purposes
    )
    from .model_backoff import active

    references = [
        ctx["models"].get("task_models", {}).get(p)
        or ctx["models"].get("default_model")
        for p in purposes
    ]
    cooling = not free and active(backoff, ctx["model_settings_version"], references)
    payload = (task or {}).get("input", {})
    host = urlsplit(payload.get("candidate_url") or "").hostname
    if host in ("www.google.com", "google.com"):
        hint = re.search(r"https?://([^\s/›]+)", payload.get("candidate_label", ""))
        host = hint[1] if hint else host
    blocked_host = next(
        (r for r in ctx.get("blocked_hosts", []) if r["host"] == host), None
    )
    return {
        "allowed": estimate <= headroom
        and score > 0
        and not blocked_host
        and not cooling
        and not missing,
        "estimated_token_units": estimate,
        "value": value,
        "risk_penalty": risk,
        "budget_penalty": penalty,
        "priority": score,
        "reason": "model_not_configured"
        if missing
        else "model_action_required"
        if cooling and backoff.get("requires_owner_action")
        else "model_rate_limit_cooldown"
        if cooling
        else "public_host_risk_cooldown"
        if blocked_host
        else "token_budget_insufficient"
        if estimate > headroom
        else "no_positive_value"
        if score <= 0
        else "within_budget_and_scope",
        "retry_at": backoff.get("retry_at")
        if cooling
        else blocked_host["retry_at"]
        if blocked_host
        else None,
    }
