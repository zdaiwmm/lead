"""Agent-scoped durable admission and atomic analytics projection.

Transport retries reuse the first accepted snapshot. PostgreSQL is the source of
truth; Temporal is a wake-up mechanism, never the authority for event uniqueness.
"""

import asyncio
import hashlib
import ipaddress
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from temporalio import activity

from .analytics_privacy import redact_payload
from .config import env
from .db import all_rows, audit, emit, j, one, require_agent, uid
from .db import pooled_tx as tx
from .pixel_events import (
    BrowserEvent,
    ServerEvent,
    browser_process,
    receive,
    test_scope,
    user_key,
)
from .scope import current_tenant, service_scope, tenant_scope

router = APIRouter()
VERSION = "analytics-clean-v1"
MAX_ATTEMPTS = 8
TASK_QUEUE = env().get("LEAD_ANALYTICS_TASK_QUEUE") or "lead-analytics"


def canonical_scope(value):
    if value == "live":
        return value
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise HTTPException(422, "数据环境无效") from None


def digest(value):
    return hashlib.sha256(
        json.dumps(
            jsonable_encoder(value),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    ).hexdigest()


@contextmanager
def scoped_tx(tenant, agent, scope):
    # There is deliberately no tenant-wide or missing-Agent fallback.
    try:
        UUID(str(tenant))
        UUID(str(agent))
        if scope != "live":
            UUID(scope)
    except ValueError:
        raise HTTPException(422, "Agent 或数据环境无效") from None
    with tx(str(agent), tenant_id=str(tenant)) as c:
        c.execute("SELECT set_config('app.analytics_scope',%s,true)", (scope,))
        yield c


def request_network(request):
    # X-Forwarded-For is untrusted. Proxy headers must be validated by the ASGI
    # server's explicit trusted-proxy configuration before populating client.
    try:
        ip = str(ipaddress.ip_address(request.client.host)) if request.client else None
    except ValueError:
        ip = None
    return {"client_ip": ip, "user_agent": request.headers.get("user-agent", "")[:1024]}


def frozen_browser(c, conn, body):
    from .admin_controls import attribution

    test = test_scope(c, conn, body)
    policy, version = attribution(c, str(conn["agent_id"]))
    definition = one(
        c,
        "SELECT * FROM lead.pixel_definitions WHERE connection_id=%s AND name=%s",
        (conn["id"], body.event_name),
    )
    marker = getattr(body, "ref", None)
    link = (
        one(
            c,
            "SELECT id FROM lead.tracking_links WHERE public_code=%s AND agent_id=%s AND allowed_host=%s AND state='active'",
            (marker, conn["agent_id"], urlsplit(conn["site_host"]).hostname),
        )
        if marker and not test
        else None
    )
    identity = user_key(conn["id"], body.user_id)
    if not identity and body.anonymous_id and not test:
        row = one(
            c,
            "SELECT user_key FROM lead.pixel_identities WHERE connection_id=%s AND anonymous_id=%s",
            (conn["id"], body.anonymous_id),
        )
        identity = row["user_key"] if row else None
    # Freeze the legacy receipt hash as well, preserving pre-upgrade retries.
    data = body.model_dump(
        mode="json", exclude={"key", "url", "ref", "lead_attribution", "click_token"}
    )
    if data.get("conversion_id") is None:
        data.pop("conversion_id", None)
    if data.get("click_id") is None:
        data.pop("click_id", None)
    fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return jsonable_encoder(
        {
            "connection": {
                k: conn[k] for k in ("id", "agent_id", "tenant_id", "site_host")
            },
            "test": {
                "id": test["id"],
                "definition_revisions": test["definition_revisions"],
            }
            if test
            else None,
            "definition": definition,
            "policy": policy,
            "policy_version": version,
            "identity_key": identity,
            "receipt_fingerprint": fingerprint,
            "link": link,
        }
    )


def admit_browser(conn, body, network, *, source="browser", legacy=False, inline=False):
    tenant, agent = str(conn["tenant_id"]), str(conn["agent_id"])
    # Serializes with the existing browser/server projection and identity binding.
    with tx(agent, tenant_id=tenant) as c:
        require_agent(c, agent)
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(conn["id"]),)
        )
        frozen = frozen_browser(c, conn, body)
        frozen["legacy"] = legacy
        scope = str(frozen["test"]["id"]) if frozen["test"] else "live"
        c.execute("SELECT set_config('app.analytics_scope',%s,true)", (scope,))
        from .analytics_clicks import resolve

        existing = one(
            c,
            """SELECT connection_id FROM lead.analytics_ingest_events WHERE event_id=%s
          UNION ALL SELECT connection_id FROM lead.analytics_receipt_tombstones WHERE event_id=%s""",
            (body.event_id, body.event_id),
        )
        # An authenticated old proof may identify an already accepted event after
        # expiry. Semantic fingerprint equality below still gates its replay;
        # an expired proof never authorizes a new event or a different payload.
        click = resolve(
            c,
            conn,
            body,
            frozen["test"],
            allow_expired=bool(
                existing and str(existing["connection_id"]) == str(conn["id"])
            ),
        )
        frozen["click"] = jsonable_encoder(click)
        if click:
            frozen["link"] = {"id": str(click["tracking_link_id"])}
        wire = body.model_dump(
            mode="json", exclude={"key", "test_code", "lead_attribution"}
        )
        old_wire = {
            k: v for k, v in wire.items() if k not in ("click_id", "click_token")
        }
        old_fingerprint = digest(
            {"source": source, "connection_id": str(conn["id"]), "event": old_wire}
        )
        # Fingerprints retain user-ID equality without retaining the raw ID.
        semantic = {
            **redact_payload(wire),
            "user_key": user_key(conn["id"], body.user_id),
        }
        fingerprint = digest(
            {"source": source, "connection_id": str(conn["id"]), "event": semantic}
        )
        # Do not persist an installation key, raw user ID or test credential.
        wire = redact_payload(wire)
        row = _admit(
            c,
            tenant,
            agent,
            scope,
            source,
            str(conn["id"]),
            body.event_id,
            body.event_name,
            wire,
            frozen,
            fingerprint,
            network,
            old_fingerprint=old_fingerprint,
        )
        if inline and not row["duplicate"]:
            # Preserve the SDK's synchronous attribution receipt. A dispatcher
            # must not steal the claim between admission and inline processing.
            c.execute(
                "UPDATE lead.outbox_events SET available_at=now()+interval '1 second' WHERE id=%s",
                (row["outbox_id"],),
            )
        return row


def admit_redirect(link, event_id, network, *, device=None):
    tenant, agent, scope = str(link["tenant_id"]), str(link["agent_id"]), "live"
    payload = {
        "event_id": event_id,
        "event_name": "ShortLinkClick",
        "tracking_link_id": str(link["id"]),
        "session_id": event_id,
        "anonymous_id": device,
    }
    frozen = {"link_id": str(link["id"]), "click_id": event_id}
    with scoped_tx(tenant, agent, scope) as c:
        require_agent(c, agent)
        from .admin_controls import attribution

        policy, version = attribution(c, agent)
        from .analytics_geo import lookup

        geo = lookup(network.get("client_ip"))
        frozen["geo"] = geo
        click = one(
            c,
            """INSERT INTO lead.analytics_clicks(tenant_id,agent_id,id,tracking_link_id,destination_host,occurred_at,expires_at,attribution_policy,attribution_version,country,geo)
          VALUES(%s,%s,%s,%s,%s,now(),now()+interval '90 days',%s,%s,%s,%s) RETURNING *""",
            (
                tenant,
                agent,
                event_id,
                link["id"],
                link["allowed_host"],
                j(policy),
                version,
                geo.get("country"),
                j(geo),
            ),
        )
        row = _admit(
            c,
            tenant,
            agent,
            scope,
            "redirect",
            None,
            event_id,
            "ShortLinkClick",
            payload,
            frozen,
            digest(payload),
            network,
        )
        from .analytics_clicks import mint

        return {**row, "click_token": mint(click)}


def _admit(
    c,
    tenant,
    agent,
    scope,
    source,
    connection,
    event_id,
    name,
    payload,
    frozen,
    fingerprint,
    network,
    *,
    old_fingerprint=None,
):
    now = datetime.now(timezone.utc)
    from .analytics_limits import policy

    cfg, _ = policy(c)
    frozen["retention"] = cfg.model_dump(
        include={"network_days", "payload_days", "clean_days"}
    )
    event_lock(c, tenant, agent, scope, event_id)
    tomb = one(
        c,
        "SELECT *,true AS archived FROM lead.analytics_receipt_tombstones WHERE event_id=%s",
        (event_id,),
    )
    if tomb:
        expected = (
            old_fingerprint
            if tomb["fingerprint_version"] == 1 and old_fingerprint
            else fingerprint
        )
        if tomb["payload_sha256"] != expected or str(
            tomb["connection_id"] or ""
        ) != str(connection or ""):
            raise HTTPException(409, {"code": "EVENT_ID_CONFLICT"})
        return {**tomb, "duplicate": True}
    ident, outbox = uid(), uid()
    frozen_hash = digest(frozen)
    row = one(
        c,
        """INSERT INTO lead.analytics_ingest_events
        (id,tenant_id,agent_id,scope_key,connection_id,source_kind,event_id,event_name,
         payload_sha256,snapshot_sha256,state,received_at,last_seen_at,outbox_id,payload_expires_at,tombstone_until,fingerprint_version,anonymous_id,session_id)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,2,%s,%s)
        ON CONFLICT(tenant_id,agent_id,scope_key,event_id) DO NOTHING RETURNING *""",
        (
            ident,
            tenant,
            agent,
            scope,
            connection,
            source,
            event_id,
            name,
            fingerprint,
            frozen_hash,
            now,
            now,
            outbox,
            now + timedelta(days=cfg.payload_days),
            now + timedelta(days=max(90, cfg.clean_days)),
            payload.get("anonymous_id"),
            payload.get("session_id"),
        ),
    )
    if not row:
        row = one(
            c,
            "SELECT * FROM lead.analytics_ingest_events WHERE event_id=%s FOR UPDATE",
            (event_id,),
        )
        expected = (
            old_fingerprint
            if row["fingerprint_version"] == 1 and old_fingerprint
            else fingerprint
        )
        if row["payload_sha256"] != expected or row["connection_id"] != (
            UUID(connection) if connection else None
        ):
            raise HTTPException(
                409, {"code": "EVENT_ID_CONFLICT", "message": "事件编号已用于不同内容"}
            )
        c.execute(
            "UPDATE lead.analytics_ingest_events SET delivery_attempts=delivery_attempts+1,last_seen_at=%s WHERE id=%s",
            (now, row["id"]),
        )
        return {**row, "duplicate": True}
    payload = {**payload, "occurred_at": payload.get("occurred_at") or now.isoformat()}
    c.execute(
        """INSERT INTO lead.analytics_event_payloads(tenant_id,agent_id,scope_key,ingest_id,payload,snapshot,storage_sha256,expires_at,privacy_version)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,1)""",
        (
            tenant,
            agent,
            scope,
            ident,
            j(payload),
            j(frozen),
            digest(payload),
            row["payload_expires_at"],
        ),
    )
    c.execute(
        """INSERT INTO lead.analytics_event_network(tenant_id,agent_id,scope_key,ingest_id,client_ip,user_agent,expires_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s)""",
        (
            tenant,
            agent,
            scope,
            ident,
            network.get("client_ip"),
            network.get("user_agent", "")[:1024],
            now + timedelta(days=cfg.network_days),
        ),
    )
    from .telemetry import trace_id, trace_parent

    c.execute(
        """INSERT INTO lead.outbox_events(id,tenant_id,agent_id,aggregate_type,aggregate_id,aggregate_version,event_type,schema_version,payload,trace_id,trace_parent)
        VALUES(%s,%s,%s,'analytics',%s,1,'analytics.ingest',1,%s,%s,%s)""",
        (
            outbox,
            tenant,
            agent,
            ident,
            j({"ingest_id": ident, "scope_key": scope, "processing_version": VERSION}),
            trace_id(),
            trace_parent(),
        ),
    )
    return {**row, "duplicate": False}


def event_lock(c, tenant, agent, scope, event_id):
    c.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        (f"analytics:{tenant}:{agent}:{scope}:{event_id}",),
    )


def work_args(row):
    return {
        "tenant_id": str(row["tenant_id"]),
        "agent_id": str(row["agent_id"]),
        "scope_key": row["scope_key"],
        "ingest_id": str(row["id"]),
        "outbox_id": str(row["outbox_id"]),
        "processing_version": row["processing_version"],
    }


def device_info(ua):
    ua = ua.lower()
    bot = bool(
        re.search(r"bot|crawler|spider|preview|slack|facebookexternalhit|headless", ua)
    )
    os = (
        "Android"
        if "android" in ua
        else "iOS"
        if re.search(r"iphone|ipad", ua)
        else "Windows"
        if "windows" in ua
        else "macOS"
        if "macintosh" in ua
        else "Linux"
        if "linux" in ua
        else "unknown"
    )
    browser = (
        "Edge"
        if "edg/" in ua
        else "Chrome"
        if "chrome/" in ua
        else "Firefox"
        if "firefox/" in ua
        else "Safari"
        if "safari/" in ua
        else "unknown"
    )
    return {
        "type": "bot"
        if bot
        else "mobile"
        if re.search(r"mobile|iphone|ipad|android", ua)
        else "desktop"
        if os != "unknown"
        else "unknown",
        "os": os,
        "browser": browser,
        "parser_version": "ua-v1",
    }


def _claim(args):
    with scoped_tx(args["tenant_id"], args["agent_id"], args["scope_key"]) as c:
        row = one(
            c,
            "SELECT * FROM lead.analytics_ingest_events WHERE id=%s FOR UPDATE",
            (args["ingest_id"],),
        )
        if (
            not row
            or str(row["outbox_id"]) != args["outbox_id"]
            or row["processing_version"] != args["processing_version"]
        ):
            return None, {"state": "scope_mismatch"}
        if row["state"] != "pending":
            return None, {"state": row["state"]}
        now = datetime.now(timezone.utc)
        if row["lease_until"] and row["lease_until"] > now:
            return None, {"state": "pending", "delay": 60}
        if row["next_attempt_at"] > now:
            return None, {
                "state": "pending",
                "delay": max(
                    1, int((row["next_attempt_at"] - now).total_seconds()) + 1
                ),
            }
        if row["cycle_attempts"] >= MAX_ATTEMPTS:
            c.execute(
                "UPDATE lead.analytics_ingest_events SET state='failed',lease_token=NULL,lease_until=NULL,error_code='CRASH_RETRY_EXHAUSTED' WHERE id=%s",
                (row["id"],),
            )
            _settle_outbox(c, row)
            return None, {"state": "failed"}
        lease = uid()
        c.execute(
            """UPDATE lead.analytics_ingest_events SET lease_token=%s,lease_until=now()+interval '60 seconds',
            processing_attempts=processing_attempts+1,cycle_attempts=cycle_attempts+1 WHERE id=%s""",
            (lease, row["id"]),
        )
        return lease, row


def process_ingest(args):
    from .identity_store import agent_tenant

    # The queue payload cannot grant a different tenant access to an Agent.
    if str(agent_tenant(args["agent_id"])) != str(args["tenant_id"]):
        return {"state": "scope_mismatch"}
    with tenant_scope(args["tenant_id"], None), service_scope():
        lease, row = _claim(args)
        if not lease:
            return row
        try:
            with scoped_tx(args["tenant_id"], args["agent_id"], args["scope_key"]) as c:
                data = one(
                    c,
                    "SELECT * FROM lead.analytics_processing_payload(%s,%s)",
                    (row["id"], lease),
                )
                network = (
                    one(
                        c,
                        "SELECT * FROM lead.analytics_processing_network(%s,%s)",
                        (row["id"], lease),
                    )
                    or {}
                )
            if not data:
                raise HTTPException(422, {"code": "PAYLOAD_EXPIRED"})
            if (
                digest(data["payload"]) != data["storage_sha256"]
                or digest(data["snapshot"]) != row["snapshot_sha256"]
            ):
                raise HTTPException(422, {"code": "PAYLOAD_INTEGRITY"})
            if args["processing_version"] != VERSION:
                raise HTTPException(422, {"code": "PROCESSING_VERSION_UNSUPPORTED"})
            with scoped_tx(args["tenant_id"], args["agent_id"], args["scope_key"]) as c:
                # Same lock order as admission: connection, then receipt. A
                # concurrent HTTP retry must not deadlock with this projection.
                if row["connection_id"]:
                    c.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (str(row["connection_id"]),),
                    )
                locked = one(
                    c,
                    "SELECT * FROM lead.analytics_ingest_events WHERE id=%s FOR UPDATE",
                    (row["id"],),
                )
                if (
                    str(locked["lease_token"]) != lease
                    or locked["state"] != "pending"
                    or locked["lease_until"] <= datetime.now(timezone.utc)
                ):
                    return {"state": "stale_lease"}
                payload, frozen = data["payload"], data["snapshot"]
                device = device_info(network.get("user_agent", ""))
                if row["source_kind"] == "server":
                    body = ServerEvent.model_validate(payload)
                    c.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                        (str(row["connection_id"]),),
                    )
                    result = receive(
                        c,
                        frozen["connection"],
                        body,
                        "server_verified",
                        test=frozen["test"],
                        received_at=row["received_at"],
                        frozen=frozen,
                    )
                    if not frozen["test"]:
                        c.execute(
                            "UPDATE lead.analytics_credentials SET last_received_at=now() WHERE connection_id=%s AND revoked_at IS NULL",
                            (row["connection_id"],),
                        )
                    context = {
                        "touchpoint_id": str(body.touchpoint_id)
                        if body.touchpoint_id
                        else None
                    }
                    properties, anon, session, conversion = (
                        body.properties,
                        body.anonymous_id,
                        body.session_id,
                        body.conversion_id,
                    )
                elif row["source_kind"] == "browser":
                    body = BrowserEvent.model_validate({**payload, "key": ""})
                    if frozen.get("legacy"):
                        result = legacy_browser_process(
                            c, frozen["connection"], body, row["received_at"], frozen
                        )
                    else:
                        if device["type"] == "bot":
                            raise HTTPException(422, {"code": "KNOWN_BOT"})
                        result = browser_process(
                            c,
                            frozen["connection"],
                            body,
                            received_at=row["received_at"],
                            frozen=frozen,
                        )
                    context = {
                        "site_origin": frozen["connection"]["site_host"],
                        "page_path": urlsplit(body.url).path or "/",
                        "ref": body.ref,
                        "touchpoint_id": result.get("attribution", {}).get(
                            "touchpoint_id"
                        ),
                    }
                    properties, anon, session, conversion = (
                        body.properties,
                        body.anonymous_id,
                        body.session_id,
                        body.conversion_id,
                    )
                else:
                    from .community_feedback import probe_cookie
                    from uuid import uuid5
                    probe_device = str(uuid5(UUID(str(row["agent_id"])), probe_cookie(row["agent_id"])))
                    probe = network.get("user_agent") == "LeadCoverageBot/1" and payload.get("anonymous_id") == probe_device
                    c.execute(
                        """INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,
                        trust_level,qualified,exclusion_reason,rule_version,started_at,last_seen_at,anonymous_id)
                        VALUES(%s,%s,%s,'redirect',%s,'redirect_observed',NULL,%s,'engaged-v1',%s,%s,%s)""",
                        (
                            row["tenant_id"],
                            row["agent_id"],
                            payload["tracking_link_id"],
                            payload["session_id"],
                            "coverage_probe" if probe else "known_bot" if device["type"] == "bot" else None,
                            row["received_at"],
                            row["received_at"],
                            payload.get("anonymous_id"),
                        ),
                    )
                    result = {"ok": True, "duplicate": False, "id": str(row["id"])}
                    context, properties, anon, session, conversion = (
                        {"tracking_link_id": payload["tracking_link_id"]},
                        {},
                        payload.get("anonymous_id"),
                        payload["session_id"],
                        None,
                    )
                from .analytics_geo import lookup

                receipt_row = (
                    one(
                        c,
                        "SELECT click_id FROM lead.pixel_receipts WHERE connection_id=%s AND scope_key=%s AND event_id=%s",
                        (row["connection_id"], row["scope_key"], row["event_id"]),
                    )
                    if row["connection_id"]
                    else None
                )
                attributed_click = (
                    (receipt_row or {}).get("click_id")
                    if row["connection_id"]
                    else frozen.get("click_id")
                )
                if row["source_kind"] == "server":
                    evidence = (
                        one(
                            c,
                            "SELECT country,geo FROM lead.analytics_clicks WHERE id=%s",
                            (attributed_click,),
                        )
                        if attributed_click
                        else None
                    )
                    geo = (
                        {**evidence["geo"], "basis": "attributed_click"}
                        if evidence and evidence["country"]
                        else {"country": None, "status": "server_origin_ignored"}
                    )
                else:
                    geo = frozen.get("geo") or lookup(network.get("client_ip"))
                context.update({"geo": geo, "source_kind": row["source_kind"]})
                c.execute(
                    """INSERT INTO lead.analytics_clean_events(tenant_id,agent_id,scope_key,ingest_id,event_id,event_name,
                    connection_id,occurred_at,received_at,anonymous_id,session_id,user_key,conversion_id,properties,context,device,
                    quality_flags,processing_version,result,expires_at,click_id,country_code,privacy_version)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1)""",
                    (
                        row["tenant_id"],
                        row["agent_id"],
                        row["scope_key"],
                        row["id"],
                        row["event_id"],
                        row["event_name"],
                        row["connection_id"],
                        payload["occurred_at"],
                        row["received_at"],
                        anon,
                        session,
                        frozen.get("identity_key"),
                        conversion,
                        j(properties),
                        j(context),
                        j(device),
                        (
                            []
                            if geo.get("country")
                            else ["geo_" + geo.get("status", "unavailable")]
                        )
                        + (["geo_stale"] if geo.get("stale") else [])
                        + (["known_bot"] if device["type"] == "bot" else []),
                        VERSION,
                        j(jsonable_encoder(result)),
                        row["received_at"]
                        + timedelta(
                            days=frozen.get("retention", {}).get("clean_days", 90)
                        ),
                        attributed_click,
                        geo.get("country"),
                    ),
                )
                emit(
                    c,
                    str(row["agent_id"]),
                    "analytics.cleaned",
                    {"ingest_id": str(row["id"]), "scope_key": row["scope_key"]},
                )
                c.execute(
                    "UPDATE lead.analytics_ingest_events SET state='processed',processed_at=clock_timestamp(),lease_token=NULL,lease_until=NULL,error_code=NULL,error_detail=NULL WHERE id=%s",
                    (row["id"],),
                )
                _settle_outbox(c, row)
            return {"state": "processed"}
        except (HTTPException, ValidationError) as error:
            code = (
                (
                    "EVENT_CONFLICT"
                    if error.status_code == 409
                    else error.detail.get("code", "EVENT_INVALID")
                    if isinstance(error.detail, dict)
                    else "EVENT_INVALID"
                )
                if isinstance(error, HTTPException)
                else "EVENT_INVALID"
            )
            # Error messages never echo submitted data or credentials.
            _finish_error(
                args,
                lease,
                "quarantined",
                code,
                str(error.detail)[:240]
                if isinstance(error, HTTPException) and isinstance(error.detail, str)
                else code,
            )
            return {"state": "quarantined", "error_code": code}
        except Exception:
            # A crash before this transaction is handled by lease expiry + reconciliation.
            return _finish_error(
                args, lease, None, "PROCESSING_UNAVAILABLE", "事件处理暂时不可用"
            )


def _finish_error(args, lease, state, code, detail):
    with scoped_tx(args["tenant_id"], args["agent_id"], args["scope_key"]) as c:
        row = one(
            c,
            "SELECT * FROM lead.analytics_ingest_events WHERE id=%s FOR UPDATE",
            (args["ingest_id"],),
        )
        if not row or str(row["lease_token"]) != lease or row["state"] != "pending":
            return {"state": "stale_lease"}
        state = state or (
            "failed" if row["cycle_attempts"] >= MAX_ATTEMPTS else "pending"
        )
        delay = min(300, 2 ** row["cycle_attempts"])
        c.execute(
            """UPDATE lead.analytics_ingest_events SET state=%s,error_code=%s,error_detail=%s,
            lease_token=NULL,lease_until=NULL,next_attempt_at=now()+make_interval(secs=>%s) WHERE id=%s""",
            (state, code, detail, delay, row["id"]),
        )
        if state != "pending":
            _settle_outbox(c, row)
        return {"state": state, "delay": delay}


def _settle_outbox(c, row):
    # Synchronous compatibility processing is also a successful local delivery.
    # Clearing a publisher's token fences a late acknowledgement after completion.
    c.execute(
        "UPDATE lead.outbox_events SET published_at=coalesce(published_at,now()),delivery_error=NULL,lease_token=NULL,lease_until=NULL WHERE id=%s",
        (row["outbox_id"],),
    )


def receipt(row, *, legacy=False, test_code=None):
    with scoped_tx(str(row["tenant_id"]), str(row["agent_id"]), row["scope_key"]) as c:
        current = one(
            c,
            "SELECT state,error_code,error_detail FROM lead.analytics_ingest_events WHERE id=%s",
            (row["id"],),
        )
        if not current:
            current = one(
                c,
                "SELECT state,error_code,NULL AS error_detail,result FROM lead.analytics_receipt_tombstones WHERE id=%s",
                (row["id"],),
            )
        clean = one(
            c,
            "SELECT result FROM lead.analytics_clean_events WHERE ingest_id=%s",
            (row["id"],),
        )
        if not clean:
            archived = one(
                c,
                "SELECT event->'result' AS result FROM lead.analytics_event_archives WHERE ingest_id=%s",
                (row["id"],),
            )
            clean = archived or (
                {"result": current["result"]}
                if current and current.get("result")
                else None
            )
    if not current:
        raise HTTPException(404, "事件回执不存在")
    if legacy and current["state"] == "quarantined":
        raise HTTPException(
            409 if current["error_code"] == "EVENT_CONFLICT" else 422,
            current["error_detail"] or current["error_code"],
        )
    if legacy and clean:
        result = clean["result"]
        result["duplicate"] = row["duplicate"] or result.get("duplicate", False)
        if result.get("attribution") and row["scope_key"] != "live":
            result["attribution"]["test_code"] = test_code
        return result
    return {
        "event_id": row["event_id"],
        "receipt_id": str(row["id"]),
        "status": current["state"],
        "duplicate": row["duplicate"],
        "error_code": current["error_code"],
    }


def collect_browser(conn, body, request):
    row = admit_browser(conn, body, request_network(request), inline=True)
    if not row.get("archived"):
        process_ingest(work_args(row))
    result = receipt(row, legacy=True, test_code=body.test_code)
    result.update(
        {
            "agent_id": str(conn["agent_id"]),
            "capabilities": {"batch": True, "receipts": True},
        }
    )
    return JSONResponse(
        jsonable_encoder(result),
        status_code=200 if result.get("ok") else 202,
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


def collect_server(body, credential, request):
    agent, tenant = str(credential["agent_id"]), str(credential["tenant_id"])
    with tx(agent, tenant_id=tenant) as c:
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (body.connection_id,),
        )
    if not conn:
        raise HTTPException(403, "网站连接不可用")
    from .analytics_limits import enforce

    enforce(
        conn,
        request,
        environment="test" if body.test_code else "production",
        server=True,
    )
    row = admit_browser(
        conn, body, request_network(request), source="server", inline=True
    )
    if not row.get("archived"):
        process_ingest(work_args(row))
    result = receipt(row, legacy=True)
    return JSONResponse(
        jsonable_encoder(result), status_code=200 if result.get("ok") else 202
    )


def collect_legacy(conn, body, request):
    # Old clients have no event_id. Stable content IDs preserve retry behavior
    # within their original session-grained pageview/engagement contract.
    wire = body.model_dump(mode="json", exclude={"key"})
    modern = BrowserEvent(
        key=body.key,
        event_id="legacy:" + digest(wire),
        event_name="PageView" if body.event == "pageview" else "Engagement",
        anonymous_id=body.session_id,
        session_id=body.session_id,
        url=body.url,
        ref=body.ref,
    )
    row = admit_browser(
        conn, modern, request_network(request), legacy=True, inline=True
    )
    if not row.get("archived"):
        process_ingest(work_args(row))
    return Response(
        status_code=204,
        headers={
            "Access-Control-Allow-Origin": conn["site_host"],
            "Vary": "Origin",
            "X-Lead-Engagement-Seconds": str(_legacy_policy(row)),
            "Access-Control-Expose-Headers": "X-Lead-Engagement-Seconds",
        },
    )


def _legacy_policy(row):
    with scoped_tx(str(row["tenant_id"]), str(row["agent_id"]), row["scope_key"]) as c:
        result = one(
            c,
            """SELECT result FROM lead.analytics_clean_events WHERE ingest_id=%s
              UNION ALL SELECT event->'result' FROM lead.analytics_event_archives WHERE ingest_id=%s
              UNION ALL SELECT result FROM lead.analytics_receipt_tombstones WHERE id=%s AND result IS NOT NULL LIMIT 1""",
            (row["id"], row["id"], row["id"]),
        )
    return result["result"]["engagement_seconds"] if result else 10


def legacy_browser_process(c, conn, body, observed, frozen):
    from .admin_controls import legacy_attribution

    aid, cid = str(conn["agent_id"]), str(conn["id"])
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (cid,))
    policy, version = frozen["policy"], frozen["policy_version"]
    existing = one(
        c,
        "SELECT * FROM lead.visit_sessions WHERE source=%s AND source_session_id=%s FOR UPDATE",
        ("site:" + cid, str(body.session_id)),
    )
    if not existing and body.event_name == "PageView":
        c.execute(
            """INSERT INTO lead.visit_sessions(tenant_id,agent_id,tracking_link_id,source,source_session_id,trust_level,
            qualified,rule_version,started_at,last_seen_at,attribution_policy,attribution_version)
            VALUES(%s,%s,%s,%s,%s,'client_observed',false,'engaged-v1',%s,%s,%s,%s)""",
            (
                current_tenant(),
                aid,
                frozen["link"]["id"] if frozen["link"] else None,
                "site:" + cid,
                str(body.session_id),
                observed,
                observed,
                j(policy),
                version,
            ),
        )
    elif existing:
        policy = legacy_attribution(existing["attribution_policy"])
        qualified = existing["qualified"] or (
            body.event_name == "Engagement"
            and (observed - existing["started_at"]).total_seconds()
            >= policy["engagement_seconds"]
        )
        c.execute(
            "UPDATE lead.visit_sessions SET last_seen_at=greatest(last_seen_at,%s),qualified=%s WHERE id=%s",
            (observed, qualified, existing["id"]),
        )
    c.execute(
        "UPDATE lead.analytics_connections SET state='verified',last_received_at=now() WHERE id=%s",
        (cid,),
    )
    return {
        "ok": True,
        "duplicate": False,
        "engagement_seconds": policy["engagement_seconds"],
    }


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    write_key: str = Field(max_length=500)
    agent_id: UUID
    environment: str = Field(pattern="^(production|test)$")
    test_code: str | None = Field(default=None, max_length=100)
    events: list[dict] = Field(min_length=1, max_length=20)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class ReceiptQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(max_length=500)
    url: str = Field(max_length=3000)
    anonymous_id: UUID
    session_id: UUID
    test_code: str | None = Field(default=None, max_length=100)
    event_ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/collect/{cid}/receipts")
def public_receipts(cid: UUID, body: ReceiptQuery, request: Request):
    from .analytics_limits import enforce
    from .security import valid_analytics_key

    with tx() as c:
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (cid,),
        )
    parsed = urlsplit(body.url)
    if (
        not conn
        or not valid_analytics_key(body.key, cid)
        or request.headers.get("origin") != conn["site_host"]
        or parsed.scheme + "://" + parsed.netloc != conn["site_host"]
    ):
        raise HTTPException(403)
    enforce(
        conn, request, events=0, environment="test" if body.test_code else "production"
    )
    with tx(str(conn["agent_id"]), tenant_id=str(conn["tenant_id"])) as c:
        test = test_scope(c, conn, body)
        scope = str(test["id"]) if test else "live"
        c.execute("SELECT set_config('app.analytics_scope',%s,true)", (scope,))
        result = []
        for event_id in body.event_ids:
            row = one(
                c,
                """SELECT *,false AS archived FROM lead.analytics_ingest_events
              WHERE event_id=%s AND connection_id=%s AND anonymous_id=%s AND session_id=%s""",
                (event_id, cid, body.anonymous_id, body.session_id),
            )
            if not row:
                row = one(
                    c,
                    """SELECT *,true AS archived FROM lead.analytics_receipt_tombstones
                  WHERE event_id=%s AND connection_id=%s AND anonymous_id=%s AND session_id=%s""",
                    (event_id, cid, body.anonymous_id, body.session_id),
                )
            if not row:
                result.append({"event_id": event_id, "status": "unknown"})
                continue
            status = receipt({**row, "duplicate": False})
            if row["state"] == "processed":
                status["result"] = receipt(
                    {**row, "duplicate": False}, legacy=True, test_code=body.test_code
                )
            result.append(status)
    return JSONResponse(
        jsonable_encoder({"agent_id": str(conn["agent_id"]), "events": result}),
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


@router.post("/collect/{cid}/batch")
async def collect_batch(cid: UUID, request: Request):
    from .security import valid_analytics_key

    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 256 * 1024:
            raise HTTPException(413, "批次超过 256 KiB")
    try:
        batch = Batch.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
    except (ValueError, ValidationError):
        raise HTTPException(422, "批次格式无效") from None
    with tx() as c:
        conn = one(
            c,
            "SELECT * FROM lead.analytics_connections WHERE id=%s AND state<>'disconnected'",
            (cid,),
        )
    if (
        not conn
        or str(conn["agent_id"]) != str(batch.agent_id)
        or not valid_analytics_key(batch.write_key, cid)
        or request.headers.get("origin") != conn["site_host"]
    ):
        raise HTTPException(403, "采集凭证或 Agent 不匹配")
    if (batch.environment == "test") != bool(batch.test_code):
        raise HTTPException(422, "测试环境必须使用有效测试会话")
    from .analytics_limits import enforce

    await asyncio.to_thread(
        enforce, conn, request, events=len(batch.events), environment=batch.environment
    )

    def accept():
        results = []
        for event in batch.events:
            try:
                if ("test_code" in event and event["test_code"] != batch.test_code) or (
                    "key" in event and event["key"] != batch.write_key
                ):
                    raise HTTPException(422, "批次中的事件与采集环境不匹配")
                body = BrowserEvent.model_validate(
                    {**event, "key": batch.write_key, "test_code": batch.test_code}
                )
                parsed = urlsplit(body.url)
                if parsed.scheme + "://" + parsed.netloc != conn["site_host"]:
                    raise HTTPException(403, "页面来源不匹配")
                row = admit_browser(conn, body, request_network(request))
                results.append(receipt(row))
            except (ValidationError, HTTPException) as error:
                results.append(
                    {
                        "event_id": event.get("event_id"),
                        "status": "rejected",
                        "error_code": "EVENT_ID_CONFLICT"
                        if isinstance(error, HTTPException) and error.status_code == 409
                        else "EVENT_INVALID",
                    }
                )
        return results

    results = await asyncio.to_thread(accept)
    return JSONResponse(
        {
            "agent_id": str(batch.agent_id),
            "environment": batch.environment,
            "events": results,
        },
        status_code=202,
        headers={"Access-Control-Allow-Origin": conn["site_host"], "Vary": "Origin"},
    )


@router.get("/api/agents/{aid}/analytics/ingestion")
def ingestion_status(aid: UUID, scope_key: str = "live"):
    from .analytics_reporting import can_manage, require_scope

    scope_key = canonical_scope(scope_key)
    with scoped_tx(current_tenant(), str(aid), scope_key) as c:
        require_scope(c, str(aid), scope_key)
        counts = all_rows(
            c,
            "SELECT state,count(*) AS count FROM lead.analytics_ingest_events GROUP BY state",
        )
        failures = all_rows(
            c,
            """SELECT id,event_id,event_name,state,error_code,received_at,processing_attempts,
              state='failed' AND payload_expires_at>now() AS retryable
            FROM lead.analytics_ingest_events WHERE state IN ('quarantined','failed') ORDER BY received_at DESC LIMIT 50""",
        )
        backlog = one(
            c,
            """SELECT count(*) FILTER(WHERE state='pending') AS pending,
          extract(epoch FROM now()-min(received_at) FILTER(WHERE state='pending')) AS oldest_pending_seconds,
          count(*) FILTER(WHERE state='pending' AND lease_until<now()) AS expired_leases,
          count(*) FILTER(WHERE processing_attempts>1) AS retried,
          percentile_cont(0.95) WITHIN GROUP(ORDER BY extract(epoch FROM processed_at-received_at))
            FILTER(WHERE processed_at>now()-interval '30 days') AS processing_p95_seconds
          FROM lead.analytics_ingest_events""",
        )
        outbox = one(
            c,
            """SELECT count(*) AS pending,count(*) FILTER(WHERE o.delivery_error IS NOT NULL) AS delivery_errors,
          extract(epoch FROM now()-min(o.occurred_at)) AS oldest_pending_seconds
          FROM lead.outbox_events o JOIN lead.analytics_ingest_events e ON
            (o.tenant_id,o.agent_id,o.id)=(e.tenant_id,e.agent_id,e.outbox_id)
          WHERE o.event_type='analytics.ingest' AND o.published_at IS NULL AND e.state='pending'""",
        )
        reports = all_rows(
            c,
            """SELECT day,state,attempts,error_code,changed_at FROM lead.analytics_report_dirty
          ORDER BY day DESC LIMIT 93""",
        )
        report_backlog = one(
            c,
            """SELECT count(*) AS pending_days,count(*) FILTER(WHERE state='failed') AS failed_days,
          min(changed_at) AS oldest_change_at FROM lead.analytics_report_dirty""",
        )
    return {
        "agent_id": str(aid),
        "scope_key": scope_key,
        "counts": counts,
        "failures": failures,
        "backlog": backlog,
        "outbox": outbox,
        "reporting": reports,
        "reporting_backlog": report_backlog,
        "can_manage": can_manage(),
    }


@router.post("/api/agents/{aid}/analytics/ingestion/{ingest_id}/retry")
def retry_ingestion(aid: UUID, ingest_id: UUID, scope_key: str = "live"):
    from .admin_permissions import require

    require("analytics.manage")
    scope_key = canonical_scope(scope_key)
    with scoped_tx(current_tenant(), str(aid), scope_key) as c:
        require_agent(c, str(aid))
        row = one(
            c,
            "SELECT * FROM lead.analytics_ingest_events WHERE id=%s FOR UPDATE",
            (ingest_id,),
        )
        if not row:
            raise HTTPException(404, "事件不存在")
        if row["state"] != "failed" or row["payload_expires_at"] <= datetime.now(
            timezone.utc
        ):
            raise HTTPException(409, "仅支持重试保留期内的暂时失败事件")
        c.execute(
            "UPDATE lead.analytics_ingest_events SET state='pending',cycle_attempts=0,next_attempt_at=now(),error_code=NULL,error_detail=NULL WHERE id=%s",
            (ingest_id,),
        )
        c.execute(
            "UPDATE lead.outbox_events SET published_at=NULL,available_at=now(),lease_token=NULL,lease_until=NULL,dispatch_generation=dispatch_generation+1 WHERE id=%s",
            (row["outbox_id"],),
        )
        audit(
            c,
            str(aid),
            "analytics.ingestion.retry",
            {"ingest_id": str(ingest_id), "scope_key": scope_key},
        )
    return {"ok": True, "receipt_id": str(ingest_id)}


@activity.defn
async def process_analytics_ingest(args: dict):
    return await asyncio.to_thread(process_ingest, args)
