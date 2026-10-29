"""Server-reported conversions: distinct from public browser analytics keys."""

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from .db import audit, one, require_agent, tx
from .scope import current_tenant

router = APIRouter()


class KeyRequest(BaseModel):
    connection_id: UUID


class Ping(BaseModel):
    connection_id: UUID


class Conversion(Ping):
    event_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_.-]+$")
    type: Literal["lead", "signup", "trial", "purchase"]
    session_id: UUID
    ref: str = Field(pattern=r"^[A-Za-z0-9_-]{16}$")
    occurred_at: datetime | None = None


@router.post("/api/agents/{aid}/analytics/server-key")
def issue(aid: UUID, body: KeyRequest):
    aid = str(aid)
    key = "lead_cv_" + secrets.token_urlsafe(32)
    with tx(aid) as c:
        require_agent(c, aid, True)
        connection = one(
            c,
            "SELECT id FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (body.connection_id,),
        )
        if not connection:
            raise LookupError("网站连接不存在")
        # Rotating a server-write key does not affect the public website snippet.
        c.execute(
            "UPDATE lead.analytics_credentials SET state='revoked',revoked_at=now() WHERE connection_id=%s AND state='active'",
            (body.connection_id,),
        )
        credential = one(
            c,
            "INSERT INTO lead.analytics_credentials(tenant_id,agent_id,connection_id,secret_sha256) VALUES(%s,%s,%s,%s) RETURNING id",
            (
                current_tenant(),
                aid,
                body.connection_id,
                hashlib.sha256(key.encode()).hexdigest(),
            ),
        )
        audit(
            c,
            aid,
            "analytics.server_key.issued",
            {
                "credential_id": str(credential["id"]),
                "connection_id": str(body.connection_id),
            },
        )
    return {
        "key": key,
        "credential_id": credential["id"],
        "connection_id": body.connection_id,
    }


def authenticate(request, connection_id):
    if request.headers.get("origin"):
        raise HTTPException(403, "服务端接口不接受网页脚本来源")
    authorization = request.headers.get("authorization", "")
    if not re.fullmatch(r"Bearer lead_cv_[A-Za-z0-9_-]{43}", authorization):
        raise HTTPException(401, "回传凭据无效")
    digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
    from .identity_store import public_tenant

    workspace = public_tenant("conversion", digest)
    if not workspace:
        raise HTTPException(401, "回传凭据无效")
    with tx(tenant_id=workspace) as c:
        credential = one(
            c,
            "SELECT k.id,k.tenant_id,k.agent_id,k.connection_id FROM lead.analytics_credentials k JOIN lead.analytics_connections n ON n.id=k.connection_id JOIN lead.agents a ON a.id=k.agent_id WHERE k.connection_id=%s AND k.secret_sha256=%s AND k.state='active' AND n.state<>'disconnected' AND a.desired_state<>'archived'",
            (connection_id, digest),
        )
    if not credential:
        raise HTTPException(401, "回传凭据无效")
    return credential


@router.post("/collect/server/ping")
def ping(body: Ping, request: Request):
    credential = authenticate(request, body.connection_id)
    with tx(str(credential["agent_id"]), tenant_id=str(credential["tenant_id"])) as c:
        c.execute(
            "UPDATE lead.analytics_credentials SET last_received_at=now() WHERE id=%s",
            (credential["id"],),
        )
    return {"ok": True}


@router.post("/collect/server/conversion")
def receive(body: Conversion, request: Request):
    credential = authenticate(request, body.connection_id)
    aid = str(credential["agent_id"])
    now = datetime.now(timezone.utc)
    occurred = body.occurred_at or now
    if occurred.tzinfo is None:
        raise HTTPException(422, "事件时间必须包含时区")
    if occurred > now + timedelta(minutes=5) or occurred < now - timedelta(days=30):
        raise HTTPException(422, "事件时间超出当前归因窗口")
    fingerprint = hashlib.sha256(
        json.dumps(body.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    with tx(aid, tenant_id=str(credential["tenant_id"])) as c:
        existing = one(
            c,
            "SELECT id,payload_sha256 FROM lead.conversion_events WHERE connection_id=%s AND source_event_id=%s",
            (body.connection_id, body.event_id),
        )
        if existing:
            if existing["payload_sha256"] != fingerprint:
                raise HTTPException(409, "事件标识已用于不同内容")
            return {"ok": True, "duplicate": True, "id": existing["id"]}
        visit = one(
            c,
            "SELECT v.id FROM lead.visit_sessions v JOIN lead.tracking_links l ON l.id=v.tracking_link_id JOIN lead.analytics_connections n ON n.id=%s WHERE v.source_session_id=%s AND v.source=%s AND v.trust_level='client_observed' AND l.public_code=%s AND l.state='active' AND v.started_at>=now()-interval '30 days'",
            (
                body.connection_id,
                str(body.session_id),
                "site:" + str(body.connection_id),
                body.ref,
            ),
        )
        if not visit:
            raise HTTPException(409, "未找到此网站连接下可归因的访问")
        event = one(
            c,
            "INSERT INTO lead.conversion_events(tenant_id,agent_id,connection_id,visit_session_id,source_event_id,type,trust_level,rule_version,occurred_at,payload_sha256) VALUES(%s,%s,%s,%s,%s,%s,'server_verified','server-attributed-v1',%s,%s) ON CONFLICT(tenant_id,connection_id,source_event_id) DO NOTHING RETURNING id",
            (
                current_tenant(),
                aid,
                body.connection_id,
                visit["id"],
                body.event_id,
                body.type,
                occurred,
                fingerprint,
            ),
        )
        if not event:
            previous = one(
                c,
                "SELECT id,payload_sha256 FROM lead.conversion_events WHERE connection_id=%s AND source_event_id=%s",
                (body.connection_id, body.event_id),
            )
            if previous["payload_sha256"] != fingerprint:
                raise HTTPException(409, "事件标识已用于不同内容")
            return {"ok": True, "duplicate": True, "id": previous["id"]}
        c.execute(
            "UPDATE lead.analytics_credentials SET last_received_at=now() WHERE id=%s",
            (credential["id"],),
        )
    return {"ok": True, "duplicate": False, "id": event["id"]}
