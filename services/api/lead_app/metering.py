"""Measured units with operator-versioned prices; missing prices remain unknown."""

import json
from decimal import Decimal, InvalidOperation

from .config import STATE
from .db import all_rows, one, require_agent, tx


def priced(details):
    result = {
        k: v
        for k, v in details.items()
        if k
        in (
            "model",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "requests",
            "unmeasured_requests",
            "cache_breakdown_verified",
        )
    }
    result["cost"] = None
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
            "SELECT units,details FROM lead.usage_events WHERE occurred_at>now()-interval '30 days'",
        )
        browser = one(
            c,
            "SELECT coalesce(sum(extract(epoch FROM (coalesce(ended_at,now())-started_at))),0) AS seconds, coalesce(sum((usage->>'proxy_bytes')::bigint),0) AS network_bytes, coalesce(sum((usage->>'cpu_sampled_seconds')::numeric),0) AS cpu_sampled_seconds, coalesce(max((usage->>'rss_sum_peak_bytes')::bigint),0) AS rss_sum_peak_bytes, count(*) FILTER(WHERE usage='{}'::jsonb) AS unmetered_sessions FROM lead.browser_sessions WHERE started_at>now()-interval '30 days'",
        )
        storage = one(
            c,
            "SELECT coalesce(sum(size_bytes),0) AS bytes FROM lead.assets WHERE state='ready'",
        )
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
        "browser_seconds": float(browser["seconds"]),
        "network_bytes": browser["network_bytes"],
        "cpu_sampled_seconds": float(browser["cpu_sampled_seconds"]),
        "rss_sum_peak_bytes": browser["rss_sum_peak_bytes"],
        "unmetered_browser_sessions": browser["unmetered_sessions"],
        "asset_bytes": storage["bytes"],
        "infrastructure_cost": None,
        "quotas": periods,
    }
