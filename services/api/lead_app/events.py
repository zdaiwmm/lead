"""Small authenticated change notifications; snapshots remain the source of truth."""

import asyncio
import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .db import all_rows, one, tx
from .scope import current_tenant

router = APIRouter()


def revision():
    with tx() as c:
        payload = {
            "agents": all_rows(
                c,
                "SELECT id,name,desired_state,health_state,config_revision,control_version,authorization_version,version FROM lead.agents WHERE desired_state<>'archived' ORDER BY id",
            ),
            "messages": one(
                c, "SELECT coalesce(sum(next_seq),0) AS n FROM lead.conversations"
            ),
            "jobs": all_rows(
                c,
                "SELECT status,count(*) AS n FROM lead.chat_jobs GROUP BY status ORDER BY status",
            ),
            "tasks": all_rows(
                c,
                "SELECT state,count(*) AS n FROM lead.tasks GROUP BY state ORDER BY state",
            ),
            "activity": one(
                c, "SELECT count(*) AS n,max(created_at) AS last FROM lead.activity"
            ),
            "browser": all_rows(
                c,
                "SELECT state,count(*) AS n,max(ended_at) AS last FROM lead.browser_steps GROUP BY state ORDER BY state",
            ),
            "runs": all_rows(
                c,
                "SELECT state,count(*) AS n FROM lead.browser_runs GROUP BY state ORDER BY state",
            ),
            "assets": one(
                c,
                "SELECT count(*) AS n,max(created_at) AS last FROM lead.assets WHERE state='ready'",
            ),
            "visits": one(
                c,
                "SELECT count(*) AS n,count(*) FILTER(WHERE qualified) AS qualified,max(last_seen_at) AS last FROM lead.visit_sessions",
            ),
            "conversions": one(c, "SELECT count(*) AS n FROM lead.conversion_events"),
        }
    return hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()


async def stream(request):
    previous = request.headers.get("last-event-id", "")
    yield "retry: 2000\n\n"
    for tick in range(55):
        if await request.is_disconnected():
            return
        from .auth import auth_mode

        if auth_mode() == "oidc":
            from .identity_store import lookup_session

            identity = await asyncio.to_thread(
                lookup_session, request.cookies.get("lead_session", "")
            )
            if not identity or identity["tenant_id"] != current_tenant():
                yield 'data: {"refresh":true}\n\n'
                return
        current = await asyncio.to_thread(revision)
        if current != previous:
            previous = current
            yield "id: " + current + '\ndata: {"refresh":true}\n\n'
        elif tick % 15 == 0:
            yield ": keepalive\n\n"
        await asyncio.sleep(1)


@router.get("/api/events")
async def events(request: Request):
    return StreamingResponse(
        stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )
