"""Bounded maintenance under a non-bypass role and explicit Agent/environment RLS.

Archives retain sanitized analytical facts. Compact tombstones retain idempotency;
business receipt projections are preserved for report rebuilds and order dedup.
"""

from contextlib import contextmanager

import psycopg
from fastapi.encoders import jsonable_encoder
from psycopg.rows import dict_row

from .analytics_ingest import digest, event_lock
from .analytics_privacy import redact_context, redact_payload
from .config import database_kwargs
from .db import all_rows, j, one


@contextmanager
def maintenance_tx(tenant, agent, scope):
    from uuid import UUID

    UUID(str(tenant))
    UUID(str(agent))
    if scope != "live":
        UUID(scope)
    with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
        c.execute("SET LOCAL ROLE lead_analytics_maintenance")
        c.execute("SET LOCAL statement_timeout='10s'")
        c.execute("SET LOCAL lock_timeout='1s'")
        for name, value in [
            ("app.tenant_id", tenant),
            ("app.agent_id", agent),
            ("app.analytics_scope", scope),
        ]:
            c.execute("SELECT set_config(%s,%s,true)", (name, str(value)))
        yield c


def maintain_scope(tenant, agent, scope="live", limit=100):
    if not 1 <= limit <= 1000:
        raise ValueError("invalid batch size")
    counts = {
        "redacted": 0,
        "network": 0,
        "payloads": 0,
        "archived": 0,
        "tombstones": 0,
    }
    with maintenance_tx(tenant, agent, scope) as c:
        _backfill_geography(c, limit)
        # Repairs pre-upgrade URL storage without changing wire dedup hashes or
        # frozen business semantics. Active processors are fenced by their lease.
        payloads = all_rows(
            c,
            """SELECT p.ingest_id,p.payload FROM lead.analytics_event_payloads p
          JOIN lead.analytics_ingest_events e ON e.id=p.ingest_id
          WHERE (e.lease_until IS NULL OR e.lease_until<now())
            AND p.privacy_version<1
          ORDER BY p.ingest_id LIMIT %s FOR UPDATE OF e,p SKIP LOCKED""",
            (limit,),
        )
        for row in payloads:
            safe = redact_payload(row["payload"])
            c.execute(
                "UPDATE lead.analytics_event_payloads SET payload=%s,storage_sha256=%s,privacy_version=1 WHERE ingest_id=%s",
                (j(safe), digest(safe), row["ingest_id"]),
            )
            counts["redacted"] += int(safe != row["payload"])
        for row in all_rows(
            c,
            "SELECT ingest_id,context FROM lead.analytics_clean_events WHERE privacy_version<1 LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        ):
            c.execute(
                "UPDATE lead.analytics_clean_events SET context=%s,privacy_version=1 WHERE ingest_id=%s",
                (j(redact_context(row["context"])), row["ingest_id"]),
            )
        counts["network"] = c.execute(
            """DELETE FROM lead.analytics_event_network WHERE ingest_id IN
          (SELECT ingest_id FROM lead.analytics_event_network WHERE expires_at<=now() LIMIT %s FOR UPDATE SKIP LOCKED)""",
            (limit,),
        ).rowcount
        expired = all_rows(
            c,
            """SELECT e.id,e.outbox_id,e.state FROM lead.analytics_ingest_events e
          JOIN lead.analytics_event_payloads p ON p.ingest_id=e.id WHERE p.expires_at<=now()
          AND (e.lease_until IS NULL OR e.lease_until<now()) LIMIT %s FOR UPDATE OF e SKIP LOCKED""",
            (limit,),
        )
        for row in expired:
            if row["state"] == "pending":
                c.execute(
                    "UPDATE lead.analytics_ingest_events SET state='quarantined',error_code='PAYLOAD_EXPIRED',error_detail='原始事件已超过保留期',lease_token=NULL,lease_until=NULL WHERE id=%s",
                    (row["id"],),
                )
                c.execute(
                    "UPDATE lead.outbox_events SET published_at=coalesce(published_at,now()),lease_token=NULL,lease_until=NULL WHERE id=%s",
                    (row["outbox_id"],),
                )
            counts["payloads"] += c.execute(
                "DELETE FROM lead.analytics_event_payloads WHERE ingest_id=%s",
                (row["id"],),
            ).rowcount
        clean = all_rows(
            c,
            "SELECT * FROM lead.analytics_clean_events WHERE expires_at<=now() ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED",
            (limit,),
        )
        for row in clean:
            fact = jsonable_encoder(row)
            fact["context"] = redact_context(fact["context"])
            c.execute(
                """INSERT INTO lead.analytics_event_archives(tenant_id,agent_id,scope_key,ingest_id,event_id,connection_id,click_id,occurred_at,event)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                (
                    tenant,
                    agent,
                    scope,
                    row["ingest_id"],
                    row["event_id"],
                    row["connection_id"],
                    row["click_id"],
                    row["occurred_at"],
                    j(fact),
                ),
            )
            archived = one(
                c,
                "SELECT event FROM lead.analytics_event_archives WHERE ingest_id=%s",
                (row["ingest_id"],),
            )
            if not archived or archived["event"] != fact:
                raise RuntimeError("ARCHIVE_INTEGRITY")
            counts["archived"] += c.execute(
                "DELETE FROM lead.analytics_clean_events WHERE ingest_id=%s",
                (row["ingest_id"],),
            ).rowcount
    # Event lock precedes receipt lock, matching admission, and each deletion is
    # its own transaction so a crash cannot expose a gap in deduplication.
    with maintenance_tx(tenant, agent, scope) as c:
        candidates = all_rows(
            c,
            "SELECT event_id FROM lead.analytics_ingest_events WHERE tombstone_until<=now() AND state<>'pending' LIMIT %s",
            (limit,),
        )
    for item in candidates:
        with maintenance_tx(tenant, agent, scope) as c:
            event_lock(c, tenant, agent, scope, item["event_id"])
            row = one(
                c,
                "SELECT * FROM lead.analytics_ingest_events WHERE event_id=%s AND tombstone_until<=now() AND state<>'pending' FOR UPDATE SKIP LOCKED",
                (item["event_id"],),
            )
            if not row:
                continue
            if one(
                c,
                "SELECT ingest_id FROM lead.analytics_event_payloads WHERE ingest_id=%s UNION ALL SELECT ingest_id FROM lead.analytics_clean_events WHERE ingest_id=%s UNION ALL SELECT ingest_id FROM lead.analytics_event_network WHERE ingest_id=%s",
                (row["id"], row["id"], row["id"]),
            ):
                continue
            archive = one(
                c,
                "SELECT event->'result' AS result FROM lead.analytics_event_archives WHERE ingest_id=%s",
                (row["id"],),
            )
            if row["state"] == "processed" and not archive:
                continue
            c.execute(
                """INSERT INTO lead.analytics_receipt_tombstones(tenant_id,agent_id,scope_key,event_id,id,connection_id,source_kind,event_name,payload_sha256,fingerprint_version,state,error_code,received_at,anonymous_id,session_id,result)
              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                tuple(
                    row[k]
                    for k in (
                        "tenant_id",
                        "agent_id",
                        "scope_key",
                        "event_id",
                        "id",
                        "connection_id",
                        "source_kind",
                        "event_name",
                        "payload_sha256",
                        "fingerprint_version",
                        "state",
                        "error_code",
                        "received_at",
                        "anonymous_id",
                        "session_id",
                    )
                )
                + (j(archive["result"]) if archive else None,),
            )
            c.execute(
                "DELETE FROM lead.analytics_ingest_events WHERE id=%s", (row["id"],)
            )
            c.execute(
                "DELETE FROM lead.outbox_events WHERE id=%s AND event_type='analytics.ingest'",
                (row["outbox_id"],),
            )
            counts["tombstones"] += 1
    with maintenance_tx(tenant, agent, scope) as c:
        c.execute("DELETE FROM lead.analytics_limit_buckets WHERE expires_at<now()")
        c.execute("DELETE FROM lead.analytics_limit_counters WHERE day<current_date-90")
    return counts


def _backfill_geography(c, limit):
    from .analytics_geo import lookup

    candidates = all_rows(
        c,
        """SELECT fact.*,n.client_ip,e.source_kind
      FROM lead.analytics_clean_events fact
      JOIN lead.analytics_event_network n ON n.ingest_id=fact.ingest_id
      JOIN lead.analytics_ingest_events e ON e.id=fact.ingest_id
      WHERE NOT (fact.context ? 'geo') AND n.expires_at>now()
      ORDER BY CASE e.source_kind WHEN 'redirect' THEN 0 WHEN 'browser' THEN 1 ELSE 2 END,fact.occurred_at
      LIMIT %s FOR UPDATE OF fact SKIP LOCKED""",
        (limit,),
    )
    for row in candidates:
        if row["source_kind"] == "server":
            evidence = (
                one(
                    c,
                    "SELECT geo FROM lead.analytics_clicks WHERE id=%s",
                    (row["click_id"],),
                )
                if row["click_id"]
                else None
            )
            geo = (
                {**evidence["geo"], "basis": "attributed_click"}
                if evidence and evidence["geo"].get("country")
                else {"country": None, "status": "server_origin_ignored"}
            )
        else:
            geo = lookup(row["client_ip"])
        if geo.get("status") in ("database_unavailable", "disabled"):
            continue
        context = {**row["context"], "geo": geo, "source_kind": row["source_kind"]}
        flags = [f for f in row["quality_flags"] if not f.startswith("geo_")]
        if not geo.get("country"):
            flags.append("geo_" + geo.get("status", "unavailable"))
        c.execute(
            "UPDATE lead.analytics_clean_events SET country_code=%s,context=%s,quality_flags=%s WHERE ingest_id=%s",
            (geo.get("country"), j(context), flags, row["ingest_id"]),
        )
        if row["source_kind"] == "redirect":
            c.execute(
                "UPDATE lead.analytics_clicks SET country=%s,geo=%s WHERE id=%s",
                (geo.get("country"), j(geo), row["event_id"]),
            )
        c.execute(
            "SELECT lead.analytics_mark_report_dirty(%s,%s,%s,%s)",
            (row["tenant_id"], row["agent_id"], row["scope_key"], row["occurred_at"]),
        )


def maintain_all():
    # Enumerate only analytics partition identities. Old social/browser profiles
    # without analytics data do not incur one cleanup transaction per profile.
    with psycopg.connect(**database_kwargs(True), row_factory=dict_row) as c:
        partitions = all_rows(
            c,
            """SELECT DISTINCT tenant_id,agent_id,scope_key FROM lead.analytics_ingest_events
          UNION SELECT tenant_id,agent_id,'live' FROM lead.analytics_limit_buckets WHERE expires_at<now()
          UNION SELECT tenant_id,agent_id,'live' FROM lead.analytics_limit_counters WHERE day<current_date-90""",
        )
    failures = []
    for part in partitions:
        try:
            maintain_scope(
                str(part["tenant_id"]), str(part["agent_id"]), part["scope_key"]
            )
        except Exception:
            failures.append(str(part["agent_id"]))
    if failures:
        raise RuntimeError(
            f"Analytics maintenance failed for {len(failures)} partitions"
        )
