"""Scoped website events, source snapshots, identity and real test receipts."""

import hashlib
import json
import math
import re
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from .db import all_rows, audit, j, one, require_agent, tx
from .pixel_catalog import catalog
from .scope import current_tenant

router = APIRouter()
NAME = r"^[A-Za-z][A-Za-z0-9_]{0,63}$"
EVENT_ID = r"^[A-Za-z0-9:_.-]{1,128}$"
RESERVED = {"pageview", "engagement", "identify"}
PROPERTIES = {
    "order_id",
    "value",
    "currency",
    "action",
    "rule_version",
    "content_id",
    "form_id",
    "trial_id",
    "subscription_id",
    "channel",
    "search_string",
    "quantity",
    "payment_method",
    "location_id",
    "appointment_id",
    "application_id",
}


@router.get("/api/agents/{aid}/analytics/report")
def report(aid: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        conn = one(
            c,
            """SELECT * FROM lead.analytics_connections WHERE provider='lead_site'
            AND state<>'disconnected' ORDER BY selected_at DESC,created_at DESC LIMIT 1""",
        )
        pixel = configuration(c, conn)
    return {
        "agent_id": str(aid),
        "connection": {
            k: conn[k] for k in ("id", "site_host", "state", "last_received_at")
        }
        if conn
        else None,
        "pixel": pixel,
    }


class DefinitionInput(BaseModel):
    connection_id: UUID
    name: str = Field(pattern=NAME)
    label: str = Field(min_length=1, max_length=64)
    method: Literal["browser", "server"] = "browser"
    definition: str = Field(default="", max_length=200)
    expected_revision: int = Field(default=0, ge=0)


class ConnectionInput(BaseModel):
    connection_id: UUID


class CompleteInput(ConnectionInput):
    visits_only: bool = False


class TestInput(BaseModel):
    connection_id: UUID | None = None
    definition_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)


class AttributionSnapshot(BaseModel):
    connection_id: UUID
    anonymous_id: UUID | None = None
    session_id: UUID | None = None
    touchpoint_id: UUID | None = None
    test_code: str | None = Field(default=None, max_length=100)


class TestControl(BaseModel):
    key: str = Field(max_length=500)
    url: str = Field(max_length=3000)
    test_code: str = Field(min_length=1, max_length=100)
    action: Literal["status", "end"] = "status"


class PixelEvent(BaseModel):
    event_id: str = Field(pattern=EVENT_ID)
    event_name: str = Field(pattern=NAME)
    anonymous_id: UUID | None = None
    session_id: UUID | None = None
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    occurred_at: datetime | None = None
    properties: dict = Field(default_factory=dict)
    touchpoint_id: UUID | None = None
    test_code: str | None = Field(default=None, max_length=100)

    @field_validator("properties")
    @classmethod
    def properties_only(cls, value):
        if set(value) - PROPERTIES or len(json.dumps(value, ensure_ascii=False)) > 4000:
            raise ValueError("事件字段不支持或内容过长")
        if any(
            not isinstance(v, (str, int, float, bool, type(None)))
            for v in value.values()
        ):
            raise ValueError("事件字段必须为简单值")
        if any(isinstance(v, float) and not math.isfinite(v) for v in value.values()):
            raise ValueError("事件字段数值无效")
        return value


class BrowserEvent(PixelEvent):
    key: str = Field(max_length=500)
    url: str = Field(max_length=3000)
    ref: str | None = Field(default=None, max_length=100)


class ServerEvent(PixelEvent):
    connection_id: UUID
    lead_attribution: AttributionSnapshot | None = None

    @model_validator(mode="after")
    def apply_snapshot(self):
        source = self.lead_attribution
        if source is None:
            return self
        if source.connection_id != self.connection_id:
            raise ValueError("归因快照不属于此网站连接")
        for name in ("anonymous_id", "session_id", "touchpoint_id", "test_code"):
            value, saved = getattr(self, name), getattr(source, name)
            if value is not None and saved is not None and value != saved:
                raise ValueError("回传字段与订单保存的归因快照不一致")
            if value is None:
                setattr(self, name, saved)
        return self


def seed(c, conn):
    for item in catalog()[:3]:
        c.execute(
            """INSERT INTO lead.pixel_definitions(tenant_id,agent_id,connection_id,name,label,method,definition)
               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(connection_id,name) DO NOTHING""",
            (
                current_tenant(),
                conn["agent_id"],
                conn["id"],
                item["name"],
                item["label"],
                "server" if item["name"] == "Purchase" else "browser",
                "首次成功完成核心行为" if item["name"] == "Activation" else "",
            ),
        )


def configuration(c, conn):
    if not conn:
        return {
            "catalog": catalog(),
            "events": [],
            "installed": False,
            "completed": False,
            "embedded": False,
            "report": {
                "visitors": None,
                "sessions": None,
                "event_count": None,
                "events": [],
            },
        }
    definitions = all_rows(
        c,
        """
        SELECT d.*, EXISTS(SELECT 1 FROM lead.pixel_receipts p WHERE p.connection_id=d.connection_id
          AND p.scope_key<>'live' AND p.event_name=d.name
          AND p.definition_revision=d.revision AND p.counted) AS tested
        FROM lead.pixel_definitions d WHERE connection_id=%s ORDER BY
          CASE name WHEN 'CompleteRegistration' THEN 1 WHEN 'Activation' THEN 2 WHEN 'Purchase' THEN 3 ELSE 4 END,created_at,id
    """,
        (conn["id"],),
    )
    rows = all_rows(
        c,
        """
        WITH events AS (
          SELECT event_name,user_key,anonymous_id::text AS anonymous_id,occurred_at
          FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key='live' AND counted
            AND event_name NOT IN ('PageView','Engagement','Identify') AND occurred_at>now()-interval '30 days'
          UNION ALL
          SELECT CASE type WHEN 'signup' THEN 'CompleteRegistration' WHEN 'lead' THEN 'Lead'
             WHEN 'trial' THEN 'StartTrial' WHEN 'purchase' THEN 'Purchase' ELSE type END,
             NULL,NULL,occurred_at FROM lead.conversion_events legacy
          WHERE connection_id=%s AND occurred_at>now()-interval '30 days'
            AND NOT EXISTS(SELECT 1 FROM lead.pixel_receipts p WHERE p.connection_id=legacy.connection_id
              AND p.scope_key='live' AND p.event_id=legacy.source_event_id)
        )
        SELECT event_name,count(*) AS count,
          CASE WHEN count(*) FILTER(WHERE user_key IS NULL AND anonymous_id IS NULL)>0 THEN NULL
            ELSE count(DISTINCT coalesce(user_key,'anon:'||anonymous_id)) END AS users,
          max(occurred_at) AS last_occurred_at FROM events GROUP BY event_name
    """,
        (conn["id"], conn["id"]),
    )
    by_name = {r["event_name"]: r for r in rows}
    report = []
    for d in definitions:
        r = by_name.pop(d["name"], None)
        report.append(
            {
                **(r or {}),
                "name": d["name"],
                "label": d["label"],
                "count": r["count"] if r else None,
                "users": r["users"] if r else None,
                "last_occurred_at": r["last_occurred_at"] if r else None,
                "method": d["method"],
                "tested": d["tested"],
            }
        )
    for name, row in by_name.items():
        report.append(
            {
                **row,
                "name": name,
                "label": next(
                    (e["label"] for e in catalog() if e["name"] == name), name
                ),
                "tested": False,
                "method": "server",
            }
        )
    traffic = one(
        c,
        """
        SELECT count(*) AS sessions,count(DISTINCT anonymous_id) AS visitors,
          count(*) FILTER(WHERE anonymous_id IS NULL) AS unknown_visitors
        FROM lead.visit_sessions WHERE source=%s AND started_at>now()-interval '30 days'
    """,
        ("site:" + str(conn["id"]),),
    )
    from .admin_controls import attribution

    policy, _ = attribution(c, str(conn["agent_id"]))
    last = conn.get("last_pageview_at") or conn.get("last_received_at")
    installed = bool(
        conn["state"] == "verified"
        and last
        and last
        > datetime.now(timezone.utc) - timedelta(hours=policy["freshness_hours"])
    )
    return {
        "catalog": catalog(),
        "events": definitions,
        "installed": installed,
        "embedded": bool(conn.get("embedded_at")),
        "completed": bool(conn.get("onboarding_completed_at")),
        "report": {
            "visitors": traffic["visitors"]
            if installed and not traffic["unknown_visitors"]
            else None,
            "sessions": traffic["sessions"] if installed else None,
            "event_count": sum(r["count"] for r in rows) if rows else None,
            "events": report,
        },
    }


@router.post("/api/agents/{aid}/analytics/published")
def published(aid: UUID, body: ConnectionInput):
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        if not one(
            c,
            "UPDATE lead.analytics_connections SET embedded_at=now() WHERE id=%s AND state<>'disconnected' RETURNING id",
            (body.connection_id,),
        ):
            raise LookupError("网站连接不存在")
    return {"ok": True}


@router.post("/api/agents/{aid}/analytics/complete")
def complete(aid: UUID, body: CompleteInput):
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (body.connection_id,),
        )
        if not conn:
            raise LookupError("网站连接不存在")
        status = configuration(c, conn)
        if not status["installed"]:
            raise HTTPException(409, "请先完成像素安装检测")
        if not body.visits_only and not any(e["tested"] for e in status["events"]):
            raise HTTPException(409, "请先完成至少一个业务事件测试")
        c.execute(
            "UPDATE lead.analytics_connections SET onboarding_completed_at=now() WHERE id=%s",
            (body.connection_id,),
        )
        audit(
            c, str(aid), "pixel.onboarding.completed", {"visits_only": body.visits_only}
        )
    return {"ok": True}


@router.post("/api/agents/{aid}/analytics/events/configure")
def configure(aid: UUID, body: DefinitionInput):
    name, label = body.name.strip(), body.label.strip()
    if name.lower() in RESERVED or not label:
        raise HTTPException(422, "事件名称不可用")
    standard = next((e for e in catalog() if e["name"].lower() == name.lower()), None)
    if standard and standard["name"] != name:
        raise HTTPException(422, "请使用标准事件的准确标识")
    if (name == "Activation" or not standard) and not body.definition.strip():
        raise HTTPException(422, "请填写触发行为")
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        if not one(
            c,
            "SELECT id FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (body.connection_id,),
        ):
            raise LookupError("网站连接不存在")
        previous = one(
            c,
            "SELECT * FROM lead.pixel_definitions WHERE connection_id=%s AND lower(name)=lower(%s) FOR UPDATE",
            (body.connection_id, name),
        )
        if (previous["revision"] if previous else 0) != body.expected_revision:
            raise HTTPException(409, "事件设置已更新，请重新读取")
        values = (label, body.method, body.definition.strip())
        if previous:
            if (
                previous["label"],
                previous["method"],
                previous["definition"],
            ) != values:
                previous = one(
                    c,
                    """UPDATE lead.pixel_definitions SET label=%s,method=%s,definition=%s,
                    revision=revision+1,updated_at=now() WHERE id=%s RETURNING *""",
                    (*values, previous["id"]),
                )
            result = previous
        else:
            if (
                one(
                    c,
                    "SELECT count(*) AS n FROM lead.pixel_definitions WHERE connection_id=%s",
                    (body.connection_id,),
                )["n"]
                >= 100
            ):
                raise HTTPException(409, "每个网站最多配置 100 个事件")
            result = one(
                c,
                """INSERT INTO lead.pixel_definitions(tenant_id,agent_id,connection_id,name,label,method,definition)
                VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                (current_tenant(), str(aid), body.connection_id, name, *values),
            )
        audit(
            c,
            str(aid),
            "pixel.event.configured",
            {"name": name, "revision": result["revision"]},
        )
    return result


@router.post("/api/agents/{aid}/analytics/events/test")
def start_test(aid: UUID, body: TestInput):
    code = "lt_" + secrets.token_urlsafe(24)
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        definition = None
        if body.definition_id:
            definition = one(
                c,
                "SELECT * FROM lead.pixel_definitions WHERE id=%s",
                (body.definition_id,),
            )
            if not definition:
                raise LookupError("事件不存在")
            if definition["revision"] != body.expected_revision:
                raise HTTPException(409, "事件设置已更新，请重新读取")
        cid = body.connection_id or (
            definition["connection_id"] if definition else None
        )
        if not cid or (definition and str(cid) != str(definition["connection_id"])):
            raise HTTPException(422, "请选择正确的网站连接")
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state='verified'",
            (cid,),
        )
        if not conn:
            raise HTTPException(409, "请先完成像素安装检测")
        versions = {
            d["name"]: d["revision"]
            for d in all_rows(
                c,
                "SELECT name,revision FROM lead.pixel_definitions WHERE connection_id=%s",
                (cid,),
            )
        }
        test = one(
            c,
            """INSERT INTO lead.pixel_tests(tenant_id,agent_id,connection_id,
            definition_id,definition_revision,definition_revisions,code_sha256)
            VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id,expires_at""",
            (
                current_tenant(),
                str(aid),
                cid,
                definition["id"] if definition else None,
                definition["revision"] if definition else None,
                j(versions),
                hashlib.sha256(code.encode()).hexdigest(),
            ),
        )
        audit(
            c,
            str(aid),
            "pixel.test.started",
            {"test_id": str(test["id"]), "connection_id": str(cid)},
        )
    return {
        **test,
        "test_code": code,
        "url": conn["site_host"] + "/?lead_test=" + code,
        "active": True,
        "state": "active",
        "events": [],
    }


def test_state(test):
    if test["ended_at"]:
        return "ended"
    return "expired" if test["expires_at"] <= datetime.now(timezone.utc) else "active"


@router.get("/api/agents/{aid}/analytics/events/test/{test_id}")
def test_result(aid: UUID, test_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid))
        result = one(c, "SELECT * FROM lead.pixel_tests WHERE id=%s", (test_id,))
        if not result:
            raise LookupError("测试不存在")
        versions = {
            d["name"]: d["revision"]
            for d in all_rows(
                c,
                "SELECT name,revision FROM lead.pixel_definitions WHERE connection_id=%s",
                (result["connection_id"],),
            )
        }
        events = all_rows(
            c,
            """SELECT id,event_name,received_at,trust_level,duplicate_of IS NOT NULL AS duplicate,
            definition_revision,'accepted' AS status,NULL::text AS reason FROM lead.pixel_receipts
            WHERE connection_id=%s AND scope_key=%s
            UNION ALL SELECT id,payload->>'event_name',occurred_at,'rejected',false,NULL,'rejected',payload->>'reason'
            FROM lead.audit_events WHERE type='pixel.test.rejected' AND payload->>'test_id'=%s
            ORDER BY received_at DESC,id DESC LIMIT 100""",
            (result["connection_id"], str(test_id), str(test_id)),
        )
        state = test_state(result)
        return {
            "id": result["id"],
            "expires_at": result["expires_at"],
            "ended_at": result["ended_at"],
            "received_at": result["received_at"],
            "state": state,
            "active": state == "active",
            "current": all(
                versions.get(k) == v for k, v in result["definition_revisions"].items()
            ),
            "events": events,
            "error": next(
                (e["reason"] for e in events if e["status"] == "rejected"), None
            ),
        }


@router.post("/api/agents/{aid}/analytics/events/test/{test_id}/end")
def end_test(aid: UUID, test_id: UUID):
    with tx(str(aid)) as c:
        require_agent(c, str(aid), True)
        test = one(
            c,
            "UPDATE lead.pixel_tests SET ended_at=coalesce(ended_at,now()) WHERE id=%s RETURNING id,ended_at",
            (test_id,),
        )
        if not test:
            raise LookupError("测试不存在")
        audit(c, str(aid), "pixel.test.ended", {"test_id": str(test_id)})
    return {**test, "active": False, "state": "ended"}


@router.post("/collect/{cid}/test")
def browser_test_control(cid: UUID, body: TestControl, request: Request):
    from .security import valid_analytics_key

    with tx() as c:
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (cid,),
        )
    p = urlsplit(body.url)
    if (
        not conn
        or not valid_analytics_key(body.key, cid)
        or request.headers.get("origin") != conn["site_host"]
        or p.scheme + "://" + p.netloc != conn["site_host"]
    ):
        raise HTTPException(403, "网站来源或安装码无效")
    with tx(str(conn["agent_id"])) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(cid),))
        test = one(
            c,
            "SELECT * FROM lead.pixel_tests WHERE connection_id=%s AND code_sha256=%s",
            (cid, hashlib.sha256(body.test_code.encode()).hexdigest()),
        )
        if not test:
            result = {"active": False, "state": "invalid"}
        else:
            if body.action == "end":
                c.execute(
                    "UPDATE lead.pixel_tests SET ended_at=coalesce(ended_at,now()) WHERE id=%s",
                    (test["id"],),
                )
                test["ended_at"] = test["ended_at"] or datetime.now(timezone.utc)
            state = test_state(test)
            result = {
                "id": str(test["id"]),
                "active": state == "active",
                "state": state,
                "expires_at": test["expires_at"],
            }
    return JSONResponse(
        jsonable_encoder(result),
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


def record_test_failure(conn, body, reason):
    """Persist fixed diagnostics, never identifiers, keys or raw event data."""
    if not body.test_code:
        return
    from .telemetry import trace_id

    with tx(str(conn["agent_id"]), tenant_id=str(conn["tenant_id"])) as c:
        test = one(
            c,
            "SELECT id FROM lead.pixel_tests WHERE connection_id=%s AND code_sha256=%s",
            (conn["id"], hashlib.sha256(body.test_code.encode()).hexdigest()),
        )
        if test:
            message = (
                reason.get("message", "测试会话不可用")
                if isinstance(reason, dict)
                else str(reason)
            )
            c.execute(
                """INSERT INTO lead.audit_events(tenant_id,agent_id,actor_type,actor_ref,type,object_ref,payload,trace_id)
                VALUES(%s,%s,'pixel','test-session','pixel.test.rejected',%s,%s,%s)""",
                (
                    current_tenant(),
                    conn["agent_id"],
                    str(test["id"]),
                    j(
                        {
                            "test_id": str(test["id"]),
                            "event_name": body.event_name,
                            "reason": message[:300],
                        }
                    ),
                    trace_id(),
                ),
            )


def test_scope(c, conn, body):
    if not body.test_code:
        return None
    test = one(
        c,
        "SELECT * FROM lead.pixel_tests WHERE connection_id=%s AND code_sha256=%s FOR SHARE",
        (conn["id"], hashlib.sha256(body.test_code.encode()).hexdigest()),
    )
    state = test_state(test) if test else "invalid"
    if state != "active":
        raise HTTPException(
            422,
            {
                "code": "pixel_test_" + state,
                "message": "测试会话已结束、过期或无效，请重新开始测试",
            },
        )
    return test


def user_key(cid, value):
    return (
        hashlib.sha256((str(cid) + ":" + value).encode()).hexdigest() if value else None
    )


def receive(c, conn, body, trust, visit=None, touch=None, test=None):
    now = datetime.now(timezone.utc)
    occurred = body.occurred_at or now
    if (
        occurred.tzinfo is None
        or occurred > now + timedelta(minutes=5)
        or occurred < now - timedelta(days=30)
    ):
        raise HTTPException(422, "事件发生时间必须包含时区且在近 30 天内")
    fingerprint = hashlib.sha256(
        json.dumps(
            body.model_dump(
                mode="json", exclude={"key", "url", "ref", "lead_attribution"}
            ),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    scope = str(test["id"]) if test else "live"
    old = one(
        c,
        "SELECT id,payload_sha256,duplicate_of FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key=%s AND event_id=%s",
        (conn["id"], scope, body.event_id),
    )
    if old:
        if old["payload_sha256"] != fingerprint:
            raise HTTPException(409, "事件编号已用于不同内容")
        return {"ok": True, "duplicate": True, "id": old["duplicate_of"] or old["id"]}
    definition = one(
        c,
        "SELECT * FROM lead.pixel_definitions WHERE connection_id=%s AND name=%s",
        (conn["id"], body.event_name),
    )
    basic = body.event_name in ("PageView", "Engagement", "Identify")
    if not basic and (
        not definition
        or (definition["method"] == "server" and trust != "server_verified")
    ):
        raise HTTPException(422, "事件尚未配置或必须由网站服务端上报")
    if (
        test
        and not basic
        and test["definition_revisions"].get(body.event_name) != definition["revision"]
    ):
        raise HTTPException(422, "事件设置已变更或不在此测试会话中，请开始新的测试会话")
    identity = user_key(conn["id"], body.user_id)
    if not identity and body.anonymous_id and not test:
        binding = one(
            c,
            "SELECT user_key FROM lead.pixel_identities WHERE connection_id=%s AND anonymous_id=%s",
            (conn["id"], body.anonymous_id),
        )
        identity = binding["user_key"] if binding else None
    if (
        body.event_name in ("CompleteRegistration", "Activation", "Identify")
        and not identity
    ):
        raise HTTPException(422, "此事件需要用户编号")
    properties = body.properties
    business = None
    if body.event_name == "CompleteRegistration":
        business = identity
    elif body.event_name == "Activation":
        if properties.get("rule_version") != f"activation-v{definition['revision']}":
            raise HTTPException(422, "有效使用规则版本不匹配")
        business = f"{identity}:v{definition['revision']}"
    elif body.event_name == "Purchase":
        if (
            not isinstance(properties.get("order_id"), str)
            or not properties["order_id"].strip()
            or len(properties["order_id"]) > 128
        ):
            raise HTTPException(422, "支付事件需要订单编号")
        value = properties.get("value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0 <= value <= 1e12
            or not re.fullmatch(r"[A-Z]{3}", str(properties.get("currency", "")))
        ):
            raise HTTPException(422, "支付事件需要有效金额与币种")
        business = properties["order_id"]
    if identity and body.anonymous_id and not test:
        c.execute(
            """INSERT INTO lead.pixel_identities(tenant_id,agent_id,connection_id,anonymous_id,user_key)
            VALUES(%s,%s,%s,%s,%s) ON CONFLICT(connection_id,anonymous_id)
            DO UPDATE SET user_key=excluded.user_key,identified_at=now()""",
            (
                current_tenant(),
                conn["agent_id"],
                conn["id"],
                body.anonymous_id,
                identity,
            ),
        )
        c.execute(
            """UPDATE lead.pixel_receipts SET user_key=%s WHERE connection_id=%s AND anonymous_id=%s
            AND user_key IS NULL AND scope_key='live'""",
            (identity, conn["id"], body.anonymous_id),
        )
    if body.touchpoint_id and not test:
        touch = one(
            c,
            """SELECT * FROM lead.pixel_touchpoints WHERE id=%s AND connection_id=%s""",
            (body.touchpoint_id, conn["id"]),
        )
        if not touch:
            raise HTTPException(422, "来源触点不属于此网站")
        if body.anonymous_id and body.anonymous_id != touch["anonymous_id"]:
            raise HTTPException(422, "来源触点与访客不匹配")
    if touch and not test:
        window = touch["attribution_policy"]["attribution_window_days"]
        if (
            not touch["occurred_at"]
            <= occurred
            <= touch["occurred_at"] + timedelta(days=window)
        ):
            touch = None
    prior = (
        one(
            c,
            """SELECT id,value,currency,user_key FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key=%s
        AND event_name=%s AND business_key=%s AND counted""",
            (conn["id"], scope, body.event_name, business),
        )
        if business
        else None
    )
    if prior and body.event_name == "Purchase":
        amount = Decimal(str(properties["value"])).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        if (
            prior["value"] != amount
            or prior["currency"] != properties["currency"]
            or (prior["user_key"] and identity and prior["user_key"] != identity)
        ):
            raise HTTPException(409, "订单编号已用于不同支付内容")
    row = one(
        c,
        """INSERT INTO lead.pixel_receipts(tenant_id,agent_id,connection_id,event_id,event_name,scope_key,
        anonymous_id,user_key,business_key,touchpoint_id,visit_session_id,definition_revision,trust_level,
        counted,duplicate_of,payload_sha256,occurred_at,value,currency)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
        (
            current_tenant(),
            conn["agent_id"],
            conn["id"],
            body.event_id,
            body.event_name,
            scope,
            body.anonymous_id,
            identity,
            business,
            touch["id"] if touch else None,
            visit["id"] if visit else touch["visit_session_id"] if touch else None,
            definition["revision"] if definition else None,
            trust,
            not bool(prior),
            prior["id"] if prior else None,
            fingerprint,
            occurred,
            properties.get("value") if body.event_name == "Purchase" else None,
            properties.get("currency") if body.event_name == "Purchase" else None,
        ),
    )
    if test and not basic:
        c.execute(
            "UPDATE lead.pixel_tests SET received_at=coalesce(received_at,now()) WHERE id=%s",
            (test["id"],),
        )
    return {
        "ok": True,
        "duplicate": bool(prior),
        "id": prior["id"] if prior else row["id"],
    }


def browser_receive(conn, body: BrowserEvent):
    if not body.anonymous_id or not body.session_id:
        raise HTTPException(422, "缺少匿名访客或会话编号")
    aid, cid = str(conn["agent_id"]), str(conn["id"])
    with tx(aid) as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (cid,))
        test = test_scope(c, conn, body)
        from .admin_controls import attribution

        policy, version = attribution(c, aid)
        source = "pixel-test:" + str(test["id"]) if test else "site:" + cid
        visit = one(
            c,
            "SELECT * FROM lead.visit_sessions WHERE source=%s AND source_session_id=%s",
            (source, str(body.session_id)),
        )
        link = (
            one(
                c,
                """SELECT id FROM lead.tracking_links WHERE public_code=%s AND state='active'
            AND agent_id=%s AND allowed_host=%s""",
                (body.ref, aid, urlsplit(conn["site_host"]).hostname),
            )
            if body.ref and not test
            else None
        )
        if not visit:
            visit = one(
                c,
                """INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,
                anonymous_id,trust_level,qualified,rule_version,started_at,last_seen_at,attribution_policy,attribution_version)
                VALUES(%s,%s,%s,%s,%s,%s,'client_observed',false,'engaged-v1',now(),now(),%s,%s) RETURNING *""",
                (
                    current_tenant(),
                    aid,
                    link["id"] if link else None,
                    source,
                    str(body.session_id),
                    body.anonymous_id,
                    j(policy),
                    version,
                ),
            )
        elif visit.get("anonymous_id") and visit["anonymous_id"] != body.anonymous_id:
            raise HTTPException(409, "会话与访客不匹配")
        touch = None
        if not test:
            touch = one(
                c,
                """SELECT * FROM lead.pixel_touchpoints WHERE connection_id=%s AND anonymous_id=%s
                AND occurred_at>=now()-make_interval(days=>%s) ORDER BY occurred_at DESC,id DESC LIMIT 1""",
                (cid, body.anonymous_id, policy["attribution_window_days"]),
            )
            if link and (
                not touch
                or touch["tracking_link_id"] != link["id"]
                or touch["visit_session_id"] != visit["id"]
            ):
                touch = one(
                    c,
                    """INSERT INTO lead.pixel_touchpoints(tenant_id,agent_id,connection_id,visit_session_id,
                    tracking_link_id,anonymous_id,attribution_policy,attribution_version,occurred_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,LEAST(%s,now())) RETURNING *""",
                    (
                        current_tenant(),
                        aid,
                        cid,
                        visit["id"],
                        link["id"],
                        body.anonymous_id,
                        j(policy),
                        version,
                        body.occurred_at or datetime.now(timezone.utc),
                    ),
                )
            # An explicit invalid marker must never borrow an earlier valid source.
            if body.ref and not link:
                touch = None
        result = receive(c, conn, body, "client_observed", visit, touch, test)
        if body.event_name == "PageView":
            c.execute(
                "UPDATE lead.analytics_connections SET state='verified',last_pageview_at=now(),last_received_at=now() WHERE id=%s",
                (cid,),
            )
        if body.event_name == "Engagement":
            elapsed = (datetime.now(timezone.utc) - visit["started_at"]).total_seconds()
            c.execute(
                "UPDATE lead.visit_sessions SET qualified=qualified OR %s,last_seen_at=now() WHERE id=%s",
                (
                    elapsed >= visit["attribution_policy"]["engagement_seconds"],
                    visit["id"],
                ),
            )
        context = {
            "connection_id": cid,
            "session_id": str(body.session_id),
            "anonymous_id": str(body.anonymous_id),
            "touchpoint_id": str(touch["id"]) if touch else None,
            "test_code": body.test_code if test else None,
        }
    return JSONResponse(
        jsonable_encoder(
            {
                **result,
                "attribution": context,
                "engagement_seconds": policy["engagement_seconds"],
            }
        ),
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


@router.post("/collect/server/events")
def server_receive(body: ServerEvent, request: Request):
    from .conversions import authenticate

    credential = authenticate(request, body.connection_id)
    try:
        return server_event(body, credential)
    except HTTPException as error:
        record_test_failure(
            {**credential, "id": body.connection_id}, body, error.detail
        )
        raise


def server_event(body, credential):
    with tx(str(credential["agent_id"]), tenant_id=str(credential["tenant_id"])) as c:
        cid = str(body.connection_id)
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (cid,))
        conn = one(c, "SELECT * FROM lead.analytics_connections WHERE id=%s", (cid,))
        test = test_scope(c, conn, body)
        result = receive(c, conn, body, "server_verified", test=test)
        if not test:
            c.execute(
                "UPDATE lead.analytics_credentials SET last_received_at=now() WHERE id=%s",
                (credential["id"],),
            )
    return result
