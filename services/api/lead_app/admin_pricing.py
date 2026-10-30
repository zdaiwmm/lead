"""Operator-confirmed token rates, pinned at admission and never applied retroactively."""

import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .db import one


class ModelRate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    model: str = Field(min_length=1, max_length=120)
    input_per_million: Decimal = Field(ge=0, le=100000, max_digits=16, decimal_places=8)
    cached_input_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, max_digits=16, decimal_places=8
    )
    cache_write_30m_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, max_digits=16, decimal_places=8
    )
    cache_write_12h_per_million: Decimal | None = Field(
        default=None, ge=0, le=100000, max_digits=16, decimal_places=8
    )
    output_per_million: Decimal = Field(
        ge=0, le=100000, max_digits=16, decimal_places=8
    )
    max_request_input_tokens: int | None = Field(
        default=None, strict=True, ge=1, le=10000000
    )
    long_input_multiplier: Decimal = Field(default=Decimal(1), ge=1, le=10)
    long_output_multiplier: Decimal = Field(default=Decimal(1), ge=1, le=10)
    source_url: str = Field(min_length=1, max_length=2000)
    verified_at: datetime
    notes: str = Field(default="", max_length=2000)

    @field_validator("model")
    @classmethod
    def reference(cls, value):
        from .config import model_reference, resolve_model

        return model_reference(resolve_model(value))

    @field_validator("verified_at")
    @classmethod
    def timezone_required(cls, value):
        if not value.tzinfo or value > datetime.now(timezone.utc):
            raise ValueError("核对时间需要包含时区且不能位于未来")
        return value

    @field_validator("source_url")
    @classmethod
    def source(cls, value):
        from urllib.parse import urlsplit

        p = urlsplit(value)
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            raise ValueError("请填写不含凭据的 HTTPS 计价来源")
        return value


class PricingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: Literal["USD", "CNY", "EUR"] = "USD"
    effective_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    models: list[ModelRate] = Field(default_factory=list, max_length=100)

    @field_validator("effective_at")
    @classmethod
    def zoned(cls, value):
        if not value.tzinfo:
            raise ValueError("生效时间需要包含时区")
        return value

    @field_validator("models")
    @classmethod
    def unique(cls, value):
        if len(set(row.model for row in value)) != len(value):
            raise ValueError("同一模型只能登记一份单价")
        return value


def quote(c, reference, now=None):
    now = now or datetime.now(timezone.utc)
    row = one(
        c,
        "SELECT body,published_version FROM lead.admin_configs WHERE config_key='pricing/default'",
    )
    if not row or not row["body"]:
        from .config import STATE

        try:
            legacy = json.loads((STATE / "operator-rates.json").read_text())
            rate = legacy["models"][reference]
            fields = {
                name: str(Decimal(str(rate[name])))
                for name in (
                    "input_per_million",
                    "cached_input_per_million",
                    "output_per_million",
                )
            }
            if (
                any(
                    not Decimal(value).is_finite() or Decimal(value) < 0
                    for value in fields.values()
                )
                or not legacy["version"]
                or not legacy["currency"]
                or datetime.fromisoformat(legacy["effective_at"]).astimezone(
                    timezone.utc
                )
                > now
            ):
                raise ValueError("invalid legacy rates")
            return {
                "status": "priced",
                "version": legacy["version"],
                "currency": legacy["currency"],
                "effective_at": legacy["effective_at"],
                "rate": {
                    "model": reference,
                    **fields,
                    "max_request_input_tokens": None,
                    "long_input_multiplier": "1",
                    "long_output_multiplier": "1",
                    "source_url": "operator-rates.json",
                    "notes": "既有部署计价文件，在调用开始时保存快照。",
                },
            }
        except (OSError, ValueError, KeyError, TypeError, InvalidOperation):
            return {"status": "unpriced", "reason": "no_published_rates"}
    body = row["body"]
    version = "pricing/default@" + str(row["published_version"])
    if datetime.fromisoformat(body["effective_at"]) > now:
        # Scheduling a new price must not make today's published price disappear.
        prior = one(
            c,
            "SELECT after_state->'body' AS body,version AS published_version FROM lead.admin_config_versions WHERE config_key='pricing/default' AND action IN ('publish','rollback') AND (after_state->'body'->>'effective_at')::timestamptz<=%s ORDER BY version DESC LIMIT 1",
            (now,),
        )
        if not prior:
            return {"status": "unpriced", "reason": "not_effective_yet"}
        body = prior["body"]
        version = "pricing/default@" + str(prior["published_version"])
    rate = next(
        (entry for entry in body["models"] if entry["model"] == reference), None
    )
    if not rate:
        return {"status": "unpriced", "reason": "model_not_priced", "version": version}
    return {
        "status": "priced",
        "version": version,
        "currency": body["currency"],
        "effective_at": body["effective_at"],
        "rate": rate,
    }


def calculate(details, snapshot):
    result = {"cost": None}
    if snapshot.get("status") != "priced":
        return {**result, "pricing_status": snapshot.get("reason", "unpriced")}
    rate = snapshot["rate"]
    result.update(
        currency=snapshot["currency"],
        rate_version=snapshot["version"],
        rate_effective_at=snapshot["effective_at"],
        rate_snapshot=rate,
    )
    if (
        details.get("unmeasured_requests")
        or details.get("unpriced_service_tier_requests")
        or details.get("unmeasured_cache_write_requests")
    ):
        return {**result, "pricing_status": "unmeasured_or_service_tier"}
    input_tokens = details.get("input_tokens")
    output = details.get("output_tokens")
    hit = details.get("cache_read_tokens", 0)
    write = details.get("cache_write_tokens") or 0
    if (
        not details.get("cache_breakdown_verified")
        or any(
            not isinstance(n, int) or isinstance(n, bool) or n < 0
            for n in [input_tokens, output, hit, write]
        )
        or hit + write > input_tokens
    ):
        return {**result, "pricing_status": "missing_verified_breakdown"}
    # SDK reports totals across requests. For a long-context tariff, only price
    # aggregates certainly below its threshold or a single measured request.
    threshold = rate.get("max_request_input_tokens")
    if threshold and input_tokens > threshold and details.get("requests") != 1:
        return {**result, "pricing_status": "request_context_breakdown_required"}
    if hit and rate["cached_input_per_million"] is None:
        return {**result, "pricing_status": "cache_rate_unknown"}
    write_30m = details.get("cache_write_30m_tokens") or 0
    write_12h = details.get("cache_write_12h_tokens") or 0
    if (
        (write and not details.get("cache_write_breakdown_verified"))
        or any(
            not isinstance(n, int) or isinstance(n, bool) or n < 0
            for n in [write_30m, write_12h]
        )
        or write_30m + write_12h != write
    ):
        return {**result, "pricing_status": "cache_write_lifetime_unknown"}
    if (write_30m and rate.get("cache_write_30m_per_million") is None) or (
        write_12h and rate.get("cache_write_12h_per_million") is None
    ):
        return {**result, "pricing_status": "cache_write_rate_unknown"}
    if details.get("service_tier") not in (None, "default", "standard", "auto"):
        return {**result, "pricing_status": "service_tier_not_priced"}
    long = bool(threshold and input_tokens > threshold)
    input_multiplier = (
        Decimal(str(rate["long_input_multiplier"])) if long else Decimal(1)
    )
    output_multiplier = (
        Decimal(str(rate["long_output_multiplier"])) if long else Decimal(1)
    )
    amount = (
        (input_tokens - hit - write)
        * Decimal(rate["input_per_million"])
        * input_multiplier
        + hit * Decimal(rate["cached_input_per_million"] or "0") * input_multiplier
        + write_30m
        * Decimal(rate.get("cache_write_30m_per_million") or "0")
        * input_multiplier
        + write_12h
        * Decimal(rate.get("cache_write_12h_per_million") or "0")
        * input_multiplier
        + output * Decimal(rate["output_per_million"]) * output_multiplier
    ) / Decimal(1000000)
    return {
        **result,
        "cost": str(amount),
        "currency": snapshot["currency"],
        "rate_version": snapshot["version"],
        "rate_effective_at": snapshot["effective_at"],
        "rate_snapshot": rate,
        "pricing_status": "estimated",
    }
