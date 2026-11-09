"""Replay a fixed mixed partition on a disposable local PostgreSQL database.

Run with PYTHONPATH=services/api, an isolated LEAD_STATE_DIR and LEAD_DB_HOST
set to the absolute private socket directory. Transport latency is injected.
The Temporal acceptance fixture records the exact args; real ingestion consumes
them after the measured delivery/aggregation pair returns.
"""

import argparse
import asyncio
import json
import math
import os
import statistics
import time
from unittest.mock import patch

from lead_app import (
    analytics,
    analytics_delivery,
    analytics_ingest,
    analytics_reporting,
    budget,
    domain,
)
from lead_app.db import one, tx, uid
from lead_app.pixel_events import BrowserEvent
from lead_app.scope import current_tenant


async def run_once(parallel, delay):
    aid, tenant = domain.create_agent(), current_tenant()
    try:
        with patch.object(analytics, "resolve_public"):
            analytics.setup(aid, analytics.AnalyticsSetup(site_url="https://fixture.example/"))
        with tx(aid) as c:
            budget.apply_daily_limit(c, aid, 0)
            conn = one(c, "SELECT * FROM lead.analytics_connections")
        # One committed fact dirties the day. A distinct second event needs delivery.
        def admit():
            body = BrowserEvent(key="fixture", event_id=uid(), event_name="PageView", anonymous_id=uid(),
                                session_id=uid(), url="https://fixture.example/")
            return analytics_ingest.admit_browser(conn, body, {})

        ready = admit()
        with analytics_ingest.scoped_tx(tenant, aid, "live") as c:
            row = one(c, "SELECT * FROM lead.analytics_ingest_events WHERE id=%s", (ready["id"],))
        analytics_ingest.process_ingest(analytics_ingest.work_args(row))
        admit()
        starts, accepted = [], []
        refresh = analytics_reporting.refresh_scope

        class Transport:
            async def start_workflow(self, workflow, args, **options):
                starts.append(time.perf_counter())
                await asyncio.sleep(delay)
                assert not accepted and workflow == "AnalyticsIngestWorkflow"
                accepted.append(args)

        def delayed_refresh(*args):
            starts.append(time.perf_counter())
            time.sleep(delay)
            return refresh(*args)

        with patch.dict(os.environ, {"LEAD_PARALLEL_READ_AGENT_IDS":str(aid) if parallel else ""}), patch.object(
            analytics_reporting, "refresh_scope", delayed_refresh
        ):
            at = time.perf_counter()
            assert await analytics_delivery.deliver_partition(Transport(), tenant, aid, "live") == 2
            assert len(starts) == 2 and len(accepted) == 1
        # Replay the same delivery independently. Require two unique facts and
        # one final materialized day; include settling time in the workload.
        for _ in range(2):
            analytics_ingest.process_ingest(accepted[0])
        refresh(tenant, aid, "live")
        duration = time.perf_counter() - at
        with analytics_ingest.scoped_tx(tenant, aid, "live") as c:
            facts = one(c, "SELECT count(*) AS n FROM lead.analytics_clean_events")["n"]
            days = one(c, "SELECT count(*) AS n FROM lead.analytics_report_days WHERE source_rows>0")["n"]
            tokens = one(c, "SELECT coalesce(sum(units),0) AS n FROM lead.usage_events WHERE category='model_tokens'")["n"]
            assert (facts, days, tokens) == (2, 1, 0)
        return [start-at for start in starts], duration
    finally:
        domain.set_control(aid, "archive")


async def main(rounds, delay):
    if not os.path.isabs(os.environ.get("LEAD_DB_HOST", "")) or not os.environ.get("LEAD_STATE_DIR"):
        raise SystemExit("Configure a disposable local socket and private fixture state directory")
    samples = {False: ([], []), True: ([], [])}
    for i in range(rounds):
        for parallel in ([False, True] if i % 2 == 0 else [True, False]):
            waits, duration = await run_once(parallel, delay)
            samples[parallel][0].extend(waits)
            samples[parallel][1].append(duration)
    for parallel, (waits, elapsed) in samples.items():
        print(json.dumps(dict(parallel=parallel, rounds=rounds, injected_seconds=delay,
            ready_wait_p95_s=sorted(waits)[math.ceil(.95*len(waits))-1], workload_median_s=statistics.median(elapsed),
            distinct_facts=rounds*2, nonempty_days=rounds, model_tokens=0)))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=12)
    parser.add_argument("--delay", type=float, default=.15)
    args = parser.parse_args()
    asyncio.run(main(args.rounds, args.delay))
