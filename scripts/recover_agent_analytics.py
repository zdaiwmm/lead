"""Drain one agent through the normal ingest leases and snapshot builder during queue recovery."""
import argparse
import json
import signal
import threading
from uuid import UUID

from lead_app.analytics_ingest import process_ingest, scoped_tx, work_args
from lead_app.analytics_reporting import refresh_scope
from lead_app.db import one, require_agent, tx
from lead_app.identity_store import agent_tenant
from lead_app.scope import service_scope, tenant_scope


def summary(tenant, aid):
    with scoped_tx(tenant, aid, "live") as c:
        return one(c, """SELECT
          (SELECT count(*) FROM lead.analytics_ingest_events WHERE state='pending') AS pending,
          (SELECT count(*) FROM lead.analytics_ingest_events WHERE state='failed') AS failed_events,
          (SELECT count(*) FROM lead.analytics_report_dirty) AS dirty_days,
          (SELECT count(*) FROM lead.analytics_report_dirty WHERE state='failed') AS failed_days,
          (SELECT max(refreshed_at) FROM lead.analytics_report_days) AS refreshed_at,
          (SELECT max(last_pageview_at) FROM lead.analytics_connections WHERE provider='lead_site') AS last_pageview""")


def ensure_live_day(c):
    c.execute("""INSERT INTO lead.analytics_report_dirty(tenant_id,agent_id,scope_key,day)
        SELECT lead.ctx_tenant(),lead.ctx_agent(),'live',(now() AT TIME ZONE 'Asia/Shanghai')::date
        WHERE NOT EXISTS (SELECT 1 FROM lead.analytics_report_days
            WHERE day=(now() AT TIME ZONE 'Asia/Shanghai')::date) ON CONFLICT DO NOTHING""")


def recover_scope(tenant, aid, scope):
    with scoped_tx(tenant, aid, scope) as c:
        if scope == "live":
            ensure_live_day(c)
        events = c.execute("""SELECT tenant_id,agent_id,id,outbox_id,scope_key,processing_version
            FROM lead.analytics_ingest_events WHERE state='pending' AND next_attempt_at<=now()
              AND (lease_until IS NULL OR lease_until<now())
            ORDER BY received_at,id LIMIT 12""").fetchall()
    settled = 0
    for event in events:
        result = process_ingest(work_args(event))
        settled += result["state"] in ("processed", "failed", "quarantined")
    return settled + len(refresh_scope(tenant, aid, scope, limit=3))


def run(aid, once):
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    with service_scope(), tenant_scope(agent_tenant(aid), None):
        tenant = agent_tenant(aid)
        with tx(aid) as c:
            require_agent(c, aid)
        print("ANALYTICS_BASELINE", json.dumps(summary(tenant, aid), default=str), flush=True)
        while not stopped.is_set():
            with tx(aid) as c:
                scopes = ["live", *(str(r["id"]) for r in c.execute("SELECT id FROM lead.pixel_tests WHERE agent_id=%s", (aid,)))]
            changed = 0
            for scope in scopes:
                if stopped.is_set():
                    break
                changed += recover_scope(tenant, aid, scope)
            if once or changed:
                print("ANALYTICS_PROGRESS", json.dumps(summary(tenant, aid), default=str), flush=True)
            if once:
                return
            stopped.wait(1 if changed else 15)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", required=True, type=UUID)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run(str(args.agent), args.once)
