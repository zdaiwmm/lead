"""Opt-in native child-agent proof using an existing isolated verification Agent."""
import asyncio
import hmac
import json
import os
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from lead_app import model
from lead_app.config import model_config
from lead_app.db import all_rows, one, tx
from lead_app.local_executor_api import router
from lead_app.local_executor_client import execution_context, run_model
from pydantic import BaseModel

agent_id = str(UUID(os.environ["LEAD_CONSUMER_AGENT_ID"]))
marker = os.environ["LEAD_SUBAGENT_MARKER"]
app = FastAPI()
app.include_router(router)
JOB = None
assert not model_config("openai")["key"] and not model_config("deepseek")["key"]
with tx(agent_id) as c:
    assert one(c, "SELECT id FROM lead.agents WHERE id=%s", (agent_id,))


def forbidden(*args, **kwargs):
    raise AssertionError("Legacy server model or API reservation was entered")


model.Agent = forbidden
model.reserve = forbidden


@app.middleware("http")
async def fixture_auth(request: Request, call_next):
    if request.url.path.startswith("/fixture/") and not hmac.compare_digest(request.headers.get("x-fixture-token", ""), os.environ["LEAD_FIXTURE_TOKEN"]):
        return JSONResponse({"detail": "fixture_unavailable"}, status_code=401)
    return await call_next(request)


class ChildReport(BaseModel):
    child_thread_id: str
    observed_marker: str
    observed_fact: str


async def run_job():
    context = execution_context.set({"key": "consumer-subagent:" + agent_id,
        "session_key": "consumer-subagent:" + agent_id, "agent_id": agent_id})
    try:
        value = await run_model(ChildReport, {
            "request": "Spawn exactly ONE native default child agent. Ask that child to read the relative file native-subagent-fixture.txt in the current workspace using a read-only native tool, and return its marker and fact. Do not read that file yourself. Wait for the child to complete, then report its actual thread ID, marker and fact. Do not browse or send any messages outside the native child-agent tool.",
            "managed_tools": {"public_web": False},
        }, "Verify the official native child-agent runtime. Parent and child must use gpt-6.1-sol with medium reasoning. Use the default native agent profile, not an explorer or a cheaper model. Use exactly one child and one harmless local fixture. Never write files, browse, contact an account or invent a child receipt.", agent_id=agent_id, purpose="research")
        assert value.observed_marker == marker
        assert value.observed_fact == "Seven blue notebooks belong to this harmless fixture."
        JOB.update(state="completed", output=value.model_dump())
    except BaseException as error:
        JOB.update(state="failed", error=str(error), error_type=type(error).__name__)
    finally:
        execution_context.reset(context)


@app.post("/fixture/start/subagent")
async def start():
    global JOB
    if JOB is None:
        JOB = {"state": "running"}
        asyncio.create_task(run_job())
    return JOB


@app.get("/fixture/status/subagent")
def status():
    with tx(agent_id) as c:
        requests = all_rows(c, "SELECT id,request_key,state,thread_id,turn_id,usage,result,session_key,task_id FROM lead.local_executor_requests ORDER BY created_at,id")
        billing = {"reservations": one(c, "SELECT count(*) n FROM lead.budget_reservations")["n"], "usage_events": one(c, "SELECT count(*) n FROM lead.usage_events")["n"]}
        events = all_rows(c, "SELECT request_id,method,params FROM lead.local_executor_events WHERE method IN ('request/started','thread/started','item/started','item/completed','thread/tokenUsage/updated','error','turn/completed','result') ORDER BY created_at,event_id")
        assert not one(c, "SELECT id FROM lead.actions")
    return json.loads(json.dumps({"job": JOB, "requests": requests, "billing": billing, "events": events, "agent_id": agent_id}, default=str))
