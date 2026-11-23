"""Measured traffic for registered Chamber links, grouped by request slot."""

from datetime import datetime, time

from ..analytics_reporting import TZ
from ..db import all_rows, j, one
from .promotion_dashboard import (
    COUNTED_PAGE_VIEW,
    COUNTED_REDIRECT,
    TRAFFIC_COUNTS,
    PromotionRange,
    device_lower_bounds,
)

_RECORD_COUNTS = f"""
WITH selected AS (
    SELECT * FROM jsonb_to_recordset(%(links)s::jsonb) AS link(slot integer, link_id uuid, published_at timestamptz, start_at timestamptz)
), facts AS (
    SELECT link.slot, 'click' AS kind, v.anonymous_id::text AS device
    FROM selected link JOIN lead.visit_sessions v ON v.tracking_link_id=link.link_id
    WHERE v.tenant_id=lead.ctx_tenant() AND v.agent_id=lead.ctx_agent()
      AND v.started_at BETWEEN link.start_at AND %(now)s
      AND {COUNTED_REDIRECT}
    UNION ALL
    SELECT link.slot, 'page', p.connection_id::text||':'||p.anonymous_id::text
    FROM selected link
    JOIN lead.visit_sessions v ON v.tracking_link_id=link.link_id
    JOIN lead.pixel_receipts p ON (p.tenant_id,p.agent_id,p.visit_session_id)=(v.tenant_id,v.agent_id,v.id)
    WHERE %(pixel_connected)s AND p.tenant_id=lead.ctx_tenant() AND p.agent_id=lead.ctx_agent()
      AND p.connection_id=%(connection)s::uuid
      AND v.started_at BETWEEN link.published_at AND %(now)s
      AND p.occurred_at BETWEEN link.start_at AND %(now)s AND p.received_at<=%(now)s
      AND {COUNTED_PAGE_VIEW}
)
SELECT slot,{TRAFFIC_COUNTS} FROM facts GROUP BY slot
"""


def read_link_report(c, connection, period: PromotionRange, now, links):
    """Read known registered slots. Input rows contain trusted link IDs."""
    if not links:
        return []
    today_start = datetime.combine(now.astimezone(TZ).date(), time.min, TZ)
    selected = [
        {
            "slot": slot,
            "link_id": str(link_id),
            "published_at": published_at.isoformat(),
            "start_at": (max(published_at, today_start) if period == "today" else published_at).isoformat(),
        }
        for slot, link_id, published_at in links
    ]
    pixel_connected = one(c, """SELECT last_pageview_at IS NOT NULL AS connected
        FROM lead.analytics_connections WHERE id=%s::uuid""", (connection,))["connected"]
    rows = all_rows(c, _RECORD_COUNTS, {
        "connection": connection, "links": j(selected), "now": now, "pixel_connected": pixel_connected,
    })
    traffic = {row["slot"]: row for row in rows}
    result = []
    for slot in sorted({slot for slot, _, _ in links}):
        row = traffic.get(slot)
        counts = {key: row[key] if row else 0 for key in ("clicks", "click_devices", "pv", "site_devices")}
        if not pixel_connected:
            counts.update(pv=None, site_devices=None)
        bounds = device_lower_bounds(row, counts, pixel_connected=pixel_connected) if row else {}
        result.append({
            "slot": slot, **counts,
            "click_devices_min": bounds.get("click_devices_min"),
            "site_devices_min": bounds.get("site_devices_min"),
        })
    return result
