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
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from . import analytics, distribution
from .analytics_ingest import scoped_tx
from .analytics_reporting import TZ
from .config import env
from .db import all_rows, one, tx
from .growth.promotion_dashboard import (
    NON_CHANNELS,
    PromotionRange,
    read_promotion_dashboard,
)
from .growth.promotion_link_report import read_link_report
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
MAX_RECORD_SLOTS = 10
MAX_RECORD_LINKS = 40


class ChamberAnalyticsUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Chamber 生产统计暂时不可用")


Count = Annotated[int, Field(strict=True, ge=0)]
PositiveCount = Annotated[int, Field(strict=True, gt=0)]
RecordSlot = Annotated[int, Field(strict=True, ge=0, lt=MAX_RECORD_SLOTS)]
Code = Annotated[str, Field(strict=True, pattern=rf"^{CODE_PATTERN}$")]
ReportDay = Annotated[str, Field(strict=True, pattern=r"^\d{4}-\d{2}-\d{2}$")]
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


class BoundedCounts(Counts):
    click_devices_min: PositiveCount | None = None
    site_devices_min: PositiveCount | None = None

    @model_validator(mode="after")
    def coherent_bounds(self):
        for events, devices, minimum in (
            (self.clicks, self.click_devices, self.click_devices_min),
            (self.pv, self.site_devices, self.site_devices_min),
        ):
            if minimum is not None and (devices is not None or (events is not None and minimum > events)):
                raise ValueError("Invalid minimum device count")
        return self


class PlatformCounts(BoundedCounts):
    platform: Channel


class PromotionBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: Annotated[str, Field(strict=True, max_length=35)]
    totals: BoundedCounts
    channels: list[PlatformCounts]


class RangedReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: ReportDay
    range: PromotionRange
    pixel_connected: Annotated[bool, Field(strict=True)]
    history_complete: Annotated[bool, Field(strict=True)]
    totals: BoundedCounts
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


class RecordLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: RecordSlot
    code: Code
    published_at: AwareDatetime


class RecordCounts(BoundedCounts):
    slot: RecordSlot
    click_devices_min: PositiveCount | None
    site_devices_min: PositiveCount | None


class RecordReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day: ReportDay
    range: PromotionRange
    records: Annotated[list[RecordCounts], Field(max_length=MAX_RECORD_SLOTS)]

    @model_validator(mode="after")
    def unique_slots(self):
        if len({row.slot for row in self.records}) != len(self.records):
            raise ValueError("Duplicate promotion record slot")
        return self


class LinkSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Code
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


def fetch_promotion_dashboard(aid, period: PromotionRange, day, *, device_bounds=False):
    values = _local_settings(aid)
    if values is None:
        return None
    requested_day = str(day)
    params = {"day": requested_day, "range": period}
    if device_bounds:
        params["device_bounds"] = "true"
    payload = _request(values, READ_KEY, "GET", REPORT_PATH, params=params)
    try:
        report = RangedReport.model_validate_json(payload)
        if report.day != requested_day or report.range != period:
            raise ValueError("Unexpected promotion reporting window")
    except ValueError:
        raise ChamberAnalyticsUnavailable() from None
    return report.model_dump(mode="json", exclude_unset=True)


def _validate_record_links(links):
    if not 1 <= len(links) <= MAX_RECORD_LINKS:
        raise ValueError("Invalid promotion link count")
    codes = set()
    publication_times = {}
    for link in links:
        if link.code in codes or publication_times.setdefault(link.slot, link.published_at) != link.published_at:
            raise ValueError("Ambiguous promotion link selection")
        codes.add(link.code)
    return publication_times.keys()


def fetch_promotion_links(aid, period: PromotionRange, day, links: list[RecordLink]) -> dict[int, dict] | None:
    values = _local_settings(aid)
    if values is None:
        return None
    if not links:
        return {}
    requested_day = str(day)
    try:
        slots = _validate_record_links(links)
        params = [("day", requested_day), ("range", period)]
        params.extend(("link", f"{link.slot},{link.code},{link.published_at.isoformat()}") for link in links)
        payload = _request(values, READ_KEY, "GET", REPORT_PATH, params=params)
        report = RecordReport.model_validate_json(payload)
        if report.day != requested_day or report.range != period or any(row.slot not in slots for row in report.records):
            raise ValueError("Unexpected promotion record report")
    except ValueError:
        raise ChamberAnalyticsUnavailable() from None
    return {row.slot: row.model_dump(mode="json", exclude={"slot"}) for row in report.records}


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


def _report_response(report):
    body = report.model_dump_json(exclude_unset=True).encode("utf-8")
    if len(body) > MAX_REPORT_BYTES:
        raise HTTPException(503, "Chamber 生产统计暂时不可用")
    return Response(body, media_type="application/json")


def _ranged_response(data, *, device_bounds):
    fields = BoundedCounts.model_fields if device_bounds else Counts.model_fields

    def counts(row):
        return {field: row[field] for field in fields if field in row}

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
    except ValueError:
        raise HTTPException(503, "Chamber 生产统计暂时不可用") from None
    return _report_response(report)


def _parse_record_links(values, now):
    links = []
    try:
        for value in values:
            if len(value) > 80:
                raise ValueError("Invalid promotion link selection")
            slot, code, published = value.split(",", 2)
            if not re.fullmatch("[0-9]", slot) or not published:
                raise ValueError("Invalid promotion link selection")
            link = RecordLink(slot=int(slot), code=code, published_at=datetime.fromisoformat(published))
            if link.published_at > now:
                raise ValueError("Promotion publication is in the future")
            links.append(link)
        _validate_record_links(links)
    except (ValueError, OverflowError):
        raise HTTPException(422, "推广记录查询范围无效") from None
    return links


def _registered_links(c, origin, links):
    rows = all_rows(c, """
        SELECT id,public_code,destination_url,allowed_host FROM lead.tracking_links
        WHERE tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent()
          AND placement='chamber' AND intent_key='chamber:'||public_code
          AND public_code=ANY(%s) AND NOT (platform=ANY(%s))
    """, ([link.code for link in links], NON_CHANNELS))
    allowed_host = urlsplit(origin).hostname
    registered = {}
    for row in rows:
        try:
            if row["allowed_host"] == allowed_host and _https_origin(row["destination_url"]) == origin:
                registered[row["public_code"]] = row["id"]
        except ValueError:
            continue
    incomplete = {link.slot for link in links if link.code not in registered}
    return [
        (link.slot, registered[link.code], link.published_at)
        for link in links if link.slot not in incomplete
    ]


@router.get(REPORT_PATH, response_model=DailyReport | RangedReport | RecordReport)
def production_daily_report(
    day: date,
    connection: Annotated[str, Depends(_report_connection)],
    period: Annotated[PromotionRange | None, Query(alias="range")] = None,
    link: Annotated[list[str] | None, Query(max_length=MAX_RECORD_LINKS)] = None,
    device_bounds: Annotated[Literal["true"] | None, Query()] = None,
):
    from .analytics_reporting import read_daily_promotion

    now = datetime.now(TZ)
    bounds = device_bounds is not None
    if period is None and (link is not None or bounds) or link is not None and bounds:
        raise HTTPException(422, "推广记录和设备下限须分别指定范围")
    if period is not None and day != now.date():
        raise HTTPException(422, "推广范围统计仅支持当前北京时间")
    links = _parse_record_links(link, now) if link is not None else None
    tenant, aid, origin = _site_owner(connection)
    with tenant_scope(tenant, None), scoped_tx(tenant, aid, "live") as c:
        _ensure_site_owner(c, connection, origin)
        if period is None:
            return read_daily_promotion(c, aid, day)
        if links is not None:
            data = read_link_report(c, connection, period, now, _registered_links(c, origin, links))
        else:
            data = read_promotion_dashboard(c, aid, period, now, [], device_bounds=bounds)
    if links is not None:
        try:
            report = RecordReport(day=day.isoformat(), range=period, records=data)
        except ValueError:
            raise HTTPException(503, "Chamber 生产统计暂时不可用") from None
        return _report_response(report)
    return _ranged_response(data, device_bounds=bounds)


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
