"""Disposable real-model consumer verification API. Never mount in the product."""
import asyncio
import hmac
import json
import os
from datetime import timedelta
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from lead_app import domain, local_executor, model, website_brief
from lead_app.config import model_config
from lead_app.db import all_rows, j, one, tx
from lead_app.fetch import fetch_product
from lead_app.growth import agent as decision_agent
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.store import create_experiment, submit_work
from lead_app.local_executor_api import router
from lead_app.local_executor_client import ExecutorUnavailable, execution_context
from pydantic import BaseModel, ValidationError

app = FastAPI()
app.include_router(router)
client_id = str(uuid4())
agent_id = domain.create_agent("website-draft:" + client_id, draft_client_id=client_id, runtime_engine="growth")
SOURCE_URL = "https://www.python.org/about/"
JOBS = {}


def forbidden(*args, **kwargs):
    raise AssertionError("Legacy server model or API reservation was entered")


model.Agent = forbidden
model.reserve = forbidden
assert not model_config("openai")["key"] and not model_config("deepseek")["key"]

# A protocol/schema failure ends this verification's original request. Normal
# business-command validation and its original repair remain unchanged.
_original_decision_model = decision_agent.run_model


class NativeProtocolStop(BaseException):
    pass


async def stop_on_protocol_failure(*args, **kwargs):
    try:
        return await _original_decision_model(*args, **kwargs)
    except (ExecutorUnavailable, ValidationError, json.JSONDecodeError) as error:
        raise NativeProtocolStop(str(error)) from error


decision_agent.run_model = stop_on_protocol_failure


@app.middleware("http")
async def fixture_auth(request: Request, call_next):
    if request.url.path.startswith("/fixture/") and not hmac.compare_digest(request.headers.get("x-fixture-token", ""), os.environ["LEAD_FIXTURE_TOKEN"]):
        return JSONResponse({"detail": "fixture_unavailable"}, status_code=401)
    return await call_next(request)


def output(value):
    return json.loads(json.dumps(value, default=str))


@app.get("/fixture/health")
def health():
    return {"ready": True, "agent_id": str(agent_id), "model_api_keys": False}


@app.post("/fixture/pairing")
def pairing():
    return local_executor.create_pairing(agent_id)


async def execute_claim(claim):
    while True:
        result = await runtime.growth_execute({"agent_id": str(agent_id), "claim": claim})
        if not result.get("executor_pending"):
            accepted = runtime.finish(claim, result)
            return {"work_result": result, "finish": accepted}
        await asyncio.sleep(1)


async def website():
    token = execution_context.set({"key": "consumer-website:" + str(agent_id), "session_key": "consumer-website:" + str(agent_id), "agent_id": str(agent_id)})
    try:
        brief = await website_brief.analyze(SOURCE_URL, agent_id=str(agent_id))
        assert brief["product_name"] and brief["introduction"] and brief["features"]
        assert domain.confirm_draft_agent(str(agent_id), client_id, brief) == str(agent_id)
        return {"brief": brief, "source_url": SOURCE_URL, "confirmed_agent_id": str(agent_id)}
    finally:
        execution_context.reset(token)


async def decision():
    assert JOBS.get("website", {}).get("state") == "completed"
    # Complete the real product-reading work queued by confirmation, without
    # fabricating a research result or running any of the decision's child tasks.
    with tx(str(agent_id)) as c:
        product_tasks = all_rows(c, "SELECT id,input FROM lead.tasks WHERE type='growth.research' AND input#>>'{spec,purpose}'='product' AND state IN ('planned','queued')")
    for row in product_tasks:
        ref = {"agent_id": str(agent_id), "task_id": str(row["id"]), "task_generation": row["input"]["task_generation"]}
        ready = runtime.advance(ref)
        if ready["state"] == "ready":
            result = await execute_claim(ready["claim"])
            assert result["work_result"]["outcome"] == "completed", result
    with tx(str(agent_id)) as c:
        agent = one(c, "SELECT onboarding_state FROM lead.agents WHERE id=%s", (agent_id,))
        contract = {**agent["onboarding_state"].get("operating_contract", {}), "revision": 1,
            "objective": "Identify one reachable public venue where programming beginners ask for help learning Python. Research only; no posting, account actions, or acquisition claims.", "audience": "People learning their first programming language"}
        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract}',%s),desired_state='running' WHERE id=%s", (j(contract), agent_id))
    triggers = [{"reason": "owner_facts", "revision": 1, "source": "explicit disposable verification goal"}]
    ref = runtime.ensure_decision(str(agent_id), triggers)
    ready = runtime.advance(ref)
    assert ready["state"] == "ready", ready
    result = await execute_claim(ready["claim"])
    work = result["work_result"]
    assert work["outcome"] == "completed", result
    accepted = work["output"].get("accepted", [])
    assert accepted and not work["output"].get("rejected"), work
    with tx(str(agent_id)) as c:
        tasks = all_rows(c, "SELECT id,type,state,input FROM lead.tasks WHERE id<>%s ORDER BY created_at", (ref["task_id"],))
        assert any(item["type"] in ("growth.research", "growth.content") for item in tasks)
        assert not one(c, "SELECT id FROM lead.actions"), "this validation must never submit to a social account"
    return {"decision_task": ref, **result, "child_tasks": tasks}


async def content():
    assert JOBS.get("website", {}).get("state") == "completed"
    page = await fetch_product(SOURCE_URL, discover_links=False)
    evidence, _ = tools._remember(str(agent_id), page)
    assert evidence is not None
    with tx(str(agent_id)) as c:
        experiment = create_experiment(c, str(agent_id), ExperimentSpec(question_type="value", question="Can a factual draft explain what Python offers to beginners?", hypothesis="A concise factual overview helps readers understand the documented language.", learning_signal="A source-backed draft for review, not evidence of customer acquisition", audience="Programming beginners", variation="consumer-content-verification", token_limit=1000000, review_at=utcnow()+timedelta(hours=1)))
        ref = submit_work(c, str(agent_id), TaskSpec(kind="content", purpose="source-backed introduction", experiment_id=experiment["id"], evidence_refs=[evidence], payload={"brief": "Write a short factual introduction for a person considering learning Python. Use only the supplied official source and confirmed product facts. Include source claim checks. Do not claim testimonials, measured efficacy, adoption or that this draft was posted.", "audience": "People learning their first programming language", "language": "English", "format": "text"}, review_at=utcnow()+timedelta(hours=1)))
    ready = runtime.advance(ref)
    assert ready["state"] == "ready", ready
    result = await execute_claim(ready["claim"])
    assert result["work_result"]["outcome"] == "completed", result
    with tx(str(agent_id)) as c:
        item = one(c, "SELECT * FROM lead.content_items WHERE validation_result->>'growth_task_id'=%s", (ref["task_id"],))
        assert item and item["body"]["body"] and item["validation_result"]["publication_status"] == "draft"
        assert item["validation_result"]["claim_checks"], "factual content must carry actual source checks"
        assert any(str(value["id"]) == str(evidence.id) for value in item["source_refs"])
        assert not one(c, "SELECT id FROM lead.actions")
    return {"content_task": ref, **result, "content_item": item, "source_url": SOURCE_URL}


class Recall(BaseModel):
    product_name: str
    source_url: str


async def recall():
    token = execution_context.set({"key": "consumer-recall:" + str(agent_id), "session_key": "consumer-website:" + str(agent_id), "agent_id": str(agent_id)})
    try:
        answer = await model.run_model(Recall, {"request": "Using only this conversation's earlier verified website analysis, give its product name and the original about-page URL. Do not browse or use tools to look it up again."}, "Recall the original verified product and source from the existing conversation. Do not search or call tools for this check.", agent_id=str(agent_id), purpose="research")
        assert "python" in answer.product_name.lower() and answer.source_url == SOURCE_URL
        return answer.model_dump()
    finally:
        execution_context.reset(token)


async def run_job(phase):
    job = JOBS[phase]
    try:
        job["output"] = output(await {"website": website, "decision": decision, "content": content, "recall": recall}[phase]())
        job["state"] = "completed"
    except BaseException as error:
        job["state"] = "failed"
        job["error"] = str(error)
        job["error_type"] = type(error).__name__


@app.post("/fixture/start/{phase}")
async def start(phase: str):
    if phase not in {"website", "decision", "content", "recall"}:
        return JSONResponse({"detail": "unknown_fixture_phase"}, status_code=404)
    if phase not in JOBS:
        JOBS[phase] = {"state": "running"}
        asyncio.create_task(run_job(phase))
    return JOBS[phase]


@app.get("/fixture/status/{phase}")
def status(phase: str):
    with tx(str(agent_id)) as c:
        requests = all_rows(c, "SELECT id,request_key,state,thread_id,turn_id,usage,result,session_key,task_id FROM lead.local_executor_requests ORDER BY created_at,id")
        billing = {"reservations": one(c, "SELECT count(*) n FROM lead.budget_reservations")["n"], "usage_events": one(c, "SELECT count(*) n FROM lead.usage_events")["n"]}
        methods = all_rows(c, "SELECT request_id,method,params FROM lead.local_executor_events WHERE method IN ('item/started','item/completed','thread/tokenUsage/updated','error','turn/completed','result') ORDER BY created_at,event_id")
    return output({"job": JOBS.get(phase), "requests": requests, "billing": billing, "events": methods, "agent_id": str(agent_id)})
