"""Attributed traffic and confirmed publications for the promotion dashboard."""
from datetime import datetime, time, timedelta
from typing import Literal

from ..analytics_reporting import TZ
from ..db import all_rows, one

PromotionRange = Literal["today", "all"]

_METRICS = ("clicks", "click_devices", "pv", "site_devices")
_UNKNOWN = dict.fromkeys(_METRICS)
_NON_CHANNELS = ["lab", "site", "unattributed", "promotion", "direct"]

_LIVE_FACTS = """
WITH pending_days AS (
    SELECT day FROM lead.analytics_report_dirty
    WHERE %(today_view)s AND tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND scope_key='live'
      AND day>=%(trend_day)s AND day<%(snapshot_before)s
), windows AS (
    SELECT %(start)s::timestamptz AS start_at, %(now)s::timestamptz + INTERVAL '1 microsecond' AS end_at
    UNION ALL
    SELECT day::timestamp AT TIME ZONE 'Asia/Shanghai', (day+1)::timestamp AT TIME ZONE 'Asia/Shanghai'
    FROM pending_days
), facts AS (
    SELECT v.started_at at_time, l.platform, 'click' AS kind, v.anonymous_id::text device
    FROM windows w
    JOIN lead.visit_sessions v ON v.started_at>=w.start_at AND v.started_at<w.end_at
    JOIN lead.tracking_links l ON (l.tenant_id,l.agent_id,l.id)=(v.tenant_id,v.agent_id,v.tracking_link_id)
    WHERE v.tenant_id=lead.ctx_tenant() AND v.agent_id=lead.ctx_agent()
      AND v.source='redirect' AND v.trust_level='redirect_observed' AND v.exclusion_reason IS NULL
      AND NOT (l.platform=ANY(%(non_channels)s))
    UNION ALL
    SELECT p.occurred_at, l.platform, 'page', p.connection_id::text||':'||p.anonymous_id::text
    FROM windows w
    JOIN lead.pixel_receipts p ON p.occurred_at>=w.start_at AND p.occurred_at<w.end_at
    JOIN lead.visit_sessions v ON (v.tenant_id,v.agent_id,v.id)=(p.tenant_id,p.agent_id,p.visit_session_id)
    JOIN lead.tracking_links l ON (l.tenant_id,l.agent_id,l.id)=(v.tenant_id,v.agent_id,v.tracking_link_id)
    WHERE p.tenant_id=lead.ctx_tenant() AND p.agent_id=lead.ctx_agent()
      AND p.scope_key='live' AND p.event_name='PageView' AND p.counted AND p.received_at<=%(now)s
      AND v.source<>'redirect' AND v.source NOT LIKE 'pixel-test:%%' AND v.exclusion_reason IS NULL
      AND NOT (l.platform=ANY(%(non_channels)s))
), eligible_platforms AS (
    SELECT unnest(%(published_platforms)s::text[]) AS platform
    UNION SELECT platform FROM facts
    UNION
    SELECT channel FROM lead.analytics_rollups r
    WHERE r.tenant_id=lead.ctx_tenant() AND r.agent_id=lead.ctx_agent() AND r.scope_key='live' AND r.grain='day'
      AND r.day<%(snapshot_before)s AND NOT (r.channel=ANY(%(non_channels)s)) AND (r.clicks>0 OR r.pv>0)
      AND NOT EXISTS(SELECT 1 FROM pending_days p WHERE p.day=r.day)
)
"""

_TODAY_METRICS = _LIVE_FACTS + """
, hourly AS (
    SELECT date_trunc('hour',at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai' AS bucket,
           platform,kind,device
    FROM facts WHERE at_time>=%(start)s
)
SELECT bucket,platform,count(*) FILTER(WHERE kind='click') clicks,
    CASE WHEN NOT coalesce(bool_or(device IS NULL) FILTER(WHERE kind='click'),false)
        THEN count(DISTINCT device) FILTER(WHERE kind='click') END click_devices,
    count(*) FILTER(WHERE kind='page') pv,
    CASE WHEN NOT coalesce(bool_or(device IS NULL) FILTER(WHERE kind='page'),false)
        THEN count(DISTINCT device) FILTER(WHERE kind='page') END site_devices
FROM hourly
GROUP BY GROUPING SETS ((bucket,platform),(bucket),(platform),())
UNION ALL
SELECT NULL,platform,0,0,0,0 FROM eligible_platforms e
WHERE NOT EXISTS(SELECT 1 FROM hourly h WHERE h.platform=e.platform)
"""

_ALL_METRICS = _LIVE_FACTS + """
, historical_counts AS (
    SELECT day,channel AS platform,sum(clicks)::bigint clicks,sum(pv)::bigint pv,bool_or(unknown_pv>0) unknown_pv
    FROM lead.analytics_rollups
    WHERE tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND scope_key='live'
      AND grain='day' AND day<%(trend_day)s AND channel IN (SELECT platform FROM eligible_platforms)
      AND (clicks>0 OR pv>0 OR unknown_pv>0)
    GROUP BY day,channel
), historical_devices AS (
    SELECT day,channel AS platform,kind,identity_key FROM lead.analytics_audience
    WHERE tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND scope_key='live'
      AND grain='day' AND day<%(trend_day)s AND channel IN (SELECT platform FROM eligible_platforms)
      AND (starts_with(kind,'click_device:') OR kind='click_device_snapshot'
        OR (%(pixel_connected)s AND starts_with(kind,'site_device:')))
), device_coverage AS (
    SELECT day,platform,
        bool_or(kind='click_device_snapshot' AND identity_key='complete') AND
            NOT bool_or(kind='click_device_snapshot' AND identity_key='incomplete') AS full_click_snapshot,
        count(DISTINCT identity_key) FILTER(WHERE starts_with(kind,'click_device:')) AS click_devices,
        count(DISTINCT identity_key) FILTER(WHERE starts_with(kind,'site_device:') AND identity_key<>'unknown') AS site_devices,
        bool_or(starts_with(kind,'site_device:') AND identity_key='unknown') AS unknown_site_device
    FROM historical_devices
    GROUP BY day,platform
), daily_counts AS (
    SELECT day,platform,clicks,pv,
        clicks>0 AND NOT coalesce(full_click_snapshot OR click_devices=clicks,false) AS unknown_click_device,
        unknown_pv,
        pv>0 AND (NOT coalesce(site_devices>0,false) OR coalesce(unknown_site_device,false)) AS unknown_site_device
    FROM historical_counts LEFT JOIN device_coverage USING(day,platform)
    UNION ALL
    SELECT (at_time AT TIME ZONE 'Asia/Shanghai')::date AS day,platform,
        count(*) FILTER(WHERE kind='click'),count(*) FILTER(WHERE kind='page'),
        bool_or(kind='click' AND device IS NULL),false,bool_or(kind='page' AND device IS NULL)
    FROM facts GROUP BY day,platform
), counts AS (
    SELECT day,platform,coalesce(sum(clicks),0)::bigint clicks,coalesce(sum(pv),0)::bigint pv,
        bool_or(unknown_click_device) AS unknown_click_device,
        bool_or(unknown_pv) AS unknown_pv,
        bool_or(unknown_site_device) AS unknown_site_device
    FROM (SELECT CASE WHEN day>=%(trend_day)s THEN day END AS day,platform,clicks,pv,
         unknown_click_device,unknown_pv,unknown_site_device FROM daily_counts) ranked
    GROUP BY GROUPING SETS ((day,platform),(day),(platform),())
    HAVING GROUPING(day)=1 OR day IS NOT NULL
), identities AS (
    SELECT day,platform,CASE WHEN starts_with(kind,'click_device:') THEN 'click' ELSE 'page' END AS kind,identity_key AS device
    FROM historical_devices WHERE kind<>'click_device_snapshot' AND identity_key<>'unknown'
    UNION ALL
    SELECT (at_time AT TIME ZONE 'Asia/Shanghai')::date,platform,kind,device FROM facts WHERE device IS NOT NULL
), devices AS (
    SELECT day,platform,count(DISTINCT device) FILTER(WHERE kind='click') click_devices,
        count(DISTINCT device) FILTER(WHERE kind='page') site_devices
    FROM (SELECT CASE WHEN day>=%(trend_day)s THEN day END AS day,platform,kind,device FROM identities) ranked
    GROUP BY GROUPING SETS ((day,platform),(day),(platform),())
    HAVING GROUPING(day)=1 OR day IS NOT NULL
)
SELECT counts.day,counts.platform,counts.clicks,
    CASE WHEN NOT coalesce(counts.unknown_click_device,false) THEN coalesce(devices.click_devices,0) END click_devices,
    CASE WHEN NOT coalesce(counts.unknown_pv,false) THEN counts.pv END pv,
    CASE WHEN NOT coalesce(counts.unknown_pv OR counts.unknown_site_device,false) THEN coalesce(devices.site_devices,0) END site_devices,
    NOT EXISTS(SELECT 1 FROM lead.analytics_report_dirty
        WHERE tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent() AND scope_key='live'
          AND day<%(trend_day)s) AS history_complete
FROM counts LEFT JOIN devices ON counts.day IS NOT DISTINCT FROM devices.day
    AND counts.platform IS NOT DISTINCT FROM devices.platform
"""


def read_promotion_dashboard(c, aid, period: PromotionRange, now, publications):
    today = now.astimezone(TZ).date()
    start = datetime.combine(today, time.min, TZ)
    trend_day = today - timedelta(days=29)
    pixel_connected = one(c, """SELECT EXISTS(
        SELECT 1 FROM lead.analytics_connections
        WHERE tenant_id=lead.ctx_tenant() AND agent_id=lead.ctx_agent()
          AND provider='lead_site' AND state='verified' AND last_pageview_at IS NOT NULL
    ) AS connected""")["connected"]
    today_view = period == "today"
    counts = {row["platform"]: row[period] for row in publications}
    # For today's roster only unmaterialized recent dates need raw reads.
    query_start = start if today_view else datetime.combine(trend_day, time.min, TZ)
    rows = all_rows(
        c, _TODAY_METRICS if today_view else _ALL_METRICS,
        {"now": now, "start": query_start, "trend_day": trend_day,
         "today_view": today_view, "snapshot_before": today if today_view else trend_day,
         "published_platforms": list(counts), "non_channels": _NON_CHANNELS, "pixel_connected": pixel_connected},
    )
    platforms = sorted(set(counts) | {row["platform"] for row in rows if row["platform"] is not None})
    history_complete = today_view or rows[0]["history_complete"]
    if today_view:
        points = [start + timedelta(hours=i) for i in range(24)]
        by_window = {(row["bucket"], row["platform"]): row for row in rows}
    else:
        points = [datetime.combine(trend_day + timedelta(days=i), time.min, TZ) for i in range(30)]
        by_window = {(row["day"], row["platform"]): row for row in rows}

    def metrics(window, platform=None):
        if window is None and not history_complete:
            return _UNKNOWN
        row = by_window.get((window, platform))
        return {
            key: None if not pixel_connected and key in ("pv", "site_devices") else row[key] if row else 0
            for key in _METRICS
        }

    channels = [
        {"platform": platform, "promotions": counts.get(platform, 0), **metrics(None, platform)}
        for platform in platforms
    ]
    channels.sort(key=lambda row: (-(row["clicks"] or 0), row["platform"]))

    def bucket(point):
        if point > now:
            return {"start": point.isoformat(), "totals": _UNKNOWN, "channels": [
                {"platform": platform, **_UNKNOWN} for platform in platforms
            ]}
        window = point if today_view else point.date()
        return {"start": point.isoformat(), "totals": metrics(window), "channels": [
            {"platform": platform, **metrics(window, platform)} for platform in platforms
        ]}

    return {
        "agent_id": aid, "range": period, "day": today.isoformat(), "pixel_connected": pixel_connected,
        "history_complete": history_complete,
        "channels": channels, "totals": {"promotions": sum(counts.values()), **metrics(None)},
        "buckets": [bucket(point) for point in points],
    }
