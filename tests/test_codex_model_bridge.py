import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import lead_app
import pytest
from lead_app import local_executor_client as client
from pydantic import BaseModel


class Answer(BaseModel):
    text: str


def test_native_draft_schema_closes_every_object_and_requires_all_properties():
    from lead_app.website_brief import Draft

    schema, codec = client.result_transport(Draft)
    assert codec == "structured"

    def inspect(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                inspect(value)
        elif isinstance(node, list):
            for value in node:
                inspect(value)
    inspect(schema)


async def test_dynamic_business_payload_round_trips_without_closing_its_keys():
    from lead_app.growth.contracts import DecisionProposal

    value = {"context_key": "fixture", "rationale": "source-backed decision", "commands": [{
        "command_key": "next", "group_key": "group", "kind": "task.create", "goal_ref": {"revision": 1},
        "payload": {"kind": "research", "arbitrary": {"nested": [1, "two", None]}}}]}
    submissions = []
    fake = SimpleNamespace(submit=lambda aid, key, payload, **kwargs:
        submissions.append(payload) or {"id": "request", "payload": payload},
        request_status=lambda *args: {"state": "completed", "output": {"result_json": json.dumps(value)}})
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        result = await client.run_model(DecisionProposal, {}, "instructions", agent_id="agent")
    assert result.commands[0].payload == value["commands"][0]["payload"]
    assert submissions[0]["result_codec"] == "json_string_v1"
    assert submissions[0]["output_schema"]["additionalProperties"] is False
    assert submissions[0]["result_schema"] == DecisionProposal.model_json_schema()
    assert '"additionalProperties": true' in submissions[0]["instructions"]


async def test_json_transport_still_applies_original_business_validation():
    from lead_app.growth.contracts import DecisionProposal
    from pydantic import ValidationError

    fake = SimpleNamespace(submit=lambda aid, key, payload, **kwargs: {"id": "request", "payload": payload},
        request_status=lambda *args: {"state": "completed", "output": {"result_json": '{"commands":[]}'}})
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        with pytest.raises(ValidationError):
            await client.run_model(DecisionProposal, {}, "instructions", agent_id="agent")


async def test_pre_codec_request_replays_frozen_contract_without_resubmission():
    original = {"id": "original", "session_key": "model:agent:Answer", "task_id": None,
        "payload": {"output_schema": Answer.model_json_schema()}}
    fake = SimpleNamespace(request_by_key=lambda *args: original,
        request_status=lambda *args: {"state": "completed", "output": {"text": "original result"}})
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        result = await client.run_model(Answer, {"new_observation": True}, "instructions", agent_id="agent")
    assert result.text == "original result"


def test_retry_keeps_business_request_identity_when_observations_change():
    context = client.execution_context.set({"key": "original-chat-job"})
    try:
        first = client.request_identity(Answer, {"observed_at": "before"}, "chat", "primary")
        later = client.request_identity(Answer, {"observed_at": "later"}, "chat", "primary")
    finally:
        client.execution_context.reset(context)
    assert first == later


@pytest.mark.parametrize("enabled", [True, False])
async def test_native_path_skips_server_model_and_budget(monkeypatch, enabled):
    from lead_app import model
    calls = []

    async def native(*args, **kwargs):
        calls.append(kwargs)
        return Answer(text="native")

    def forbidden(*args, **kwargs):
        raise AssertionError("server model or API reservation entered")

    fake = SimpleNamespace(enabled=lambda aid=None: enabled, bound=lambda *args, **kwargs: True)
    monkeypatch.setattr(client, "run_model", native)
    monkeypatch.setattr(model, "Agent", forbidden)
    monkeypatch.setattr(model, "reserve", forbidden)
    monkeypatch.setattr(model, "managed_prompt", lambda aid, prompt, settings: prompt)
    monkeypatch.setattr("lead_app.admin_settings.for_agent", lambda *args: ({"tools": {}}, "fixture"))
    context = client.execution_context.set({"key": "original-job"})
    try:
        with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
            result = await model.run_model(Answer, {"question": "native"}, agent_id="fixture")
    finally:
        client.execution_context.reset(context)
    assert result.text == "native"
    assert len(calls) == 1


async def test_same_task_reuses_native_request_with_medium_and_complete_input():
    submissions = []

    def submit(aid, key, payload, **kwargs):
        submissions.append((key, payload, kwargs))
        return {"id": "original-request"}

    fake = SimpleNamespace(submit=submit, request_status=lambda *args:
        {"state": "completed", "output": {"text": "source-backed"}})
    context = client.execution_context.set({"key": "original-task", "task_id": "task", "session_key": "task:task"})
    try:
        with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
            for _ in range(2):
                result = await client.run_model(Answer, {"evidence": "x" * 100000}, "instructions", agent_id="agent")
    finally:
        client.execution_context.reset(context)
    assert result.text == "source-backed"
    assert submissions[0][0] == submissions[1][0]
    payload = submissions[0][1]
    assert payload["model"] == "gpt-6.1-sol" and payload["effort"] == "medium"
    assert len(payload["input"]) > 100000
    assert payload["output_schema"]["properties"]["text"]["type"] == "string"


async def test_unknown_native_result_never_falls_back_or_resubmits():
    submitted = []
    fake = SimpleNamespace(submit=lambda *args, **kwargs: submitted.append(1) or {"id": "original"},
        request_status=lambda *args: {"state": "execution_unknown"})
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        with pytest.raises(client.ExecutorUnavailable, match="execution_unknown"):
            await client.run_model(Answer, {}, "instructions", agent_id="agent")
    assert submitted == [1]


async def test_cancel_interrupts_only_original_native_request(monkeypatch):
    import asyncio

    calls = []
    fake = SimpleNamespace(request_status=lambda *args: {"state": "running"},
        command=lambda *args: calls.append(args))
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        task = asyncio.create_task(client.await_request("agent", "original-request"))
        await asyncio.sleep(.02)
        task.cancel("lead_business_cancel")
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) == 1
    assert calls[0][:3] == ("agent", "original-request", "interrupt")


async def test_worker_shutdown_does_not_interrupt_resumable_native_request():
    import asyncio

    calls = []
    fake = SimpleNamespace(request_status=lambda *args: {"state": "running"},
        command=lambda *args: calls.append(args))
    with patch.object(lead_app, "local_executor", fake, create=True), patch.dict(sys.modules, {"lead_app.local_executor": fake}):
        task = asyncio.create_task(client.await_request("agent", "original-request"))
        await asyncio.sleep(.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == []
