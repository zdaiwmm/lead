"""Small authenticated change notifications; snapshots remain the source of truth."""

import asyncio
import hashlib
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .config import env
from .db import all_rows
from .db import pooled_tx as tx
from .scope import current_tenant, tenant_scope

router = APIRouter()


def revision():
    with tx() as c:
        payload = all_rows(c, "SELECT agent_id,version FROM lead.ui_revisions ORDER BY agent_id")

    return hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True).encode()
    ).hexdigest()


async def stream(request):
    poll_seconds = max(1, min(10, float(env().get("LEAD_UI_POLL_SECONDS") or 1)))
    previous = request.headers.get("last-event-id", "")
    yield "retry: 2000\n\n"
    for tick in range(max(1, int(55 / poll_seconds))):
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
            from .admin_permissions import resolve

            with tenant_scope(identity["tenant_id"], identity):
                identity.update(await asyncio.to_thread(resolve, identity))
                current = await asyncio.to_thread(revision)
        else:
            current = await asyncio.to_thread(revision)
        if current != previous:
            previous = current
            yield "id: " + current + '\ndata: {"refresh":true}\n\n'
        elif tick % max(1, int(15 / poll_seconds)) == 0:
            yield ": keepalive\n\n"
        await asyncio.sleep(poll_seconds)


@router.get("/api/events")
async def events(request: Request):
    return StreamingResponse(
        stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
    )
