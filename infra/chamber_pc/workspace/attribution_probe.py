"""Verify today's two read models against scoped live receipts, without generating traffic."""
import sys
import time
from datetime import datetime, time as midnight, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from lead_app.analytics_ingest import scoped_tx
from lead_app.analytics_reporting import overview
from lead_app.identity_store import agent_tenant
from lead_app.scope import service_scope, tenant_scope


def check(aid):
    tenant = agent_tenant(aid)
    with service_scope(), tenant_scope(tenant, None):
        zone = ZoneInfo("Asia/Shanghai")
        start = datetime.combine(datetime.now(zone).date(), midnight.min, zone)
        with scoped_tx(tenant, aid, "live") as c:
            raw = c.execute("""
                SELECT count(*) AS site, count(v.tracking_link_id) AS promotion,
                   count(DISTINCT (p.connection_id,p.anonymous_id)) AS site_devices,
                   count(DISTINCT (p.connection_id,p.anonymous_id))
                     FILTER (WHERE v.tracking_link_id IS NOT NULL) AS promotion_devices
                FROM lead.pixel_receipts p LEFT JOIN lead.visit_sessions v
                  ON (p.tenant_id,p.agent_id,p.visit_session_id)=(v.tenant_id,v.agent_id,v.id)
                WHERE p.scope_key='live' AND p.event_name='PageView' AND p.counted
                  AND p.occurred_at >= %s AND p.occurred_at < %s
            """, (start, start + timedelta(days=1))).fetchone()
        for scope in ("site", "promotion"):
            row = overview(UUID(aid), traffic=scope)["series"][-1]
            if (row["report_state"] != "current" or row["pv"] != raw[scope]
                    or row["site_devices"] != raw[scope + "_devices"]):
                return False
        print("Attribution verified", start.date(), "promotion PV", raw["promotion"],
              "site PV", raw["site"], flush=True)
        return True


if __name__ == "__main__":
    for _ in range(24):
        if check(sys.argv[1]):
            break
        time.sleep(3)
    else:
        raise SystemExit("Current acquisition and site counts could not be reconciled")
