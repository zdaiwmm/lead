"""Device transport does not replace the original business-action ledger."""
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app import domain
from lead_app import local_executor as executor
from lead_app.db import one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work
from lead_app.scope import current_tenant, tenant_scope

CAPABILITIES = {"ready": True, "auth_type": "chatgpt", "models": ["gpt-6.1-sol"],
                "reasoning_efforts": ["medium"]}


def connected(aid):
    invitation = executor.create_pairing(aid)
    device = executor.pair(invitation["code"], "Test Mac", "darwin", CAPABILITIES)
    executor.poll(device["token"], device["epoch"], instance_id="first")
    return device


def payload(text="Research the supplied public source"):
    return {"input": text, "instructions": "Use observed evidence.", "tools": [],
            "model": "gpt-6.1-sol", "effort": "medium"}


def test_pairing_is_single_use_and_credentials_are_hashed(growth_agent):
    invitation = executor.create_pairing(growth_agent)
    device = executor.pair(invitation["code"], "Test Mac", "darwin", CAPABILITIES)
    with pytest.raises(ValueError, match="pairing_unavailable"):
        executor.pair(invitation["code"], "Other", "darwin", CAPABILITIES)
    with tx(growth_agent) as c:
        row = one(c, "SELECT * FROM lead.local_executor_devices WHERE id=%s", (device["device_id"],))
        assert row["token_hash"] != device["token"]
        assert device["token"] not in str(row)
    assert executor.enabled(growth_agent)
    assert executor.default_agent() == str(growth_agent)


def test_control_change_fences_original_request_and_interrupts_same_turn(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "control-original", payload(), session_key="control-original")
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s", (growth_agent,))
    commands = executor.poll(device["token"], 1, instance_id="first")["commands"]
    assert any(item["kind"] == "interrupt" and item["request_id"] == str(request["id"]) for item in commands)
    assert executor.request_status(growth_agent, request["id"])["scope"]["canceled"] is True


def test_request_key_hash_and_session_stay_on_original_device(growth_agent):
    first = connected(growth_agent)
    request = executor.submit(growth_agent, "original", payload(), session_key="same-task")
    replay = executor.submit(growth_agent, "original", payload(), session_key="same-task")
    assert replay["id"] == request["id"]
    with pytest.raises(ValueError, match="executor_request_conflict"):
        executor.submit(growth_agent, "original", payload("changed"), session_key="same-task")
    second = connected(growth_agent)
    follow = executor.submit(growth_agent, "follow", payload("Continue"), session_key="same-task")
    assert str(follow["device_id"]) == first["device_id"]
    assert second["device_id"] != first["device_id"]


def test_poll_repeats_until_durable_ack_and_restart_fences_old_epoch(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "once", payload(), session_key="once")
    batch = executor.poll(device["token"], 1, instance_id="first")
    command = batch["commands"][0]
    assert executor.poll(device["token"], 1, instance_id="first")["commands"][0]["id"] == command["id"]
    event = {"id": str(uuid4()), "request_id": str(request["id"]), "method": "command/ack",
             "params": {"command_id": command["id"]}}
    executor.events(device["token"], 1, [event])
    assert executor.poll(device["token"], 1, instance_id="first")["commands"] == []
    assert executor.poll(device["token"], 1, instance_id="restarted")["epoch"] == 2
    with pytest.raises(ValueError, match="executor_epoch_changed"):
        executor.events(device["token"], 1, [event])
    with pytest.raises(ValueError, match="executor_epoch_changed"):
        executor.poll(device["token"], 1, instance_id="first")


def test_event_replay_conflict_and_usage_snapshots(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "tokens", payload(), session_key="tokens")
    executor.events(device["token"], 1, [
        {"id": "tokens:unproven-child", "request_id": str(request["id"]), "method": "thread/tokenUsage/updated",
         "params": {"threadId": "child-thread", "tokenUsage": {"total": {"totalTokens": 999}}}},
        {"id": "tokens:no-thread", "request_id": str(request["id"]), "method": "thread/tokenUsage/updated",
         "params": {"tokenUsage": {"total": {"totalTokens": 888}}}},
    ])
    assert not executor.request_status(growth_agent, request["id"])["usage"]
    executor.events(device["token"], 1, [{"id": "tokens:root", "request_id": str(request["id"]),
        "method": "request/started", "params": {"thread_id": "thr-one"}}])
    event = {"id": "tokens:one", "request_id": str(request["id"]), "method": "thread/tokenUsage/updated",
             "params": {"threadId": "thr-one", "tokenUsage": {"total": {"totalTokens": 100}}}}
    executor.events(device["token"], 1, [event, event])
    later = {**event, "id": "tokens:two"}
    executor.events(device["token"], 1, [later])
    state = executor.request_status(growth_agent, request["id"])
    assert state["usage"]["total"]["totalTokens"] == 100
    with pytest.raises(ValueError, match="executor_event_conflict"):
        executor.events(device["token"], 1, [{**event, "params": {"changed": True}}])


def test_child_and_unrelated_turn_events_preserve_primary_request_identity(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "root-events", payload(), session_key="root-events")

    def event(method, params):
        return {"id": str(uuid4()), "request_id": str(request["id"]), "method": method, "params": params}

    executor.events(device["token"], 1, [
        event("request/started", {"thread_id": "root-thread"}),
        event("turn/started", {"threadId": "root-thread", "turn": {"id": "unproven-turn"}}),
    ])
    pending = executor.request_status(growth_agent, request["id"])
    assert (pending["thread_id"], pending["turn_id"]) == ("root-thread", None)
    executor.events(device["token"], 1, [
        event("request/started", {"thread_id": "root-thread", "turn_id": "root-turn"}),
        event("thread/tokenUsage/updated", {"threadId": "root-thread", "tokenUsage": {"total": {"totalTokens": 100}}}),
        event("thread/started", {"thread": {"id": "child-thread"}}),
        event("turn/started", {"threadId": "child-thread", "turn": {"id": "child-turn"}}),
        event("thread/tokenUsage/updated", {"threadId": "child-thread", "tokenUsage": {"total": {"totalTokens": 999}}}),
        event("turn/started", {"threadId": "root-thread", "turn": {"id": "unrelated-turn"}}),
    ])
    state = executor.request_status(growth_agent, request["id"])
    assert (state["thread_id"], state["turn_id"]) == ("root-thread", "root-turn")
    assert state["usage"]["total"]["totalTokens"] == 100
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.local_executor_events WHERE request_id=%s", (request["id"],))["n"] == 8
    with pytest.raises(ValueError, match="executor_result_conflict"):
        executor.events(device["token"], 1, [event("result", {"state": "completed", "thread_id": "root-thread", "turn_id": "unrelated-turn"})])
    executor.events(device["token"], 1, [event("result", {
        "state": "completed", "thread_id": "root-thread", "turn_id": "root-turn", "output": {"original": True}})])
    assert executor.request_status(growth_agent, request["id"])["state"] == "completed"


def test_unknown_result_never_reissues_run_or_marks_business_task(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "lost-ack", payload(), session_key="lost-ack")
    result = {"id": "unknown:one", "request_id": str(request["id"]), "method": "result",
              "params": {"state": "execution_unknown", "output": None, "thread_id": "thr-original"}}
    executor.events(device["token"], 1, [result])
    assert executor.request_status(growth_agent, request["id"])["state"] == "execution_unknown"
    assert executor.poll(device["token"], 1, instance_id="first")["commands"] == []
    assert executor.submit(growth_agent, "lost-ack", payload(), session_key="lost-ack")["state"] == "execution_unknown"
    with pytest.raises(ValueError, match="executor_result_conflict"):
        executor.events(device["token"], 1, [{"id": "wrong-thread", "request_id": str(request["id"]), "method": "result",
                                            "params": {"state": "completed", "thread_id": "thr-other", "reconciled": True}}])
    executor.events(device["token"], 1, [{"id": "reconciled", "request_id": str(request["id"]), "method": "result",
                                        "params": {"state": "completed", "thread_id": "thr-original", "turn_id": "turn-original",
                                                   "output": {"receipt": "read_original_thread"}, "reconciled": True}}])
    assert executor.request_status(growth_agent, request["id"])["state"] == "completed"
    assert executor.poll(device["token"], 1, instance_id="first")["commands"] == []


def test_foreign_device_and_tenant_cannot_read_request(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "private", payload(), session_key="private")
    other = domain.create_agent(runtime_engine="growth")
    foreign = connected(other)
    with pytest.raises(ValueError, match="executor_request_unavailable"):
        executor.events(foreign["token"], 1, [{"id": "x", "request_id": str(request["id"]),
                                               "method": "result", "params": {"state": "completed"}}])
    with tenant_scope(str(uuid4())):
        with pytest.raises(LookupError):
            executor.request_status(growth_agent, request["id"])
    executor.revoke(growth_agent, device["device_id"])
    with pytest.raises(ValueError, match="executor_device_unavailable"):
        executor.poll(device["token"], 1, instance_id="first")


def test_expired_pairing_and_offline_status(growth_agent):
    invitation = executor.create_pairing(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.local_executor_devices SET pairing_expires_at=now()-interval '1 second'")
    with pytest.raises(ValueError, match="pairing_unavailable"):
        executor.pair(invitation["code"], "Test", "darwin", CAPABILITIES)
    device = connected(growth_agent)
    assert executor.configured(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.local_executor_devices SET last_seen_at=%s WHERE id=%s",
                  (utcnow()-timedelta(minutes=3), device["device_id"]))
    assert not executor.configured(growth_agent)
    assert executor.enabled(growth_agent)
    request = executor.submit(growth_agent, "offline", payload(), session_key="offline")
    assert request["state"] == "queued"


def test_model_and_effort_are_fixed_and_event_scope_is_immutable(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "fixed", {**payload(), "model": "wrong", "effort": "low"}, session_key="fixed")
    assert request["payload"]["model"] == "gpt-6.1-sol"
    assert request["payload"]["effort"] == "medium"
    executor.events(device["token"], 1, [{"id": "done", "request_id": str(request["id"]), "method": "result",
                                        "params": {"state": "completed", "output": {"ready": True}}}])
    assert executor.request_status(growth_agent, request["id"])["output"] == {"ready": True}
    with pytest.raises(ValueError, match="executor_result_conflict"):
        executor.events(device["token"], 1, [{"id": "other-done", "request_id": str(request["id"]), "method": "result",
                                            "params": {"state": "failed", "output": None}}])


@pytest.mark.asyncio
async def test_tools_require_declared_name_live_scope_and_fixed_device(growth_agent, monkeypatch):
    import sys
    from types import SimpleNamespace
    called = []
    async def call(device, request, name, arguments):
        called.append((device, request, name, arguments))
        return {"content": [{"type": "text", "text": "observed"}]}
    monkeypatch.setitem(sys.modules, "lead_app.local_executor_tools", SimpleNamespace(call=call))
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "tools", {**payload(), "tools": [{"name": "observe", "description": "read", "inputSchema": {"type": "object"}}]}, session_key="tools")
    result = await executor.call_tool(device["token"], 1, request["id"], "observe", {})
    assert result["content"][0]["text"] == "observed" and len(called) == 1
    with pytest.raises(ValueError, match="executor_tool_not_declared"):
        await executor.call_tool(device["token"], 1, request["id"], "unregistered", {})
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s", (growth_agent,))
    with pytest.raises(ValueError, match="executor_scope_changed"):
        await executor.call_tool(device["token"], 1, request["id"], "observe", {})
    assert len(called) == 1


def test_interrupt_fences_tools_but_accepts_late_execution_evidence(growth_agent):
    device = connected(growth_agent)
    request = executor.submit(growth_agent, "cancel", payload(), session_key="cancel")
    command_id = uuid4()
    first = executor.command(growth_agent, request["id"], "interrupt", {}, command_id)
    assert executor.command(growth_agent, request["id"], "interrupt", {}, command_id) == first
    with pytest.raises(ValueError, match="executor_command_conflict"):
        executor.command(growth_agent, request["id"], "interrupt", {"changed": True}, command_id)
    executor.events(device["token"], 1, [{"id": "late", "request_id": str(request["id"]), "method": "result",
                                        "params": {"state": "completed", "output": {"receipt": "late"}}}])
    assert executor.request_status(growth_agent, request["id"])["output"] == {"receipt": "late"}


@pytest.mark.asyncio
async def test_task_generation_and_attempt_are_checked_before_tool(growth_agent, monkeypatch):
    from lead_app.db import j
    device = connected(growth_agent)
    with tx(growth_agent) as c:
        task_id = submit_work(c, growth_agent, TaskSpec(kind="research", purpose="source",
                              review_at=utcnow()+timedelta(hours=1)))["task_id"]
        c.execute("UPDATE lead.tasks SET state='running',result=%s WHERE id=%s",
                  (j({"runtime": {"attempt_token": "original"}}), task_id))
    request = executor.submit(growth_agent, "task-scope", {**payload(), "tools": [{"name": "observe"}]},
                              session_key="task-scope", task_id=task_id)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j({"runtime": {"attempt_token": "replacement"}}), task_id))
    with pytest.raises(ValueError, match="executor_scope_changed"):
        await executor.call_tool(device["token"], 1, request["id"], "observe", {})
    # No executor result is allowed to mutate the original task's status.
    executor.events(device["token"], 1, [{"id": "task-result", "request_id": str(request["id"]), "method": "result",
                                        "params": {"state": "completed", "output": {}}}])
    with tx(growth_agent) as c:
        assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task_id,))["state"] == "running"


def test_owner_controls_require_capability_and_transport_requires_bearer(growth_agent):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from lead_app.local_executor_api import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/api/agents/{growth_agent}/executor/pairing"
    with tenant_scope(current_tenant(), {"role": "viewer", "capabilities": [], "agent_ids": None}):
        assert client.post(path).status_code == 403
    with tenant_scope(current_tenant(), {"role": "owner", "subject": "fixture", "issuer": "local"}):
        response = client.post(path)
        assert response.status_code == 200
        import shlex
        command = shlex.split(response.json()["connect_command"])
        assert command[:5] == ["lead-executor", "pair", "--profile", str(growth_agent), "--url"]
        assert command[-1] == "--install"
        assert "<Lead地址>" not in response.json()["connect_command"]
    assert client.post("/executor/v1/poll", json={"epoch": 1}).status_code == 401
    assert client.post("/executor/v1/poll", json={"epoch": 1}, headers={"Authorization": "Bearer forged"}).status_code == 401
