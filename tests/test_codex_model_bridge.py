import sys
from types import SimpleNamespace
from unittest.mock import patch

import lead_app
import pytest
from lead_app import local_executor_client as client
from pydantic import BaseModel


class Answer(BaseModel):
    text: str


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
