"""Opt-in Chamber shortlink registration and aggregate reporting."""

import asyncio
import re
from datetime import date, datetime, time, timedelta
from secrets import compare_digest
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import analytics, distribution
from .analytics_ingest import scoped_tx
from .analytics_reporting import TZ
from .config import env
from .db import one, tx
from .growth.promotion_dashboard import PromotionRange, read_promotion_dashboard
from .identity_store import public_tenant
from .promotion import CODE_PATTERN
from .scope import tenant_scope

router = APIRouter()
PREFIX = "LEAD_CHAMBER_ANALYTICS_"
ORIGIN = PREFIX + "ORIGIN"
LOCAL_AGENT = PREFIX + "LOCAL_AGENT_ID"
SITE_CONNECTION = PREFIX + "SITE_CONNECTION_ID"
READ_KEY = PREFIX + "READ_KEY"
LINK_KEY = PREFIX + "LINK_KEY"
REPORT_PATH = "/_chamber/analytics/v1/report"
LINKS_PATH = "/_chamber/analytics/v1/links"
SECRET_PATTERN = r"[A-Za-z0-9_-]{32,128}"
MAX_REPORT_BYTES = 64 * 1024
MAX_LINK_BYTES = 8 * 1024


class ChamberAnalyticsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Chamber 生产统计暂时不可用")


Count = Annotated[int, Field(strict=True, ge=0)]
Channel = Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]{0,39}$")]


class Counts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clicks: Count | None
    click_devices: Count | None
    pv: Count | None
    site_devices: Count | None


class ChannelCounts(Counts):
    channel: Channel


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    site: Annotated[bool, Field(strict=True)]


class DailyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: date
    status: Literal["ready", "pending", "unavailable"]
    coverage: Coverage
    totals: Counts
    channels: list[ChannelCounts]

    @model_validator(mode="after")
    def unique_channels(self):
        if len({row.channel for row in self.channels}) != len(self.channels):
            raise ValueError("Duplicate promotion channels")
        return self


class PlatformCounts(Counts):
    platform: Channel


class PromotionBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Annotated[str, Field(strict=True, max_length=35)]
    totals: Counts
    channels: list[PlatformCounts]


class RangedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
    range: PromotionRange
    pixel_connected: Annotated[bool, Field(strict=True)]
    history_complete: Annotated[bool, Field(strict=True)]
    totals: Counts
    channels: list[PlatformCounts]
    buckets: Annotated[list[PromotionBucket], Field(min_length=24, max_length=30)]

    @model_validator(mode="after")
    def matching_window(self):
        start = datetime.combine(date.fromisoformat(self.day), time.min, TZ)
        if self.range == "today":
            count, step = 24, timedelta(hours=1)
        else:
            count, step = 30, timedelta(days=1)
            try:
                start -= 29 * step
            except OverflowError:
                raise ValueError("Invalid promotion reporting window") from None
        if len(self.buckets) != count:
            raise ValueError("Unexpected promotion bucket count")

        platforms = {row.platform for row in self.channels}
        if len(platforms) != len(self.channels):
            raise ValueError("Duplicate promotion channels")
        for offset, bucket in enumerate(self.buckets):
            if datetime.fromisoformat(bucket.start) != start + offset * step:
                raise ValueError("Unexpected promotion bucket start")
            if len(bucket.channels) != len(platforms) or {row.platform for row in bucket.channels} != platforms:
                raise ValueError("Unexpected promotion bucket channels")
        return self


class LinkSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Annotated[str, Field(pattern=rf"^{CODE_PATTERN}$")]
    platform: Channel
    destination_url: Annotated[str, Field(min_length=1, max_length=2000)]


def _https_origin(value, *, origin_only=False):
    if (
        not value
        or "\\" in value
        or any(ord(char) <= 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("Invalid HTTPS origin")
    parsed = urlsplit(value)
    host, port = parsed.hostname, parsed.port
    if (
        parsed.scheme != "https"
        or not host
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or (
            origin_only
            and (parsed.path not in ("", "/") or parsed.query or parsed.fragment)
        )
    ):
        raise ValueError("Invalid HTTPS origin")
    return "https://" + host + (f":{port}" if port not in (None, 443) else "")


def _local_settings(aid):
    values = env()
    if not values.get(ORIGIN) and not values.get(LOCAL_AGENT):
        return None
    try:
        local_agent = UUID(values.get(LOCAL_AGENT) or "")
    except ValueError:
        raise ChamberAnalyticsUnavailable() from None
    return values if str(local_agent) == str(aid) else None


def production_bridge_enabled(aid):
    return _local_settings(aid) is not None


def _bearer_key(values, kind):
    key = values.get(kind) or ""
    other = values.get(LINK_KEY if kind == READ_KEY else READ_KEY) or ""
    if not re.fullmatch(SECRET_PATTERN, key) or key == other:
        raise ValueError("Invalid Chamber credential")
    return key


def _request(values, key, method, path, **kwargs):
    try:
        if values.get(SITE_CONNECTION):
            raise ValueError("Local and production bindings cannot coexist")
        origin = _https_origin(values.get(ORIGIN), origin_only=True)
        token = _bearer_key(values, key)
        with (
            httpx.Client(
                timeout=httpx.Timeout(5.0, connect=2.0),
                trust_env=False,
                follow_redirects=False,
            ) as client,
            client.stream(
                method,
                origin + path,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept-Encoding": "identity",
                },
                **kwargs,
            ) as response,
        ):
            if method == "POST" and response.status_code == 204:
                return b""
            if method == "GET" and response.status_code == 200:
                body = bytearray()
                for chunk in response.iter_raw():
                    if len(body) + len(chunk) > MAX_REPORT_BYTES:
                        raise ValueError("Report body exceeds limit")
                    body.extend(chunk)
                return bytes(body)
            raise ValueError("Unexpected Chamber response")
    except (httpx.HTTPError, ValueError):
        raise ChamberAnalyticsUnavailable() from None


def fetch_daily_report(aid, day):
    values = _local_settings(aid)
    if values is None:
        return None
    requested_day = str(day)
    payload = _request(
        values, READ_KEY, "GET", REPORT_PATH, params={"day": requested_day}
    )
    try:
        report = DailyReport.model_validate_json(payload)
        if report.day.isoformat() != requested_day:
            raise ValueError("Unexpected report date")
    except ValueError:
        raise ChamberAnalyticsUnavailable() from None
    return report.model_dump(mode="json")


def fetch_promotion_dashboard(aid, period: PromotionRange, day):
    values = _local_settings(aid)
    if values is None:
        return None
    requested_day = str(day)
    payload = _request(
        values, READ_KEY, "GET", REPORT_PATH, params={"day": requested_day, "range": period}
    )
    try:
        report = RangedReport.model_validate_json(payload)
        if report.day != requested_day or report.range != period:
            raise ValueError("Unexpected promotion reporting window")
    except ValueError:
        raise ChamberAnalyticsUnavailable() from None
    return report.model_dump(mode="json")


def register_production_link(aid, code, platform, destination):
    values = _local_settings(aid)
    if values is not None:
        _request(
            values,
            LINK_KEY,
            "POST",
            LINKS_PATH,
            json={"code": code, "platform": platform, "destination_url": destination},
        )


def _production_connection(values):
    raw = values.get(SITE_CONNECTION)
    if not raw:
        raise HTTPException(404)
    try:
        if values.get(ORIGIN) or values.get(LOCAL_AGENT):
            raise ValueError("Local and production bindings cannot coexist")
        return str(UUID(raw))
    except ValueError:
        raise HTTPException(503, "Chamber 生产采集配置不可用") from None


def _authorize(request, key):
    values = env()
    connection = _production_connection(values)
    try:
        expected = _bearer_key(values, key)
    except ValueError:
        raise HTTPException(503, "Chamber 生产采集配置不可用") from None
    headers = request.headers.getlist("authorization")
    scheme, _, token = headers[0].partition(" ") if len(headers) == 1 else ("", "", "")
    if (
        scheme.lower() != "bearer"
        or not token.isascii()
        or not compare_digest(token, expected)
    ):
        raise HTTPException(
            401, "Chamber 生产采集未授权", headers={"WWW-Authenticate": "Bearer"}
        )
    return connection


def _report_connection(request: Request):
    return _authorize(request, READ_KEY)


def _link_connection(request: Request):
    return _authorize(request, LINK_KEY)


def _site_owner(connection):
    tenant = public_tenant("analytics", connection)
    if not tenant:
        raise HTTPException(503, "Chamber 站点连接不可用")
    with tenant_scope(tenant, None), tx() as c:
        site = one(
            c,
            """SELECT agent_id, site_host FROM lead.analytics_connections
               WHERE id=%s AND provider='lead_site' AND state='verified'""",
            (connection,),
        )
    if not site:
        raise HTTPException(503, "Chamber 站点连接不可用")
    try:
        origin = _https_origin(site["site_host"], origin_only=True)
    except ValueError:
        raise HTTPException(503, "Chamber 站点连接不可用") from None
    return tenant, str(site["agent_id"]), origin


def _ensure_site_owner(c, connection, origin):
    site = one(
        c,
        """SELECT site.id FROM lead.analytics_connections site
           JOIN lead.agents agent ON (agent.tenant_id,agent.id)=(site.tenant_id,site.agent_id)
           WHERE site.id=%s AND site.provider='lead_site' AND site.state='verified'
             AND site.site_host=%s AND agent.desired_state<>'archived'
             AND NOT EXISTS (
               SELECT 1 FROM lead.analytics_connections
               WHERE provider='lead_site' AND site_host<>%s
             )""",
        (connection, origin, origin),
    )
    if not site:
        raise HTTPException(503, "Chamber 站点连接不可用")


def _ranged_response(data):
    def counts(row):
        return {field: row[field] for field in Counts.model_fields}

    def channels(rows):
        return [{"platform": row["platform"], **counts(row)} for row in rows]

    try:
        report = RangedReport(
            day=data["day"],
            range=data["range"],
            pixel_connected=data["pixel_connected"],
            history_complete=data["history_complete"],
            totals=counts(data["totals"]),
            channels=channels(data["channels"]),
            buckets=[
                {"start": bucket["start"], "totals": counts(bucket["totals"]), "channels": channels(bucket["channels"])}
                for bucket in data["buckets"]
            ],
        )
        body = report.model_dump_json().encode("utf-8")
        if len(body) > MAX_REPORT_BYTES:
            raise ValueError("Report body exceeds limit")
    except ValueError:
        raise HTTPException(503, "Chamber 生产统计暂时不可用") from None
    return Response(body, media_type="application/json")


@router.get(REPORT_PATH, response_model=DailyReport | RangedReport)
def production_daily_report(
    day: date,
    connection: Annotated[str, Depends(_report_connection)],
    period: Annotated[PromotionRange | None, Query(alias="range")] = None,
):
    from .analytics_reporting import read_daily_promotion

    now = datetime.now(TZ)
    if period is not None and day != now.date():
        raise HTTPException(422, "推广范围统计仅支持当前北京时间")
    tenant, aid, origin = _site_owner(connection)
    with tenant_scope(tenant, None), scoped_tx(tenant, aid, "live") as c:
        _ensure_site_owner(c, connection, origin)
        if period is None:
            return read_daily_promotion(c, aid, day)
        data = read_promotion_dashboard(c, aid, period, now, [])
    return _ranged_response(data)


def _register_source(connection, source):
    tenant, aid, origin = _site_owner(connection)
    try:
        if (
            source.platform in {"lab", "unattributed"}
            or _https_origin(source.destination_url) != origin
        ):
            raise ValueError("Invalid link source")
    except ValueError:
        raise HTTPException(422, "短链必须指向 Chamber 已验证的站点") from None
    try:
        with tenant_scope(tenant, None), scoped_tx(tenant, aid, "live") as c:
            _ensure_site_owner(c, connection, origin)
            distribution.register(
                c,
                aid,
                source.platform,
                source.destination_url,
                intent="chamber:" + source.code,
                placement="chamber",
                code=source.code,
            )
    except (UniqueViolation, ValueError):
        raise HTTPException(409, "短链编号已登记到其他推广来源") from None


@router.post(LINKS_PATH, status_code=204)
async def production_register_link(
    request: Request, connection: Annotated[str, Depends(_link_connection)]
):
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_LINK_BYTES:
            raise HTTPException(413, "短链请求过大")
        body.extend(chunk)
    try:
        source = LinkSource.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "短链登记格式无效") from None
    await asyncio.to_thread(_register_source, connection, source)
    return Response(status_code=204)


@router.get("/chamber/r/{code}")
def production_redirect(code: str, request: Request):
    if not re.fullmatch(CODE_PATTERN, code):
        raise HTTPException(404)
    connection = _production_connection(env())
    tenant, aid, origin = _site_owner(connection)
    with tenant_scope(tenant, None):
        return analytics.redirect_link(
            code,
            request,
            expected_agent=aid,
            expected_intent="chamber:" + code,
            expected_origin=origin,
            cookie_path="/chamber/r/",
        )
