"""Changed decision instructions start a new thread without abandoning old work."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import pytest
from lead_app import domain, executor_studio
from lead_app import local_executor as executor
from lead_app import local_executor_client as client
from lead_app.db import one, tx
from lead_app.scope import current_tenant, tenant_scope
from pydantic import BaseModel
from test_local_executor import connected, payload


class Answer(BaseModel):
    text: str


class OpenAnswer(BaseModel):
    values: dict


class ExtendedOpenAnswer(OpenAnswer):
    explanation: str = "Observed evidence"


def settle(aid, request, state="completed"):
    with tx(aid) as c:
        c.execute("UPDATE lead.local_executor_requests SET state=%s,thread_id='original-thread' WHERE id=%s",
                  (state, request["id"]))


def test_instruction_versions_keep_same_version_history_and_original_device(growth_agent):
    original_device = connected(growth_agent)
    base = "decision:" + str(growth_agent) + ":1"
    first = executor.submit(growth_agent, "one", payload(), session_key=base, instruction_session=True)
    settle(growth_agent, first)
    replacement_device = connected(growth_agent)
    changed = {**payload(), "instructions": "Use the updated complete instructions."}
    second = executor.submit(growth_agent, "two", changed, session_key=base, instruction_session=True)
    settle(growth_agent, second)
    third = executor.submit(growth_agent, "three", changed, session_key=base, instruction_session=True)
    assert first["session_key"] != second["session_key"] == third["session_key"]
    assert all(str(row["device_id"]) == original_device["device_id"] for row in (first, second, third))
    assert replacement_device["device_id"] != original_device["device_id"]
    assert all(row["scope"]["instruction_session"] == base for row in (first, second, third))
    assert all(row["payload"]["session_key"] == row["session_key"] for row in (first, second, third))


@pytest.mark.parametrize("legacy", [True, False])
def test_frozen_unknown_request_is_returned_without_rotation_or_new_command(growth_agent, legacy):
    connected(growth_agent)
    base = "decision:" + str(growth_agent) + ":1"
    first = executor.submit(growth_agent, "original", payload(), session_key=base, instruction_session=not legacy)
    settle(growth_agent, first, "execution_unknown")
    original = executor.request_status(growth_agent, first["id"])
    replay = executor.submit(growth_agent, "original", {**payload(), "instructions": "Updated instructions"},
        session_key=base, instruction_session=True, resume=True)
    assert replay == original
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.local_executor_commands WHERE request_id=%s", (first["id"],))["n"] == 1


@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("state", ["queued", "running", "waiting_input", "execution_unknown"])
def test_new_instruction_version_cannot_bypass_unresolved_logical_session(growth_agent, legacy, state):
    connected(growth_agent)
    base = "decision:" + str(growth_agent) + ":1"
    original = executor.submit(growth_agent, "original", payload(), session_key=base, instruction_session=not legacy)
    settle(growth_agent, original, state)
    with pytest.raises(ValueError, match="executor_session_unresolved"):
        executor.submit(growth_agent, "new-version", {**payload(), "instructions": "Updated"},
            session_key=base, instruction_session=True)
    assert executor.request_by_key(growth_agent, "new-version") is None
    assert executor.request_status(growth_agent, original["id"])["state"] == state


def test_concurrent_instruction_versions_create_only_one_request(growth_agent):
    connected(growth_agent)
    tenant = current_tenant()
    base = "decision:" + str(growth_agent) + ":1"
    start = Barrier(2)

    def submit(index):
        with tenant_scope(tenant):
            start.wait(timeout=10)
            try:
                return executor.submit(growth_agent, "race:" + str(index),
                    {**payload(), "instructions": "Version " + str(index)},
                    session_key=base, instruction_session=True)["id"]
            except ValueError as error:
                return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, (1, 2)))
    assert results.count("executor_session_unresolved") == 1
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.local_executor_requests WHERE scope->>'instruction_session'=%s", (base,))["n"] == 1


def test_new_instruction_session_preserves_original_managed_route():
    from test_executor_nodes import connected as managed_connected
    from test_executor_nodes import new_user

    owner = new_user()
    with tenant_scope(owner["tenant_id"], owner):
        aid = domain.create_agent(runtime_engine="growth")
        try:
            managed_connected()
            base = "decision:" + str(aid) + ":1"
            original = executor.submit(aid, "original", payload(), session_key=base)
            settle(aid, original)
            fresh = executor.submit(aid, "fresh", payload(), session_key=base, instruction_session=True)
            assert original["scope"]["execution_route"]["source"] == "managed_node"
            assert fresh["scope"]["execution_route"] == original["scope"]["execution_route"]
            assert fresh["device_id"] == original["device_id"] is not None
        finally:
            domain.set_control(aid, "archive")


def test_foreign_project_pending_session_and_device_are_not_inherited(growth_agent):
    connected(growth_agent)
    executor.submit(growth_agent, "shared-key", payload(), session_key="same-base", instruction_session=True)
    other = domain.create_agent(runtime_engine="growth")
    other_device = connected(other)
    fresh = executor.submit(other, "shared-key", payload(), session_key="same-base", instruction_session=True)
    assert str(fresh["agent_id"]) == str(other)
    assert str(fresh["device_id"]) == other_device["device_id"]


@pytest.mark.parametrize("changed", ["task", "output_schema", "result_schema", "base", "strict"])
def test_existing_versioned_request_keeps_original_task_and_schema_contract(growth_agent, changed):
    connected(growth_agent)
    base = "decision:" + str(growth_agent) + ":1"
    body = {**payload(), "output_schema": {"type": "object"}, "result_schema": {"title": "Original"}}
    executor.submit(growth_agent, "original", body, session_key=base, instruction_session=True)
    kwargs = {"session_key": base, "instruction_session": True, "resume": True}
    changed_body = {**body, "instructions": "Updated"}
    if changed == "task":
        kwargs["task_id"] = str(uuid4())
    elif changed in ("output_schema", "result_schema"):
        changed_body[changed] = {"title": "Changed"}
    elif changed == "base":
        kwargs["session_key"] = "other-base"
    else:
        kwargs["resume"] = False
    with pytest.raises(ValueError, match="executor_request_conflict"):
        executor.submit(growth_agent, "original", changed_body, **kwargs)


def test_full_length_logical_session_has_bounded_native_key(growth_agent):
    connected(growth_agent)
    row = executor.submit(growth_agent, "key", payload(), session_key="x" * 500, instruction_session=True)
    assert len(row["session_key"]) < 500
    assert row["scope"]["instruction_session"] == "x" * 500
    with pytest.raises(ValueError, match="executor_request_key_invalid"):
        executor.submit(growth_agent, "oversize", payload(), session_key="x" * 501, instruction_session=True)


async def test_complete_business_schema_change_selects_new_native_session(growth_agent, monkeypatch):
    connected(growth_agent)
    observed = []

    async def complete(aid, request_id):
        row = executor.request_status(aid, request_id)
        observed.append(row)
        settle(aid, row)
        return {"result_json": '{"values":{"source":"original"}}'}

    monkeypatch.setattr(client, "await_request", complete)
    for index, output in enumerate((OpenAnswer, ExtendedOpenAnswer)):
        token = client.execution_context.set({"key": "decision:" + str(index),
            "session_key": "decision:" + str(growth_agent) + ":1", "growth_decision_session": True})
        try:
            await client.run_model(output, {}, "Unchanged business instructions", agent_id=growth_agent)
        finally:
            client.execution_context.reset(token)
    first, second = observed
    assert first["session_key"] != second["session_key"]
    assert first["scope"]["instruction_session"] == second["scope"]["instruction_session"]
    for row, output in zip(observed, (OpenAnswer, ExtendedOpenAnswer), strict=True):
        assert json.dumps(output.model_json_schema(), ensure_ascii=False) in row["payload"]["instructions"]


@pytest.mark.parametrize("versioned", [True, False])
async def test_client_versions_only_explicit_decision_after_complete_schema_encoding(monkeypatch, versioned):
    submitted = []

    def submit(aid, key, body, **kwargs):
        submitted.append((copy.deepcopy(body), kwargs))
        return {"id": "new", "payload": body}

    monkeypatch.setattr(executor, "request_by_key", lambda *args: None)
    monkeypatch.setattr(executor, "submit", submit)
    monkeypatch.setattr(executor, "request_status", lambda *args:
        {"state": "completed", "output": {"result_json": '{"values":{"original":"kept"}}'}})
    token = client.execution_context.set({"key": "original-task", "session_key": "decision:agent:1",
        "growth_decision_session": versioned})
    try:
        result = await client.run_model(OpenAnswer, {}, "Current instructions", agent_id="agent")
    finally:
        client.execution_context.reset(token)
    assert result.values == {"original": "kept"}
    body, kwargs = submitted[0]
    assert kwargs.get("instruction_session", False) is versioned
    assert json.dumps(OpenAnswer.model_json_schema(), ensure_ascii=False) in body["instructions"]
    assert body["instructions"].startswith("Current instructions")


@pytest.mark.parametrize("legacy", [True, False])
async def test_client_resumes_frozen_unknown_request_before_current_instructions(monkeypatch, legacy):
    base = "decision:agent:1"
    old_instructions = "Old original instructions"
    original = {"id": "original", "session_key": base if legacy else executor.instruction_session_key(base, old_instructions),
        "scope": {} if legacy else {"instruction_session": base}, "task_id": "original-task",
        "payload": {"instructions": old_instructions, "result_schema": Answer.model_json_schema(),
                    "tool_context": {"original": "scope"}}}
    resumed = []
    monkeypatch.setattr(executor, "request_by_key", lambda *args: original)
    monkeypatch.setattr(executor, "submit", lambda *args, **kwargs: pytest.fail("Resubmitted frozen request"))
    monkeypatch.setattr(executor, "request_status", lambda *args: {"state": "execution_unknown"})
    token = client.execution_context.set({"key": "same-key", "session_key": base, "task_id": "original-task",
        "growth_decision_session": True})
    try:
        with pytest.raises(client.ExecutorUnavailable, match="execution_unknown"):
            await client.run_model(Answer, {}, "Current changed instructions", agent_id="agent",
                tools=SimpleNamespace(resume=resumed.append))
    finally:
        client.execution_context.reset(token)
    assert resumed == [{"original": "scope"}]


async def test_explicit_decision_context_does_not_version_studio(monkeypatch):
    submitted = []
    monkeypatch.setattr(executor_studio, "request_by_key", lambda *args, **kwargs: None)

    def submit(key, body, **kwargs):
        submitted.append(kwargs)
        return {"id": "studio", "payload": body}

    monkeypatch.setattr(executor_studio, "submit", submit)
    monkeypatch.setattr(executor_studio, "request_status", lambda *args:
        {"state": "completed", "output": {"text": "Original studio"}})
    token = client.execution_context.set({"key": "studio-key", "session_key": "studio-base",
        "studio_purpose": "website_brief", "growth_decision_session": True})
    try:
        result = await client.run_model(Answer, {}, "Current instructions")
    finally:
        client.execution_context.reset(token)
    assert result.text == "Original studio"
    assert submitted == [{"session_key": "studio-base", "operation_key": "studio-key"}]
