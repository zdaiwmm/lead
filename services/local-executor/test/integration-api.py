"""Loopback-only disposable integration fixture; never mount in the product API."""
import json
import os
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from lead_app import domain, local_executor
from lead_app.db import all_rows, tx
from lead_app.local_executor_api import router

app = FastAPI()
app.include_router(router)
agent_id = domain.create_agent(runtime_engine="growth")


@app.middleware("http")
async def fixture_auth(request: Request, call_next):
    if request.url.path.startswith("/fixture/"):
        import hmac
        if not hmac.compare_digest(request.headers.get("x-fixture-token", ""), os.environ["LEAD_FIXTURE_TOKEN"]):
            return JSONResponse({"detail": "fixture_unavailable"}, status_code=401)
    return await call_next(request)


@app.post("/fixture/pairing")
def pairing():
    return {**local_executor.create_pairing(agent_id), "agent_id": str(agent_id)}


@app.post("/fixture/submit")
def submit(payload: dict):
    return json.loads(json.dumps(local_executor.submit(agent_id, payload.get("request_key", str(uuid4())), payload["payload"], session_key=payload["session_key"]), default=str))


@app.get("/fixture/request/{request_id}")
def status(request_id: str):
    with tx(agent_id) as connection:
        events = all_rows(connection, "SELECT method,params FROM lead.local_executor_events WHERE request_id=%s ORDER BY created_at,event_id", (request_id,))
    return json.loads(json.dumps({"request": local_executor.request_status(agent_id, request_id), "events": events}, default=str))


@app.post("/fixture/request/{request_id}/command")
def command(request_id: str, payload: dict):
    return local_executor.command(agent_id, request_id, payload["kind"], payload.get("payload", {}), payload.get("command_id", str(uuid4())))


@app.get("/fixture/health")
def health():
    return {"ready": True, "agent_id": str(agent_id)}
