"""Measured units with operator-versioned prices; missing prices remain unknown."""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from .config import STATE
from .db import all_rows, one, require_agent, tx


def model_identity(details):
    reference = details.get("model") or "unrecorded"
    provider = details.get("provider")
    if reference.startswith(("openai:", "deepseek:")):
        provider = reference.split(":", 1)[0]
    if not provider and reference.startswith("deepseek"):
        provider = "deepseek"
    return provider or "unrecorded", reference


def model_usage(c, aid=None, active_only=False):
    """Actual billing stays with its recorded model; unlabelled history stays unknown."""
    events = all_rows(
        c,
        "SELECT units,details,occurred_at FROM lead.usage_events WHERE category='model_tokens' AND occurred_at>now()-interval '30 days' AND (%s::uuid IS NULL OR agent_id=%s) AND (NOT %s OR agent_id IN(SELECT id FROM lead.agents WHERE desired_state<>'archived'))",
        (aid, aid, active_only),
    )
    reservations = all_rows(
        c,
        "SELECT model_ref,provider,state,units FROM lead.budget_reservations WHERE state IN ('reserved','unknown') AND (%s::uuid IS NULL OR agent_id=%s) AND (NOT %s OR agent_id IN(SELECT id FROM lead.agents WHERE desired_state<>'archived'))",
        (aid, aid, active_only),
    )
    rows = {}

    def bucket(details):
        provider, reference = model_identity(details)
        if (provider, reference) not in rows:
            rows[(provider, reference)] = {
                "provider": provider,
                "model": reference,
                "tokens": 0,
                "today_tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_input_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_30m_tokens": 0,
                "cache_write_12h_tokens": 0,
                "unverified_cache_write_calls": 0,
                "invocations": 0,
                "requests": 0,
                "unpriced_calls": 0,
                "missing_breakdown_calls": 0,
                "unverified_cache_calls": 0,
                "reserved_tokens": 0,
                "unknown_reserved_tokens": 0,
                "costs": {},
            }
        return rows[(provider, reference)]

    start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for event in events:
        details = event["details"]
        row = bucket(details)
        row["tokens"] += event["units"]
        if event["occurred_at"] >= start:
            row["today_tokens"] += event["units"]
        row["invocations"] += 1
        row["requests"] += details.get("requests") or 0
        row["input_tokens"] += details.get("input_tokens") or 0
        row["output_tokens"] += details.get("output_tokens") or 0
        row["cached_input_tokens"] += details.get("cache_read_tokens") or 0
        for name in (
            "cache_write_tokens",
            "cache_write_30m_tokens",
            "cache_write_12h_tokens",
        ):
            row[name] += details.get(name) or 0
        row["unverified_cache_write_calls"] += int(
            bool(details.get("cache_write_tokens"))
            and not details.get("cache_write_breakdown_verified")
        )
        row["missing_breakdown_calls"] += int(
            details.get("input_tokens") is None or details.get("output_tokens") is None
        )
        row["unverified_cache_calls"] += int(
            not details.get("cache_breakdown_verified")
        )
        row["unpriced_calls"] += int(details.get("cost") is None)
        if details.get("cost") is not None:
            currency = details["currency"]
            row["costs"][currency] = str(
                Decimal(row["costs"].get(currency, "0")) + Decimal(details["cost"])
            )
    for reservation in reservations:
        row = bucket(
            {"model": reservation["model_ref"], "provider": reservation["provider"]}
        )
        row[
            "reserved_tokens"
            if reservation["state"] == "reserved"
            else "unknown_reserved_tokens"
        ] += reservation["units"]
    return sorted(
        rows.values(), key=lambda row: (-row["tokens"], row["provider"], row["model"])
    )


def priced(details, snapshot=None):
    result = {
        k: v
        for k, v in details.items()
        if k
        in (
            "model",
            "provider",
            "api",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cache_write_30m_tokens",
            "cache_write_12h_tokens",
            "cache_write_breakdown_verified",
            "unmeasured_cache_write_requests",
            "requests",
            "unmeasured_requests",
            "cache_breakdown_verified",
            "elapsed_seconds",
            "request_profile",
            "configuration_version",
            "brand_configuration_version",
            "purpose",
            "error_type",
            "unpriced_service_tier_requests",
        )
    }
    result["cost"] = None
    if snapshot is not None:
        from .admin_pricing import calculate

        return {**result, **calculate(result, snapshot)}
    try:
        rates = json.loads((STATE / "operator-rates.json").read_text())
        model = rates["models"][result["model"]]
        if (
            result.get("unmeasured_requests")
            or result.get("cache_write_tokens")
            or not result.get("cache_breakdown_verified")
        ):
            return result
        hit = result["cache_read_tokens"]
        miss = result["input_tokens"] - hit
        output = result["output_tokens"]
        if min(hit, miss, output) < 0:
            return result
        values = [
            Decimal(str(model[name]))
            for name in (
                "input_per_million",
                "cached_input_per_million",
                "output_per_million",
            )
        ]
        if any(not v.is_finite() or v < 0 for v in values):
            return result
        amount = sum(a * b for a, b in zip((miss, hit, output), values)) / Decimal(
            1000000
        )
        if not rates["version"] or not rates["currency"] or not rates["effective_at"]:
            return result
        result.update(
            cost=str(amount),
            currency=rates["currency"],
            rate_version=rates["version"],
            rate_effective_at=rates["effective_at"],
        )
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
        pass
    return result


def summary(aid):
    with tx(aid) as c:
        require_agent(c, aid)
        periods = all_rows(
            c,
            "SELECT starts_at,ends_at,unit,limit_units,spent_units,reserved_units FROM lead.budget_periods ORDER BY starts_at DESC LIMIT 30",
        )
        events = all_rows(
            c,
            "SELECT units,details FROM lead.usage_events WHERE category='model_tokens' AND occurred_at>now()-interval '30 days'",
        )
        browser = one(
            c,
            "SELECT coalesce(sum(extract(epoch FROM (coalesce(ended_at,now())-started_at))),0) AS seconds, coalesce(sum((usage->>'proxy_bytes')::bigint),0) AS network_bytes, coalesce(sum((usage->>'cpu_sampled_seconds')::numeric),0) AS cpu_sampled_seconds, coalesce(max((usage->>'rss_sum_peak_bytes')::bigint),0) AS rss_sum_peak_bytes, count(*) FILTER(WHERE usage='{}'::jsonb) AS unmetered_sessions FROM lead.browser_sessions WHERE started_at>now()-interval '30 days'",
        )
        storage = one(
            c,
            "SELECT coalesce(sum(size_bytes),0) AS bytes FROM lead.assets WHERE state='ready'",
        )
        by_model = model_usage(c, aid)
    costs = {}
    for event in events:
        d = event["details"]
        if d.get("cost") is not None:
            currency = d["currency"]
            costs[currency] = str(
                Decimal(costs.get(currency, "0")) + Decimal(d["cost"])
            )
    return {
        "period": "30d",
        "by_model": by_model,
        "model_tokens": sum(e["units"] for e in events),
        "input_tokens": sum(e["details"].get("input_tokens") or 0 for e in events),
        "output_tokens": sum(e["details"].get("output_tokens") or 0 for e in events),
        "cached_input_tokens": sum(
            e["details"].get("cache_read_tokens") or 0 for e in events
        ),
        "unpriced_calls": sum(e["details"].get("cost") is None for e in events),
        "unverified_cache_calls": sum(
            not e["details"].get("cache_breakdown_verified") for e in events
        ),
        "model_costs": costs,
        "pricing_versions": list(
            {
                (
                    d.get("model"),
                    d.get("currency"),
                    d.get("rate_version"),
                    d.get("rate_effective_at"),
                )
                for e in events
                if (d := e["details"]).get("cost") is not None
            }
        ),
        "browser_seconds": float(browser["seconds"]),
        "network_bytes": browser["network_bytes"],
        "cpu_sampled_seconds": float(browser["cpu_sampled_seconds"]),
        "rss_sum_peak_bytes": browser["rss_sum_peak_bytes"],
        "unmetered_browser_sessions": browser["unmetered_sessions"],
        "asset_bytes": storage["bytes"],
        "infrastructure_cost": None,
        "quotas": periods,
    }
