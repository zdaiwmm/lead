"""Real scoped DB integration for Codex migration; external reads/model are fixtures."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app import local_executor
from lead_app.browser_tools import BrowserTools
from lead_app.budget import growth_cost_context, reserve, settle
from lead_app.db import all_rows, one, tx
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.runtime import recover_due
from lead_app.growth.store import create_experiment, question_costs, submit_work
from lead_app.local_executor_client import (
    execution_context,
    request_identity,
    run_model,
)
from lead_app.research import finish_run
from pydantic import BaseModel

CAPABILITIES = {"ready": True, "auth_type": "chatgpt", "models": ["gpt-6.1-sol"], "reasoning_efforts": ["medium"]}
SOURCE = {"url": "https://source.example/document", "title": "Observed public source", "text": "A complete source observation retained by the actual Lead evidence writer.", "links": []}


class Answer(BaseModel):
    text: str


def connect(aid):
    invitation = local_executor.create_pairing(aid)
    device = local_executor.pair(invitation["code"], "Integration Mac", "darwin", CAPABILITIES)
    local_executor.poll(device["token"], device["epoch"], instance_id="business-integration")
    return device


def spec(variation="original"):
    return ExperimentSpec(question_type="audience", question="Which audience describes this need?", hypothesis="Independent makers describe this need.", learning_signal="An independently observed request", token_limit=1000, review_at=utcnow() + timedelta(hours=1), variation=variation)


def ledger(aid):
    with tx(aid) as connection:
        return {"reservations": all_rows(connection, "SELECT id,state,units,pricing_snapshot FROM lead.budget_reservations ORDER BY id"), "usage": all_rows(connection, "SELECT id,reservation_id,units FROM lead.usage_events ORDER BY id")}


def test_native_experiments_ignore_old_token_debt_and_preserve_window_and_ledger(growth_agent):
    aid = str(growth_agent)
    with tx(aid) as connection:
        connection.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        original = create_experiment(connection, aid, spec())
        task = submit_work(connection, aid, TaskSpec(kind="research", purpose="audience", experiment_id=original["id"], payload={"question": "Which audience describes this need?", "urls": [SOURCE["url"]]}, review_at=utcnow() + timedelta(hours=1)))
    context = growth_cost_context.set({"task_id": task["task_id"], "experiment_id": str(original["id"])})
    try:
        known = reserve(aid, "fixture-known", 600)
        settle(aid, known, actual=600)
        unknown = reserve(aid, "fixture-unknown", 400)
        settle(aid, unknown)
    finally:
        growth_cost_context.reset(context)
    before = ledger(aid)
    assert {row["state"] for row in before["reservations"]} == {"settled", "unknown"}
    with tx(aid) as connection:
        costs = question_costs(connection, original["question_key"])
        assert costs == {"limit_units": 1000, "committed_units": 1000}
        with pytest.raises(ValueError, match="question_limit"):
            create_experiment(connection, aid, spec("blocked-before-native"))
    connect(aid)
    with tx(aid) as connection:
        continued = create_experiment(connection, aid, spec("native-variation"))
        assert continued["id"] != original["id"]
    recover_due(aid, now=utcnow() + timedelta(minutes=5))
    with tx(aid) as connection:
        rows = all_rows(connection, "SELECT id,state FROM lead.growth_experiments ORDER BY id")
        assert len(rows) == 2 and all(row["state"] == "active" for row in rows)
    assert ledger(aid) == before, "switching execution must not settle, clear, or replace API debt"
    recover_due(aid, now=utcnow() + timedelta(hours=2))
    with tx(aid) as connection:
        rows = all_rows(connection, "SELECT state,conclusion FROM lead.growth_experiments ORDER BY id")
        assert all(row["state"] == "closed" and row["conclusion"]["reason"] == "window_ended" for row in rows)
    assert ledger(aid) == before


async def wait_request(aid, key):
    for _ in range(100):
        row = await asyncio.to_thread(local_executor.request_by_key, aid, key)
        if row:
            return row
        await asyncio.sleep(0.01)
    raise AssertionError("durable request was not created")


def completed(device, request_id, output):
    local_executor.events(device["token"], device["epoch"], [{"id": str(uuid4()), "request_id": str(request_id), "method": "result", "params": {"state": "completed", "output": output, "thread_id": "fixture-original-thread", "turn_id": "fixture-original-turn"}}])


@pytest.mark.asyncio
async def test_native_read_persists_original_source_and_replay_restores_its_run(growth_agent, monkeypatch):
    aid = str(growth_agent)
    device = connect(aid)
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return dict(SOURCE)
    monkeypatch.setattr("lead_app.fetch.fetch_product", fetch)
    browser = BrowserTools(aid, "Read the original public source")
    tools = browser.tools()
    original_run = str(browser.run_id)
    token = execution_context.set({"key": "chat:source-job", "session_key": "chat:" + aid})
    try:
        key = request_identity(Answer, {"snapshot": "before"}, "chat", "primary")
        work = asyncio.create_task(run_model(Answer, {"snapshot": "before"}, "Read the source", agent_id=aid, purpose="chat", tools=tools))
        request = await wait_request(aid, key)
        assert request["payload"]["tool_context"]["browser_run_id"] == original_run
        result = await local_executor.call_tool(device["token"], device["epoch"], request["id"], "lead_public_read", {"url": SOURCE["url"]})
        observation = result["structuredContent"]
        assert str(observation["run_id"]) == original_run and observation["evidence_id"]
        with tx(aid) as connection:
            step = one(connection, "SELECT * FROM lead.browser_steps WHERE id=%s", (observation["evidence_id"],))
            assert str(step["run_id"]) == original_run and step["output"]["url"] == SOURCE["url"]
            assert step["operation"] == "codex-public-read"
        completed(device, request["id"], {"text": "Source-backed result"})
        assert (await work).text == "Source-backed result"
        # Reconstructed Python state must rejoin the saved evidence run, even if
        # local activity snapshots and provisional run IDs changed on restart.
        resumed_browser = BrowserTools(aid, "Read the original public source")
        resumed_tools = resumed_browser.tools()
        assert str(resumed_browser.run_id) != original_run
        answer = await run_model(Answer, {"snapshot": "after restart"}, "Read the source", agent_id=aid, purpose="chat", tools=resumed_tools)
        assert answer.text == "Source-backed result"
        assert str(resumed_browser.run_id) == original_run
        sources = finish_run(aid, resumed_browser.run_id)
        assert len(sources) == 1 and sources[0]["url"] == SOURCE["url"]
        assert sources[0]["evidence_id"] == str(observation["evidence_id"])
        assert str(sources[0]["run_id"]) == original_run
        with tx(aid) as connection:
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_requests")["n"] == 1
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_commands WHERE kind='run'")["n"] == 1
            assert one(connection, "SELECT count(*) n FROM lead.browser_steps")["n"] == 1
        assert calls == [SOURCE["url"]]
        assert ledger(aid) == {"reservations": [], "usage": []}
    finally:
        execution_context.reset(token)


@pytest.mark.asyncio
async def test_same_job_replays_frozen_payload_without_second_native_command(growth_agent):
    aid = str(growth_agent)
    device = connect(aid)
    token = execution_context.set({"key": "original-business-job", "session_key": "original-business-session"})
    try:
        first_prompt = {"activity": ["before"], "question": "Summarize existing facts"}
        key = request_identity(Answer, first_prompt, "research", "primary")
        work = asyncio.create_task(run_model(Answer, first_prompt, "Use facts", agent_id=aid, purpose="research"))
        original = await wait_request(aid, key)
        completed(device, original["id"], {"text": "Original completed result"})
        assert (await work).text == "Original completed result"
        replay = await run_model(Answer, {"activity": ["before", "worker restarted"]}, "Use facts", agent_id=aid, purpose="research")
        assert replay.text == "Original completed result"
        row = local_executor.request_by_key(aid, key)
        assert row["id"] == original["id"] and row["payload"] == original["payload"]
        with tx(aid) as connection:
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_requests")["n"] == 1
            assert one(connection, "SELECT count(*) n FROM lead.local_executor_commands WHERE kind='run'")["n"] == 1
    finally:
        execution_context.reset(token)


@pytest.mark.asyncio
async def test_basic_native_source_read_keeps_public_web_without_advanced_research(growth_agent, monkeypatch):
    """Adding evidence persistence must not turn a basic read into an advanced tool."""
    from lead_app.admin_config import read, write
    from lead_app.admin_settings import for_agent

    aid = str(growth_agent)
    device = connect(aid)
    settings, _ = for_agent(aid, "models")
    settings["tools"]["public_web"] = True
    settings["tools"]["advanced_research"] = False
    with tx(aid) as connection:
        key = "models/" + aid
        write(connection, key, settings, read(connection, key)["version"])
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        return dict(SOURCE)
    monkeypatch.setattr("lead_app.fetch.fetch_product", fetch)
    browser = BrowserTools(aid, "Read one public page")
    tools = browser.tools()
    request = local_executor.submit(aid, "basic-public-read", {"input": "Read one page", "instructions": "Read a public source", "tool_context": {"kind": "model", **tools.local_executor_context}, "tools": [{"name": "lead_public_read", "description": "read", "inputSchema": {"type": "object"}}]}, session_key="basic-public-read")
    result = await local_executor.call_tool(device["token"], device["epoch"], request["id"], "lead_public_read", {"url": SOURCE["url"]})
    assert result["structuredContent"]["url"] == SOURCE["url"]
    assert calls == [SOURCE["url"]]
    assert finish_run(aid, browser.run_id)[0]["url"] == SOURCE["url"]
