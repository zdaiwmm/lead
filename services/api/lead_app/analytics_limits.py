"""Per-Agent token buckets and optional daily intake quotas, shared across workers."""

import hashlib
import hmac
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import CONFIG
from .db import all_rows, audit, one, require_agent
from .db import pooled_tx as tx
from .scope import current_tenant

router = APIRouter()
TZ = ZoneInfo("Asia/Shanghai")


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events_per_minute: int = Field(default=6000, strict=True, ge=1, le=1000000)
    event_burst: int = Field(default=200, strict=True, ge=20, le=100000)
    requests_per_minute: int = Field(default=30000, strict=True, ge=1, le=1000000)
    request_burst: int = Field(default=500, strict=True, ge=1, le=100000)
    ip_events_per_minute: int = Field(default=600, strict=True, ge=0, le=1000000)
    ip_event_burst: int = Field(default=40, strict=True, ge=20, le=100000)
    daily_events: int = Field(default=0, strict=True, ge=0, le=1000000000000)
    redirect_per_minute: int = Field(default=6000, strict=True, ge=1, le=1000000)
    redirect_burst: int = Field(default=200, strict=True, ge=1, le=100000)
    network_days: int = Field(default=7, strict=True, ge=1, le=30)
    payload_days: int = Field(default=30, strict=True, ge=1, le=90)
    clean_days: int = Field(default=90, strict=True, ge=30, le=365)
    backlog_seconds: int = Field(default=300, strict=True, ge=10, le=86400)
    delivery_failure_threshold: int = Field(default=5, strict=True, ge=1, le=1000)

    @model_validator(mode="after")
    def retention_order(self):
        if not self.network_days <= self.payload_days <= self.clean_days:
            raise ValueError("保留期必须满足 IP/UA ≤ 原始事件 ≤ 清洗明细")
        return self


class PolicyUpdate(Policy):
    expected_revision: int = Field(default=0, strict=True, ge=0)


def policy(c):
    row = one(
        c, "SELECT * FROM lead.analytics_policies WHERE agent_id=lead.ctx_agent()"
    )
    return (
        (
            Policy.model_validate({key: row[key] for key in Policy.model_fields}),
            row["revision"],
        )
        if row
        else (Policy(), 0)
    )


def guard(
    agent,
    tenant,
    environment,
    *,
    events=1,
    client_ip=None,
    server=False,
    redirect=False,
):
    """Budgets count ingress attempts, including retries; they are not billing.

    A denial commits its counters before 429 is raised. Redirect denials are
    returned to the navigator so the destination can still be reached.
    """
    now = datetime.now(timezone.utc)
    day = now.astimezone(TZ).date()
    with tx(str(agent), tenant_id=str(tenant)) as c:
        require_agent(c, str(agent))
        cfg, _ = policy(c)
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            (f"analytics-rate:{tenant}:{agent}:{environment}",),
        )
        now = datetime.now(timezone.utc)
        day = now.astimezone(TZ).date()
        c.execute(
            "INSERT INTO lead.analytics_limit_counters(tenant_id,agent_id,environment,day) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (tenant, agent, environment, day),
        )
        counter = one(
            c,
            "SELECT * FROM lead.analytics_limit_counters WHERE environment=%s AND day=%s FOR UPDATE",
            (environment, day),
        )
        rules = (
            [("redirect", "agent", cfg.redirect_per_minute, cfg.redirect_burst, 1)]
            if redirect
            else [
                ("request", "agent", cfg.requests_per_minute, cfg.request_burst, 1),
                ("event", "agent", cfg.events_per_minute, cfg.event_burst, events),
            ]
        )
        if (
            not redirect
            and events
            and client_ip
            and not server
            and cfg.ip_events_per_minute
        ):
            actor = hmac.new(
                CONFIG["session_secret"].encode(),
                f"analytics-ip:{tenant}:{agent}:{client_ip}".encode(),
                hashlib.sha256,
            ).hexdigest()
            rules.append(
                (
                    "ip_event",
                    actor,
                    cfg.ip_events_per_minute,
                    cfg.ip_event_burst,
                    events,
                )
            )
        balances = []
        retry = 0
        for kind, actor, rate, burst, cost in rules:
            c.execute(
                """INSERT INTO lead.analytics_limit_buckets(tenant_id,agent_id,environment,kind,actor_key,tokens,updated_at,expires_at)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    tenant,
                    agent,
                    environment,
                    kind,
                    actor,
                    burst,
                    now,
                    now + timedelta(hours=1),
                ),
            )
            bucket = one(
                c,
                "SELECT * FROM lead.analytics_limit_buckets WHERE environment=%s AND kind=%s AND actor_key=%s FOR UPDATE",
                (environment, kind, actor),
            )
            elapsed = max(0, (now - bucket["updated_at"]).total_seconds())
            available = min(
                Decimal(burst),
                bucket["tokens"] + Decimal(str(elapsed)) * Decimal(rate) / 60,
            )
            if available < cost:
                retry = max(
                    retry, math.ceil(float((Decimal(cost) - available) * 60 / rate))
                )
            balances.append((kind, actor, available, cost))
        if (
            not redirect
            and events
            and cfg.daily_events
            and counter["granted_events"] + events > cfg.daily_events
        ):
            tomorrow = datetime.combine(
                day + timedelta(days=1), datetime.min.time(), TZ
            )
            retry = max(retry, math.ceil((tomorrow - now).total_seconds()))
        for kind, actor, available, cost in balances:
            c.execute(
                "UPDATE lead.analytics_limit_buckets SET tokens=%s,updated_at=%s,expires_at=%s WHERE environment=%s AND kind=%s AND actor_key=%s",
                (
                    available if retry else available - cost,
                    now,
                    now + timedelta(hours=1),
                    environment,
                    kind,
                    actor,
                ),
            )
        if retry:
            c.execute(
                """UPDATE lead.analytics_limit_counters SET denied_requests=denied_requests+1,
              denied_events=denied_events+%s,skipped_redirects=skipped_redirects+%s,last_denied_at=%s WHERE environment=%s AND day=%s""",
                (events, int(redirect), now, environment, day),
            )
        else:
            c.execute(
                "UPDATE lead.analytics_limit_counters SET granted_requests=granted_requests+1,granted_events=granted_events+%s WHERE environment=%s AND day=%s",
                (0 if redirect else events, environment, day),
            )
    return max(1, retry) if retry else 0


def enforce(conn, request, *, events=1, environment="production", server=False):
    from .analytics_ingest import request_network

    retry = guard(
        conn["agent_id"],
        conn["tenant_id"],
        environment,
        events=events,
        client_ip=request_network(request)["client_ip"],
        server=server,
    )
    if retry:
        headers = {"Retry-After": str(retry)}
        if not server:
            headers.update(
                {
                    "Access-Control-Allow-Origin": conn["site_host"],
                    "Vary": "Origin",
                    "Access-Control-Expose-Headers": "Retry-After",
                }
            )
        raise HTTPException(
            429,
            {
                "code": "ANALYTICS_RATE_LIMITED",
                "retry_after": retry,
                "message": "当前 Agent 的采集额度已达到限制，请稍后重试",
            },
            headers=headers,
        )


@router.get("/api/agents/{aid}/analytics/policy")
def get_policy(aid: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        cfg, revision = policy(c)
        counters = all_rows(
            c,
            "SELECT * FROM lead.analytics_limit_counters WHERE day=(now() AT TIME ZONE 'Asia/Shanghai')::date",
        )
    return {
        "agent_id": str(aid),
        "policy": cfg.model_dump(),
        "revision": revision,
        "counters": counters,
    }


@router.post("/api/agents/{aid}/analytics/policy")
def save_policy(aid: UUID, body: PolicyUpdate):
    from psycopg import sql

    from .admin_permissions import require

    require("analytics.manage")
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        _, revision = policy(c)
        if revision != body.expected_revision:
            raise HTTPException(409, "采集设置已更新，请重新读取")
        values = body.model_dump(exclude={"expected_revision"})
        cols = list(values)
        c.execute(
            sql.SQL(
                "INSERT INTO lead.analytics_policies(tenant_id,agent_id,{cols}) VALUES(%s,%s,{marks}) ON CONFLICT(tenant_id,agent_id) DO UPDATE SET {updates},revision=lead.analytics_policies.revision+1,updated_at=now()"
            ).format(
                cols=sql.SQL(",").join(map(sql.Identifier, cols)),
                marks=sql.SQL(",").join(sql.Placeholder() for _ in cols),
                updates=sql.SQL(",").join(
                    sql.SQL("{0}=excluded.{0}").format(sql.Identifier(k)) for k in cols
                ),
            ),
            (current_tenant(), str(aid), *values.values()),
        )
        audit(c, str(aid), "analytics.policy.updated", {"revision": revision + 1})
    return get_policy(aid)
