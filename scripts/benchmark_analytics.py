"""Bounded real HTTP/Temporal/PG benchmark against a dedicated analytics database.

Creates only paused verification Agents. No model, social or notification delivery.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from unittest.mock import patch

from lead_app import domain
from lead_app.config import STATE, database_kwargs
from lead_app.db import one, tx, uid
from lead_app.security import token


def percentile(values, fraction=0.95):
    return (
        sorted(values)[min(len(values) - 1, int(len(values) * fraction))]
        if values
        else None
    )


async def run(args):
    if not database_kwargs()["dbname"].startswith("lead_analytics_bench_"):
        raise ValueError("Dedicated benchmark database required")
    if args.base not in ("http://127.0.0.1:18190", "http://127.0.0.1:18191"):
        raise ValueError("Dedicated loopback benchmark listener required")
    agents = []
    accepted = []
    failures = []
    queries = []
    latencies = []
    stages = []
    headers = {"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"}
    async with httpx.AsyncClient(
        base_url=args.base, timeout=30, limits=httpx.Limits(max_connections=50)
    ) as client:
        client.cookies.set("lead_session", token("owner"))
        with patch.object(domain, "emit"):
            for label in ("A", "B"):
                aid = domain.create_agent()
                with tx(aid) as c:
                    c.execute(
                        "UPDATE lead.agents SET name=%s,desired_state='paused' WHERE id=%s",
                        ("数分压测 " + label, aid),
                    )
                response = await client.post(
                    f"/api/agents/{aid}/analytics",
                    headers=headers,
                    json={"site_url": "http://127.0.0.1:18787"},
                )
                response.raise_for_status()
                cid = response.json()["connection"]["id"]
                policy = (
                    await client.get(f"/api/agents/{aid}/analytics/policy")
                ).json()
                response = await client.post(
                    f"/api/agents/{aid}/analytics/policy",
                    headers=headers,
                    json={
                        **policy["policy"],
                        "expected_revision": policy["revision"],
                        "events_per_minute": 60000,
                        "event_burst": 5000,
                        "ip_events_per_minute": 0,
                    },
                )
                response.raise_for_status()
                agents.append(
                    {
                        "aid": aid,
                        "cid": cid,
                        "anon": uid(),
                        "session": uid(),
                        "key": token("analytics:" + cid),
                    }
                )
        stop = asyncio.Event()

        async def observe():
            while not stop.is_set():
                for agent in agents:
                    start = time.perf_counter()
                    try:
                        response = await client.get(
                            f"/api/agents/{agent['aid']}/analytics/summary"
                        )
                        if (
                            response.status_code != 200
                            or response.json().get("agent_id") != agent["aid"]
                        ):
                            failures.append("summary_scope_or_status")
                        queries.append((time.perf_counter() - start) * 1000)
                    except Exception:
                        failures.append("summary_network")
                await asyncio.sleep(0.2)

        observer = asyncio.create_task(observe())

        async def send(agent, items):
            start = time.perf_counter()
            response = await client.post(
                f"/collect/{agent['cid']}/batch",
                headers={"Origin": "http://127.0.0.1:18787"},
                json={
                    "agent_id": agent["aid"],
                    "write_key": agent["key"],
                    "environment": "production",
                    "events": items,
                },
            )
            latencies.append((time.perf_counter() - start) * 1000)
            if response.status_code != 202:
                failures.append("ingest_http_" + str(response.status_code))
                return
            result = response.json()
            for item in result["events"]:
                if item.get("status") not in ("pending", "processed"):
                    failures.append("ingest_rejected")
                else:
                    accepted.append((agent["aid"], item["event_id"]))

        for rate in args.rates:
            before = len(accepted)
            begin = time.perf_counter()
            jobs = []
            for tick in range(args.seconds * 10):
                for agent in agents:
                    count = (
                        rate // 20
                    )  # rate is total across both Agents, 10 ticks/sec.
                    items = [
                        {
                            "event_id": uid(),
                            "event_name": "PageView",
                            "anonymous_id": agent["anon"],
                            "session_id": agent["session"],
                            "url": "http://127.0.0.1:18787/",
                            "properties": {},
                        }
                        for _ in range(count)
                    ]
                    for offset in range(0, len(items), 20):
                        jobs.append(
                            asyncio.create_task(
                                send(agent, items[offset : offset + 20])
                            )
                        )
                await asyncio.sleep(
                    max(0, begin + (tick + 1) / 10 - time.perf_counter())
                )
            await asyncio.gather(*jobs)
            with tx(agents[0]["aid"]) as c:
                c.execute("SELECT set_config('app.analytics_scope','live',true)")
                pending = one(
                    c,
                    "SELECT count(*) n FROM lead.analytics_ingest_events WHERE state='pending'",
                )["n"]
            stages.append(
                {
                    "target_events_per_second": rate,
                    "accepted": len(accepted) - before,
                    "seconds": round(time.perf_counter() - begin, 3),
                    "a_pending_at_end": pending,
                }
            )
        drain_started = time.monotonic()
        deadline = drain_started + args.drain_timeout
        totals = []
        while time.monotonic() < deadline:
            totals = []
            for agent in agents:
                with tx(agent["aid"]) as c:
                    c.execute("SELECT set_config('app.analytics_scope','live',true)")
                    row = one(
                        c,
                        "SELECT count(*) n,count(*) FILTER(WHERE state='processed') processed,count(*) FILTER(WHERE state IN ('failed','quarantined')) bad FROM lead.analytics_ingest_events",
                    )
                    totals.append(row)
            if sum(r["processed"] for r in totals) == len(accepted):
                break
            await asyncio.sleep(0.5)
        stop.set()
        await observer
        expected = len(accepted)
        if sum(r["processed"] for r in totals) != expected or any(
            r["bad"] for r in totals
        ):
            failures.append("not_fully_processed")
        # Verify independent A/B accounting from the actual persisted facts.
        for agent, row in zip(agents, totals):
            count = sum(aid == agent["aid"] for aid, _ in accepted)
            if row["processed"] != count:
                failures.append("agent_count_mismatch")
        result = {
            "ok": not failures,
            "database": database_kwargs()["dbname"],
            "stages": stages,
            "accepted_events": expected,
            "processed_events": sum(r["processed"] for r in totals),
            "drain_seconds": round(time.monotonic() - drain_started, 3),
            "per_agent": totals,
            "failures": failures,
            "http_batch_p95_ms": percentile(latencies),
            "summary_p95_ms": percentile(queries),
            "summary_samples": len(queries),
            "hardware": {
                "cpu_count": psutil.cpu_count(),
                "ram_gib": round(psutil.virtual_memory().total / 1024**3, 1),
            },
            "scope": "local loopback, synthetic events, 4 analytics activity slots; not a production capacity guarantee",
        }
        for agent in agents:
            with tx(agent["aid"]) as c:
                c.execute(
                    "UPDATE lead.agents SET desired_state='archived' WHERE id=%s",
                    (agent["aid"],),
                )
        output = STATE / "analytics-verification"
        output.mkdir(exist_ok=True)
        (output / (args.label + ".json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
        print(json.dumps(result, ensure_ascii=False))
        if failures:
            raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", default="benchmark")
    parser.add_argument("--base", default="http://127.0.0.1:18190")
    parser.add_argument("--rates", nargs="+", type=int, default=[20, 60, 120, 240])
    parser.add_argument("--seconds", type=int, default=8)
    parser.add_argument("--drain-timeout", type=int, default=120)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 60 or any(
        not 20 <= r <= 1000 or r % 20 for r in args.rates
    ):
        raise SystemExit(
            "Bounded rates must be multiples of 20, 20..1000, duration 1..60 seconds"
        )
    asyncio.run(run(args))
