"""Rebuildable, Agent-scoped reporting. Read APIs never scan event history.

Source projections and dirty dates commit together. Rebuilding locks one dirty
date, atomically replaces its hour/day snapshots and exact audience sets, then
acknowledges the date. A concurrent source write leaves a new dirty notification.
"""

import logging
from datetime import date, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from .analytics_ingest import canonical_scope, scoped_tx
from .db import all_rows, audit, one, require_agent
from .scope import current_tenant, principal

router = APIRouter()
TZ = ZoneInfo("Asia/Shanghai")
VERSION = "analytics-report-v1"
BASIC = ("PageView", "Engagement", "Identify", "VisitSession", "ShortLinkClick")
OBJECTIVES = {
    "signup": "CompleteRegistration",
    "lead": "Lead",
    "trial": "StartTrial",
    "purchase": "Purchase",
}

# Only the background rebuild path reads these projections. All joins retain
# tenant and Agent keys; test sessions cannot contribute to the live partition.
SOURCES = """
WITH visits AS (
  SELECT v.*,coalesce(l.platform,'unattributed') AS channel,
    coalesce(CASE WHEN v.source LIKE 'site:%%' THEN substring(v.source from 6) END,t.connection_id::text,v.source) AS identity_namespace
  FROM lead.visit_sessions v LEFT JOIN lead.tracking_links l
    ON (l.tenant_id,l.agent_id,l.id)=(v.tenant_id,v.agent_id,v.tracking_link_id)
  LEFT JOIN lead.pixel_tests t ON v.source='pixel-test:'||t.id::text AND t.tenant_id=v.tenant_id AND t.agent_id=v.agent_id
  WHERE v.tenant_id=lead.ctx_tenant() AND v.agent_id=lead.ctx_agent()
    AND v.started_at>=%(start)s AND v.started_at<%(end)s AND v.exclusion_reason IS NULL
    AND ((%(scope)s='live' AND v.source NOT LIKE 'pixel-test:%%') OR v.source='pixel-test:'||%(scope)s)
), pixels AS (
  SELECT p.*,coalesce(l.platform,'unattributed') AS channel
  FROM lead.pixel_receipts p LEFT JOIN lead.pixel_touchpoints t
    ON (t.tenant_id,t.agent_id,t.id)=(p.tenant_id,p.agent_id,p.touchpoint_id)
  LEFT JOIN lead.analytics_clicks click ON (click.tenant_id,click.agent_id,click.id)=(p.tenant_id,p.agent_id,p.click_id)
  LEFT JOIN lead.tracking_links l ON (l.tenant_id,l.agent_id,l.id)=(p.tenant_id,p.agent_id,coalesce(t.tracking_link_id,click.tracking_link_id))
  WHERE p.tenant_id=lead.ctx_tenant() AND p.agent_id=lead.ctx_agent() AND p.scope_key=%(scope)s
    AND p.occurred_at>=%(start)s AND p.occurred_at<%(end)s
), legacy AS (
  SELECT e.*,coalesce(l.platform,'unattributed') AS channel,
    CASE e.type WHEN 'signup' THEN 'CompleteRegistration' WHEN 'lead' THEN 'Lead'
      WHEN 'trial' THEN 'StartTrial' WHEN 'purchase' THEN 'Purchase' ELSE e.type END AS event_name
  FROM lead.conversion_events e LEFT JOIN lead.visit_sessions v
    ON (v.tenant_id,v.agent_id,v.id)=(e.tenant_id,e.agent_id,e.visit_session_id)
  LEFT JOIN lead.tracking_links l ON (l.tenant_id,l.agent_id,l.id)=(v.tenant_id,v.agent_id,v.tracking_link_id)
  WHERE %(scope)s='live' AND e.tenant_id=lead.ctx_tenant() AND e.agent_id=lead.ctx_agent()
    AND e.occurred_at>=%(start)s AND e.occurred_at<%(end)s
    AND NOT EXISTS(SELECT 1 FROM lead.pixel_receipts p WHERE p.tenant_id=e.tenant_id AND p.agent_id=e.agent_id
      AND p.connection_id=e.connection_id AND p.scope_key='live' AND p.event_id=e.source_event_id)
), facts AS (
  SELECT occurred_at AS at_time,channel,event_name,coalesce(currency::text,'') AS currency,
    1 AS observed,(counted AND event_name NOT IN ('PageView','Engagement','Identify'))::integer AS counted,
    (counted AND trust_level='server_verified' AND event_name NOT IN ('PageView','Engagement','Identify'))::integer AS server_counted,
    0 AS clicks,(counted AND event_name='PageView')::integer AS pv,0 AS sessions,0 AS attributed_sessions,0 AS qualified_visits,
    0 AS unknown_pv,(event_name='PageView' AND anonymous_id IS NULL)::integer AS unknown_visitors,
    (counted AND event_name NOT IN ('PageView','Engagement','Identify') AND user_key IS NULL AND anonymous_id IS NULL)::integer AS unknown_users,
    CASE WHEN counted AND trust_level='server_verified' AND event_name='Purchase' THEN coalesce(value,0) ELSE 0 END AS server_revenue
  FROM pixels
  UNION ALL SELECT started_at,channel,CASE WHEN source='redirect' THEN 'ShortLinkClick' ELSE 'VisitSession' END,'',
    0,0,0,(source='redirect')::integer,0,(source<>'redirect')::integer,
    (source<>'redirect' AND tracking_link_id IS NOT NULL)::integer,
    (source<>'redirect' AND tracking_link_id IS NOT NULL AND qualified IS TRUE)::integer,
    (source<>'redirect' AND NOT EXISTS(SELECT 1 FROM lead.pixel_receipts p WHERE p.tenant_id=visits.tenant_id
      AND p.agent_id=visits.agent_id AND p.scope_key=%(scope)s AND p.visit_session_id=visits.id AND p.event_name='PageView'))::integer,
    (source<>'redirect' AND anonymous_id IS NULL)::integer,0,0 FROM visits
  UNION ALL SELECT occurred_at,channel,event_name,coalesce(currency::text,''),1,1,
    (trust_level='server_verified')::integer,0,0,0,0,0,0,0,1,
    CASE WHEN trust_level='server_verified' AND event_name='Purchase' THEN coalesce(value,0) ELSE 0 END FROM legacy
)
"""


def bounds(start_date=None, end_date=None):
    today = datetime.now(TZ).date()
    end_date = end_date or today
    start_date = start_date or end_date - timedelta(days=29)
    if start_date > end_date or (end_date - start_date).days > 92 or end_date > today:
        raise HTTPException(422, "请选择不超过 93 天且不晚于今天的日期范围")
    return start_date, end_date


def _replace_day(c, scope, day):
    params = {
        "scope": scope,
        "day": day,
        "start": datetime.combine(day, time.min, TZ),
        "end": datetime.combine(day + timedelta(days=1), time.min, TZ),
    }
    c.execute("DELETE FROM lead.analytics_rollups WHERE day=%s", (day,))
    c.execute("DELETE FROM lead.analytics_audience WHERE day=%s", (day,))
    c.execute(
        SOURCES
        + """
      INSERT INTO lead.analytics_rollups(tenant_id,agent_id,scope_key,grain,day,bucket,channel,event_name,currency,
        observed,counted,server_counted,clicks,pv,sessions,attributed_sessions,qualified_visits,unknown_pv,unknown_visitors,unknown_users,server_revenue)
      SELECT lead.ctx_tenant(),lead.ctx_agent(),%(scope)s,g.grain,%(day)s,g.bucket,channel,event_name,currency,
        sum(observed),sum(counted),sum(server_counted),sum(clicks),sum(pv),sum(sessions),sum(attributed_sessions),sum(qualified_visits),
        sum(unknown_pv),sum(unknown_visitors),sum(unknown_users),sum(server_revenue)
      FROM facts CROSS JOIN LATERAL (VALUES
        ('day',date_trunc('day',at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'),
        ('hour',date_trunc('hour',at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')) g(grain,bucket)
      GROUP BY g.grain,g.bucket,channel,event_name,currency
    """,
        params,
    )
    c.execute(
        SOURCES
        + """, people AS (
      SELECT occurred_at AS at_time,channel,'visitor' AS kind,connection_id::text||':'||anonymous_id::text AS identity_key
        FROM pixels WHERE event_name='PageView' AND anonymous_id IS NOT NULL
      UNION ALL SELECT started_at,channel,'visitor',identity_namespace||':'||anonymous_id::text
        FROM visits WHERE source<>'redirect' AND anonymous_id IS NOT NULL
          AND NOT EXISTS(SELECT 1 FROM lead.pixel_receipts p WHERE p.tenant_id=visits.tenant_id
            AND p.agent_id=visits.agent_id AND p.scope_key=%(scope)s AND p.visit_session_id=visits.id AND p.event_name='PageView')
      UNION ALL SELECT started_at,channel,'session',id::text FROM visits WHERE source<>'redirect'
      UNION ALL SELECT occurred_at,channel,'event:'||event_name,coalesce(user_key,'anon:'||connection_id::text||':'||anonymous_id::text)
        FROM pixels WHERE counted AND event_name NOT IN ('PageView','Engagement','Identify')
    ) INSERT INTO lead.analytics_audience(tenant_id,agent_id,scope_key,grain,day,bucket,channel,kind,identity_key)
      SELECT DISTINCT lead.ctx_tenant(),lead.ctx_agent(),%(scope)s,g.grain,%(day)s,g.bucket,channel,kind,identity_key
      FROM people CROSS JOIN LATERAL (VALUES
        ('day',date_trunc('day',at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai'),
        ('hour',date_trunc('hour',at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai')) g(grain,bucket)
      WHERE identity_key IS NOT NULL ON CONFLICT DO NOTHING
    """,
        params,
    )
    _replace_geography(c, params)
    count = one(
        c,
        "SELECT coalesce(sum(observed+sessions+clicks),0)::bigint AS n FROM lead.analytics_rollups WHERE day=%s AND grain='day'",
        (day,),
    )["n"]
    c.execute(
        """INSERT INTO lead.analytics_report_days(tenant_id,agent_id,scope_key,day,refreshed_at,source_rows,version)
        VALUES(lead.ctx_tenant(),lead.ctx_agent(),%s,%s,clock_timestamp(),%s,%s)
        ON CONFLICT(tenant_id,agent_id,scope_key,day) DO UPDATE SET revision=lead.analytics_report_days.revision+1,
          refreshed_at=excluded.refreshed_at,source_rows=excluded.source_rows,version=excluded.version""",
        (scope, day, count, VERSION),
    )
    if scope == "live":
        c.execute(
            "UPDATE lead.agents SET analytics_revision=analytics_revision+1 WHERE id=lead.ctx_agent()"
        )


def _replace_geography(c, params):
    c.execute("DELETE FROM lead.analytics_geo_rollups WHERE day=%(day)s", params)
    c.execute(
        SOURCES
        + """, geo AS (
      SELECT connection_id,event_id,country_code AS country FROM lead.analytics_clean_events
        WHERE occurred_at>=%(start)s AND occurred_at<%(end)s
      UNION ALL SELECT connection_id,event_id,event->>'country_code' FROM lead.analytics_event_archives
        WHERE occurred_at>=%(start)s AND occurred_at<%(end)s
    ), geographic_facts AS (
      SELECT p.channel,p.event_name,coalesce(g.country,'ZZ') AS country,0 AS clicks,
        (p.counted AND p.event_name='PageView')::integer AS pv,
        (p.counted AND p.trust_level='server_verified' AND p.event_name NOT IN ('PageView','Engagement','Identify'))::integer AS conversions,
        'site_device:'||coalesce(g.country,'ZZ') AS device_kind,
        CASE WHEN p.counted AND p.event_name='PageView'
          THEN coalesce(p.connection_id::text||':'||p.anonymous_id::text,'unknown') END AS device_key
      FROM pixels p LEFT JOIN geo g ON g.connection_id=p.connection_id AND g.event_id=p.event_id
      UNION ALL SELECT v.channel,'ShortLinkClick',coalesce(g.country,'ZZ'),1,0,0,
        'click_device:'||coalesce(g.country,'ZZ'),v.anonymous_id::text
        FROM visits v LEFT JOIN geo g ON g.connection_id IS NULL AND g.event_id=v.source_session_id
        WHERE v.source='redirect'
      UNION ALL SELECT channel,event_name,'ZZ',0,0,(trust_level='server_verified')::integer,NULL,NULL FROM legacy
    ), device_sets AS (
      INSERT INTO lead.analytics_audience(tenant_id,agent_id,scope_key,grain,day,bucket,channel,kind,identity_key)
      SELECT DISTINCT lead.ctx_tenant(),lead.ctx_agent(),%(scope)s,'day',%(day)s,%(start)s,
        channel,device_kind,device_key
      FROM geographic_facts WHERE device_key IS NOT NULL
    ) INSERT INTO lead.analytics_geo_rollups(tenant_id,agent_id,scope_key,day,channel,event_name,country,clicks,pv,server_conversions)
      SELECT lead.ctx_tenant(),lead.ctx_agent(),%(scope)s,%(day)s,channel,event_name,country,sum(clicks),sum(pv),sum(conversions)
      FROM geographic_facts GROUP BY channel,event_name,country""",
        params,
    )


def materialized_metrics(c, objective):
    """Compatibility fast path; old installs bootstrap before switching reads."""
    previous = (
        one(c, "SELECT current_setting('app.analytics_scope',true) AS scope")["scope"]
        or ""
    )
    try:
        c.execute("SELECT set_config('app.analytics_scope','live',true)")
        if not one(c, "SELECT 1 FROM lead.analytics_report_days LIMIT 1"):
            return None
        start, end = bounds()
        if one(
            c,
            """SELECT 1 FROM lead.analytics_report_dirty q WHERE day BETWEEN %s AND %s
          AND NOT EXISTS(SELECT 1 FROM lead.analytics_report_days d WHERE d.day=q.day) LIMIT 1""",
            (start, end),
        ):
            return None
        return one(
            c,
            """SELECT coalesce(sum(clicks),0)::bigint AS clicks,
          coalesce(sum(attributed_sessions),0)::bigint AS visits,coalesce(sum(qualified_visits),0)::bigint AS qualified,
          coalesce(sum(server_counted) FILTER(WHERE event_name=%s),0)::bigint AS conversions
          FROM lead.analytics_rollups WHERE grain='day' AND day BETWEEN %s AND %s""",
            (OBJECTIVES[objective], start, end),
        )
    finally:
        c.execute("SELECT set_config('app.analytics_scope',%s,true)", (previous,))


def refresh_scope(tenant, agent, scope="live", limit=7):
    completed = []
    for _ in range(limit):
        day = None
        try:
            with scoped_tx(tenant, agent, scope) as c:
                c.execute("SET LOCAL statement_timeout='20s'")
                row = one(
                    c,
                    """SELECT * FROM lead.analytics_report_dirty WHERE state='pending' AND next_attempt_at<=now()
                    ORDER BY day DESC LIMIT 1 FOR UPDATE SKIP LOCKED""",
                )
                if not row:
                    break
                day = row["day"]
                _replace_day(c, scope, day)
                c.execute(
                    "DELETE FROM lead.analytics_report_dirty WHERE day=%s", (day,)
                )
            completed.append(day.isoformat())
        except Exception as error:
            if day is not None:
                with scoped_tx(tenant, agent, scope) as c:
                    c.execute(
                        """UPDATE lead.analytics_report_dirty SET attempts=attempts+1,
                        state=CASE WHEN attempts>=5 THEN 'failed' ELSE 'pending' END,
                        next_attempt_at=now()+make_interval(secs=>least(300,power(2,least(attempts+1,8))::integer)),
                        error_code='REPORT_REBUILD_UNAVAILABLE' WHERE day=%s AND request_token=%s""",
                        (day, row["request_token"]),
                    )
            logging.warning(
                "Analytics report rebuild unavailable (%s); scoped snapshot retained",
                type(error).__name__,
            )
            break
    return completed


def can_manage():
    who = principal() or {}
    return who.get("role") == "owner" or (
        who.get("role") == "operator"
        and "analytics.manage" in who.get("capabilities", [])
    )


def require_scope(c, aid, scope):
    agent = require_agent(c, aid)
    if scope != "live" and not one(
        c, "SELECT id FROM lead.pixel_tests WHERE id=%s AND agent_id=%s", (scope, aid)
    ):
        raise HTTPException(404, "测试会话不存在")
    return agent


def _filters(channel, event_name):
    return " AND (%(channel)s::text IS NULL OR channel=%(channel)s)"


def read_report(
    c, aid, start_date, end_date, scope, grain="day", channel=None, event_name=None
):
    from .admin_controls import attribution
    from .analytics import promotion_settings

    agent = require_scope(c, aid, scope)
    policy, _ = attribution(c, aid)
    # Choosing a business event changes the conversion goal, while traffic
    # remains the denominator for the same date/channel selection.
    objective = event_name or OBJECTIVES[policy["objective"]]
    args = {
        "start": start_date,
        "end": end_date,
        "grain": grain,
        "channel": channel,
        "event": event_name,
        "objective": objective,
    }
    where = " WHERE grain='day' AND day BETWEEN %(start)s AND %(end)s" + _filters(
        channel, event_name
    )
    fields = """coalesce(sum(clicks),0)::bigint AS clicks,coalesce(sum(pv),0)::bigint AS pv,
      coalesce(sum(sessions),0)::bigint AS sessions,coalesce(sum(attributed_sessions),0)::bigint AS attributed_sessions,
      coalesce(sum(qualified_visits),0)::bigint AS qualified_visits,
      coalesce(sum(counted) FILTER(WHERE %(event)s::text IS NULL OR event_name=%(event)s),0)::bigint AS event_count,
      coalesce(sum(server_counted) FILTER(WHERE event_name=%(objective)s),0)::bigint AS conversions,
      coalesce(sum(unknown_pv),0)::bigint AS unknown_pv,coalesce(sum(unknown_visitors),0)::bigint AS unknown_visitors"""
    summary = one(c, "SELECT " + fields + " FROM lead.analytics_rollups" + where, args)
    audience = one(
        c,
        """SELECT count(DISTINCT identity_key) FILTER(WHERE kind='visitor') AS visitors
      FROM lead.analytics_audience WHERE grain='day' AND day BETWEEN %(start)s AND %(end)s
      AND (%(channel)s::text IS NULL OR channel=%(channel)s)""",
        args,
    )
    coverage = one(
        c,
        """SELECT EXISTS(SELECT 1 FROM lead.analytics_connections WHERE provider='lead_site' AND state='verified'
      AND coalesce(last_pageview_at,last_received_at) IS NOT NULL) AS site,
      EXISTS(SELECT 1 FROM lead.analytics_credentials WHERE last_received_at IS NOT NULL) AS server,
      (SELECT max(last_pageview_at) FROM lead.analytics_connections WHERE provider='lead_site') AS last_pageview_at""",
    )
    totals = one(
        c,
        """SELECT coalesce(sum(pv+sessions),0) AS site_rows,coalesce(sum(server_counted),0) AS server_rows
      FROM lead.analytics_rollups WHERE grain='day'""",
    )
    site = bool(coverage["site"] or totals["site_rows"])
    server = bool(coverage["server"] or totals["server_rows"])
    progress = one(
        c,
        """SELECT count(*) AS pending_days,count(*) FILTER(WHERE state='failed') AS failed_days,
      count(*) FILTER(WHERE NOT EXISTS(SELECT 1 FROM lead.analytics_report_days d WHERE d.day=q.day)) AS unbuilt_days,
      min(changed_at) AS oldest_change_at FROM lead.analytics_report_dirty q WHERE day BETWEEN %s AND %s""",
        (start_date, end_date),
    )
    last = one(
        c,
        "SELECT max(refreshed_at) AS updated_at FROM lead.analytics_report_days WHERE day BETWEEN %s AND %s",
        (start_date, end_date),
    )["updated_at"]
    summary["visitors"] = (
        audience["visitors"] if site and not summary["unknown_visitors"] else None
    )
    summary["pv"] = summary["pv"] if site and not summary["unknown_pv"] else None
    for key in ("sessions", "attributed_sessions", "qualified_visits", "event_count"):
        if not site and not (server and key == "event_count"):
            summary[key] = None
    if not server:
        summary["conversions"] = None
    if progress["unbuilt_days"]:
        summary = {key: None for key in summary}
    series = all_rows(
        c,
        """SELECT bucket,sum(clicks)::bigint AS clicks,sum(pv)::bigint AS pv,sum(sessions)::bigint AS sessions,
      sum(counted) FILTER(WHERE %(event)s::text IS NULL OR event_name=%(event)s)::bigint AS event_count,
      sum(server_counted) FILTER(WHERE event_name=%(objective)s)::bigint AS conversions,
      sum(unknown_pv)::bigint AS unknown_pv FROM lead.analytics_rollups
      WHERE grain=%(grain)s AND day BETWEEN %(start)s AND %(end)s"""
        + _filters(channel, event_name)
        + " GROUP BY bucket ORDER BY bucket",
        args,
    )
    for item in series:
        if not site or item["unknown_pv"]:
            item["pv"] = None
        if not site:
            item["sessions"] = None
        if not site and not server:
            item["event_count"] = None
        if not server:
            item["conversions"] = None
        elif item["conversions"] is None:
            item["conversions"] = 0
    channels = all_rows(
        c,
        "SELECT channel,"
        + fields
        + " FROM lead.analytics_rollups"
        + where
        + " GROUP BY channel ORDER BY channel",
        args,
    )
    for item in channels:
        if not site:
            for key in ("pv", "sessions", "attributed_sessions", "qualified_visits"):
                item[key] = None
        if item["unknown_pv"]:
            item["pv"] = None
        if not server:
            item["conversions"] = None
    events = all_rows(
        c,
        """SELECT event_name,sum(observed)::bigint AS observed,sum(counted)::bigint AS count,
      sum(server_counted)::bigint AS server_count,max(unknown_users)>0 AS users_incomplete
      FROM lead.analytics_rollups"""
        + where
        + " AND event_name<>ALL(%(basic)s) AND (%(event)s::text IS NULL OR event_name=%(event)s) GROUP BY event_name ORDER BY event_name",
        {**args, "basic": list(BASIC)},
    )
    users = all_rows(
        c,
        """SELECT substring(kind from 7) AS name,count(DISTINCT identity_key) AS n FROM lead.analytics_audience
      WHERE grain='day' AND day BETWEEN %(start)s AND %(end)s AND kind LIKE 'event:%%'
      AND (%(channel)s::text IS NULL OR channel=%(channel)s) GROUP BY kind""",
        args,
    )
    for item in events:
        item["users"] = (
            None
            if item["users_incomplete"]
            else next((u["n"] for u in users if u["name"] == item["event_name"]), 0)
        )
    amounts = all_rows(
        c,
        """SELECT currency,sum(server_revenue) AS amount FROM lead.analytics_rollups"""
        + where
        + " AND event_name='Purchase' AND currency<>'' AND server_counted>0 GROUP BY currency ORDER BY currency",
        args,
    )
    for item in amounts:
        item["amount"] = str(item["amount"])
    countries = all_rows(
        c,
        """SELECT country,sum(clicks)::bigint AS clicks,sum(pv)::bigint AS pv,
      coalesce(sum(server_conversions) FILTER(WHERE event_name=%(objective)s),0)::bigint AS conversions
      FROM lead.analytics_geo_rollups WHERE day BETWEEN %(start)s AND %(end)s
        AND (%(channel)s::text IS NULL OR channel=%(channel)s)
      GROUP BY country ORDER BY sum(clicks)+sum(pv)+sum(server_conversions) DESC,country""",
        args,
    )
    from .analytics_geo import status as geo_status

    return {
        "agent_id": aid,
        "scope_key": scope,
        "environment": "production" if scope == "live" else "test",
        "timezone": "Asia/Shanghai",
        "start_date": start_date,
        "end_date": end_date,
        "grain": grain,
        "version": VERSION,
        "summary": summary,
        "series": series,
        "channels": channels,
        "countries": countries,
        "geography": geo_status(),
        "events": events,
        "amounts": amounts,
        "coverage": {
            "site": site,
            "server": server,
            "last_pageview_at": coverage["last_pageview_at"],
        },
        "aggregation": {
            **progress,
            "updated_at": last,
            "ready": progress["pending_days"] == 0,
        },
        "conversion_event": objective,
        "promotion": promotion_settings(c, agent),
        "can_manage": can_manage(),
    }


@router.get("/api/agents/{aid}/analytics/summary")
def summary(
    aid: UUID,
    start_date: date | None = None,
    end_date: date | None = None,
    scope_key: str = "live",
    grain: str = "day",
    channel: str | None = None,
    event_name: str | None = None,
):
    scope_key = canonical_scope(scope_key)
    start_date, end_date = bounds(start_date, end_date)
    if grain not in ("day", "hour"):
        raise HTTPException(422, "统计粒度无效")
    with scoped_tx(current_tenant(), str(aid), scope_key) as c:
        return read_report(
            c, str(aid), start_date, end_date, scope_key, grain, channel, event_name
        )


class Rebuild(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    scope_key: str = "live"


@router.post("/api/agents/{aid}/analytics/summary/rebuild", status_code=202)
def rebuild(aid: UUID, body: Rebuild):
    from .admin_permissions import require

    require("analytics.manage")
    body = body.model_copy(update={"scope_key": canonical_scope(body.scope_key)})
    start, end = bounds(body.start_date, body.end_date)
    with scoped_tx(current_tenant(), str(aid), body.scope_key) as c:
        require_scope(c, str(aid), body.scope_key)
        c.execute(
            """INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
          SELECT lead.ctx_tenant(),lead.ctx_agent(),%s,d::date FROM generate_series(%s::date,%s::date,'1 day') d
          ON CONFLICT(tenant_id,agent_id,scope_key,day) DO UPDATE SET revision=lead.analytics_report_dirty.revision+1,
            changed_at=clock_timestamp(),request_token=gen_random_uuid(),state='pending',attempts=0,next_attempt_at=now(),error_code=NULL""",
            (body.scope_key, start, end),
        )
        audit(
            c,
            str(aid),
            "analytics.report.rebuild",
            {
                "scope_key": body.scope_key,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            },
        )
    return {
        "ok": True,
        "agent_id": str(aid),
        "scope_key": body.scope_key,
        "days": (end - start).days + 1,
    }


@router.get("/api/agents/{aid}/analytics/overview")
def overview(aid: UUID, channel: str | None = None, country: str | None = None):
    """Daily live snapshots for the compact workspace card, in Beijing time."""
    from .pixel_events import pixel_installed

    start, end = bounds(None, None)
    args = {"start": start, "end": end, "channel": channel, "country": country}
    window = "day BETWEEN %(start)s AND %(end)s AND (%(channel)s::text IS NULL OR channel=%(channel)s)"
    with scoped_tx(current_tenant(), str(aid), "live") as c:
        require_scope(c, str(aid), "live")
        connection = one(c, """SELECT agent_id,state,last_pageview_at FROM lead.analytics_connections
            WHERE provider='lead_site' AND state<>'disconnected'
            ORDER BY selected_at DESC,created_at DESC LIMIT 1""")
        installed = pixel_installed(c, connection)
        geo = all_rows(c, "SELECT day, country, sum(clicks) AS clicks,sum(pv) AS pv FROM lead.analytics_geo_rollups WHERE " + window + " GROUP BY day,country", args)
        totals = all_rows(c, "SELECT day,event_name,sum(clicks) AS clicks,sum(counted) AS events,sum(unknown_visitors) AS missing FROM lead.analytics_rollups WHERE grain='day' AND " + window + " GROUP BY day,event_name", args)
        visitors = all_rows(c, "SELECT day,count(DISTINCT identity_key) AS n FROM lead.analytics_audience WHERE grain='day' AND kind='visitor' AND " + window + " GROUP BY day", args)
        devices = all_rows(
            c,
            "SELECT day,count(DISTINCT identity_key) AS n FROM lead.analytics_audience "
            "WHERE grain='day' AND starts_with(kind,'click_device:') AND " + window +
            " AND (%(country)s::text IS NULL OR kind='click_device:'||%(country)s) GROUP BY day",
            args,
        )
        site_devices = all_rows(
            c,
            "SELECT day,count(DISTINCT identity_key) AS n,bool_or(identity_key='unknown') AS missing "
            "FROM lead.analytics_audience WHERE grain='day' AND starts_with(kind,'site_device:') AND "
            + window + " AND (%(country)s::text IS NULL OR kind='site_device:'||%(country)s) GROUP BY day",
            args,
        ) if installed else []
        pending = all_rows(c, "SELECT day FROM lead.analytics_report_dirty q WHERE day BETWEEN %(start)s AND %(end)s AND NOT EXISTS (SELECT 1 FROM lead.analytics_report_days d WHERE d.day=q.day)", args)
        definitions = all_rows(c, "SELECT name,label FROM lead.pixel_definitions")
    rows = {(start + timedelta(days=i)): {"clicks": 0, "pv": 0, "visitors": 0, "events": {}} for i in range((end - start).days + 1)}
    labels = {r["name"]: r["label"] for r in definitions}
    reported = set()
    missing = set()
    for r in totals:
        day = rows[r["day"]]
        if not country:
            day["clicks"] += int(r["clicks"])
        if r["missing"]:
            missing.add(r["day"])
        if r["event_name"] in labels and r["events"]:
            reported.add(r["event_name"])
            if not country:
                day["events"][r["event_name"]] = int(r["events"])
    for r in geo:
        if not country or r["country"] == country:
            rows[r["day"]]["pv"] += int(r["pv"])
        if country and r["country"] == country:
            rows[r["day"]]["clicks"] += int(r["clicks"])
    for r in visitors:
        rows[r["day"]]["visitors"] = r["n"]
    device_counts = {r["day"]: r["n"] for r in devices}
    site_counts = {r["day"]: None if r["missing"] else r["n"] for r in site_devices}
    unavailable = {r["day"] for r in pending}
    for day, row in rows.items():
        if not installed or day in unavailable:
            row["pv"] = None
            row["site_devices"] = None
        else:
            # Older snapshots may have PVs before the device set is rebuilt.
            row["site_devices"] = site_counts.get(day, 0 if row["pv"] == 0 else None)
        if country or day in missing or day in unavailable:
            row["visitors"] = None
        if day in unavailable:
            row["clicks"] = None
        # Positive PV without an identity set predates browser measurement.
        row["devices"] = device_counts.get(day, 0 if row["clicks"] == 0 else None)
        if day in unavailable:
            row["devices"] = None
        if country or day in unavailable:
            row["events"] = None
    return {"agent_id": str(aid), "countries": sorted({r["country"] for r in geo}),
            "events": {key: labels[key] for key in sorted(reported)},
            "series": [dict(day=day, **row) for day, row in rows.items()]}
