"""Real Task/request persistence with a fake external native model only."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import pytest
from lead_app import community_runtime as community
from lead_app import domain, local_executor, model
from lead_app.db import all_rows, j, one, tx
from lead_app.local_executor_client import execution_context
from lead_app.scope import current_tenant

CAPABILITIES = {"ready": True, "auth_type": "chatgpt", "models": ["gpt-6.1-sol"], "reasoning_efforts": ["medium"]}
INITIAL = "Try https://unverified.example/path"
BAD = "I am a real person."
GOOD = "Use the export menu to save your schedule."
ORIGINAL = {"body": "How can I export my schedule?", "own": False}


@pytest.fixture
def community_agent():
    aid = domain.create_agent(runtime_engine="legacy")
    yield str(aid)
    domain.set_control(aid, "archive")


def task_for(aid):
    with tx(aid) as c:
        return one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,scheduled_at,workflow_id)
            VALUES(%s,%s,'community.engage','running',%s,now(),%s) RETURNING *""",
            (current_tenant(), aid, j({"task_generation": 4}), "draft-repair:" + str(uuid4())))


def decision():
    return community.CommunityDecision(body=INITIAL, relevant_question=True, reason="actual member question", reply_to_mid="42")


def saved(aid, task):
    with tx(aid) as c:
        return one(c, "SELECT result FROM lead.tasks WHERE id=%s", (task["id"],))["result"] or {}


def requests(aid):
    with tx(aid) as c:
        return all_rows(c, "SELECT * FROM lead.local_executor_requests ORDER BY created_at,id")


async def wait_requests(aid, count):
    for _ in range(200):
        rows = requests(aid)
        if len(rows) >= count:
            return rows
        await asyncio.sleep(0.01)
    raise AssertionError("native durable request not created")


class Native:
    def __init__(self, aid, task, outputs, *, paused=False):
        self.aid, self.task, self.outputs = aid, task, outputs
        invite = local_executor.create_pairing(aid)
        self.device = local_executor.pair(invite["code"], "Draft repair fixture", "darwin", CAPABILITIES)
        local_executor.poll(self.device["token"], self.device["epoch"], instance_id=str(uuid4()))
        self.ready = asyncio.Event()
        if not paused:
            self.ready.set()
        self.completed = set()
        self.thread = str(uuid4())

    async def invoke(self, output, prompt, **kwargs):
        context = execution_context.set({"key": "community:" + str(self.task["id"]),
            "task_id": str(self.task["id"]), "session_key": "community:" + str(self.task["id"])})
        try:
            return await model.run_model(output, prompt, **kwargs)
        finally:
            execution_context.reset(context)

    async def serve(self):
        while True:
            await self.ready.wait()
            for row in requests(self.aid):
                if row["id"] in self.completed or row["state"] in local_executor.TERMINAL:
                    continue
                assert len(self.completed) < len(self.outputs), "unexpected additional model request"
                body = self.outputs[len(self.completed)]
                local_executor.events(self.device["token"], self.device["epoch"], [{
                    "id": str(uuid4()), "request_id": str(row["id"]), "method": "result", "params": {
                        "state": "completed", "thread_id": self.thread, "turn_id": str(uuid4()), "output": {"body": body},
                    },
                }])
                self.completed.add(row["id"])
            await asyncio.sleep(0.01)

    @asynccontextmanager
    async def running(self):
        server = asyncio.create_task(self.serve())
        try:
            yield self
        finally:
            server.cancel()
            with suppress(asyncio.CancelledError):
                await server


async def check(aid, task, invoke, draft=None):
    return await asyncio.wait_for(community.check_draft(aid, task, draft or decision(), ORIGINAL, {}, False, invoke), 8)


@pytest.mark.parametrize("last,expected_issue", [(GOOD, None), (INITIAL, "draft_link")])
async def test_native_second_repair_uses_new_feedback_and_stable_round_identity(community_agent, last, expected_issue):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, [BAD, last])
    async with native.running():
        revised, issue = await check(aid, task, native.invoke)
        assert issue == expected_issue
        assert revised.body == last
        rows = requests(aid)
        assert len(rows) == 2
        prompts = [json.loads(row["payload"]["input"])["task"] for row in rows]
        assert [prompt["validation_rule"] for prompt in prompts] == ["draft_link", "false_identity"]
        assert prompts[1]["draft"] == BAD
        assert rows[0]["request_key"] != rows[1]["request_key"]
        assert revised.model_dump(exclude={"body"}) == decision().model_dump(exclude={"body"})
        checkpoint = saved(aid, task)
        assert checkpoint["draft_repair_attempts"] == 2
        assert checkpoint["native_draft_repair"]["body"] == last
        assert checkpoint["native_draft_repair"]["primary_draft_hash"] == hashlib.sha256(INITIAL.encode()).hexdigest()
        assert checkpoint["native_draft_repair"]["task_generation"] == 4
        replay, replay_issue = await check(aid, task, native.invoke)
        assert (replay.body, replay_issue) == (last, expected_issue)
        assert len(requests(aid)) == 2, "completed correction replay cannot start a third model request"


async def test_native_worker_rebuild_reuses_pending_round_without_spending_it(community_agent):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, [GOOD], paused=True)
    async with native.running():
        work = asyncio.create_task(check(aid, task, native.invoke))
        try:
            original = (await wait_requests(aid, 1))[0]
            assert saved(aid, task)["draft_repair_attempts"] == 0
        finally:
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
        native.ready.set()
        revised, issue = await check(aid, task, native.invoke)
        assert (revised.body, issue) == (GOOD, None)
        assert [row["id"] for row in requests(aid)] == [original["id"]]
        checkpoint = saved(aid, task)
        assert checkpoint["draft_repair_attempts"] == 1
        assert checkpoint["native_draft_repair"]["body"] == GOOD
        with tx(aid) as c:
            commands = all_rows(c, "SELECT kind FROM lead.local_executor_commands")
        assert [row["kind"] for row in commands] == ["run"]


async def test_native_completed_result_survives_worker_loss_before_body_checkpoint(community_agent):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, [GOOD])

    async def lose_worker_after_result(*args, **kwargs):
        await native.invoke(*args, **kwargs)
        raise asyncio.CancelledError()

    async with native.running():
        with pytest.raises(asyncio.CancelledError):
            await check(aid, task, lose_worker_after_result)
        original = requests(aid)[0]
        assert original["state"] == "completed"
        assert saved(aid, task)["draft_repair_attempts"] == 0
        revised, issue = await check(aid, task, native.invoke)
        assert (revised.body, issue) == (GOOD, None)
        assert [row["id"] for row in requests(aid)] == [original["id"]]
        assert saved(aid, task)["draft_repair_attempts"] == 1


@pytest.mark.parametrize("change", ["generation", "primary_body"])
async def test_native_repair_checkpoint_cannot_move_to_another_primary_draft_or_generation(community_agent, change):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, [], paused=True)
    async with native.running():
        work = asyncio.create_task(check(aid, task, native.invoke))
        await wait_requests(aid, 1)
        work.cancel()
        with suppress(asyncio.CancelledError):
            await work
        if change == "generation":
            with tx(aid) as c:
                c.execute("UPDATE lead.tasks SET input=input || %s WHERE id=%s", (j({"task_generation": 5}), task["id"]))
            task = {**task, "input": {**task["input"], "task_generation": 5}}
        draft = decision().model_copy(update={"body": INITIAL + " changed"}) if change == "primary_body" else decision()
        with pytest.raises(ValueError, match="draft_repair_scope_changed"):
            await check(aid, task, native.invoke, draft)
        assert len(requests(aid)) == 1


@pytest.mark.parametrize("last,expected_issue", [(GOOD, None), (INITIAL, "draft_link")])
async def test_legacy_keeps_two_repairs_and_its_original_call_contract(community_agent, last, expected_issue):
    aid = str(community_agent)
    task = task_for(aid)
    calls = []

    async def legacy(output, prompt, **kwargs):
        assert "call_key" not in kwargs
        calls.append(prompt)
        assert saved(aid, task)["draft_repair_attempts"] == len(calls)
        return output(body=BAD if len(calls) == 1 else last)

    revised, issue = await check(aid, task, legacy)
    assert (revised.body, issue) == (last, expected_issue)
    assert len(calls) == 2
    assert saved(aid, task)["draft_repair_attempts"] == 2
    assert "native_draft_repair" not in saved(aid, task)
    assert requests(aid) == []


async def test_native_empty_revision_is_persisted_once_and_replayed(community_agent):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, ["   "])
    async with native.running():
        revised, issue = await check(aid, task, native.invoke)
        assert (revised.body, issue) == ("", "empty_revision")
        assert saved(aid, task)["draft_repair_attempts"] == 1
        assert saved(aid, task)["native_draft_repair"]["body"] == ""
        replay, replay_issue = await check(aid, task, native.invoke)
        assert (replay.body, replay_issue) == ("", "empty_revision")
        assert len(requests(aid)) == 1


async def test_an_old_unscoped_counter_cannot_skip_an_unproven_native_round(community_agent):
    aid = str(community_agent)
    task = task_for(aid)
    native = Native(aid, task, [])
    before = {"draft_repair_attempts": 1, "decision": {"placement": "conversation", "include_link": False}}
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j(before), task["id"]))
    with pytest.raises(ValueError, match="draft_repair_scope_changed"):
        await check(aid, task, native.invoke)
    assert saved(aid, task) == before
    assert requests(aid) == []
