"""Standalone public analytics API. Only the existing visitor routes are installed."""

import asyncio
import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import analytics, analytics_ingest, conversions, pixel_events
from .identity_store import public_tenant
from .promotion import CODE_PATTERN
from .scope import tenant_scope
from .upload_limit import UploadLimit

app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None, redirect_slashes=False)
app.add_middleware(UploadLimit)
for router in (
    analytics.router,
    analytics_ingest.router,
    conversions.router,
    pixel_events.router,
):
    for route in router.routes:
        if route.path == "/lead.js" or route.path.startswith(("/collect/", "/r/")):
            app.router.routes.append(route)


@app.middleware("http")
async def visitor_scope(request, call_next):
    path = request.url.path
    reference = re.fullmatch(r"/r/(" + CODE_PATTERN + ")", path)
    connection = re.fullmatch(
        r"/collect/([0-9a-fA-F-]{36})(?:/(?:batch|test|receipts))?", path
    )
    if reference or connection:
        kind, key = (
            ("link", reference[1]) if reference else ("analytics", connection[1])
        )
        tenant = await asyncio.to_thread(public_tenant, kind, key)
        if not tenant:
            return JSONResponse({"detail": "Unknown reference"}, status_code=404)
        with tenant_scope(tenant, None):
            response = await call_next(request)
    else:
        response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def invalid(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(LookupError)
async def missing(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)
