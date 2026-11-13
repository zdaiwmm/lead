"""Codex request compatibility and asynchronous Activity continuations.

This module schedules no model loop. The official local runtime owns reasoning,
tools, compaction and recovery; business workflows own their original evidence.
"""
import asyncio
import hashlib
import json
from contextvars import ContextVar

execution_context = ContextVar("local_executor_context", default=None)


class ExecutorUnavailable(ValueError):
    pass


class ActivityJobs:
    """Keep a live coroutine between short Activities; durable requests replay it.

    Losing this process loses only the coroutine. Re-entry uses the same business
    claim and immutable model request keys, never a fresh sending identity.
    """
    def __init__(self):
        self.jobs = {}

    async def step(self, key, factory):
        task = self.jobs.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self.jobs[key] = task
        if not task.done():
            return {"executor_pending": True}
        try:
            return task.result()
        finally:
            self.jobs.pop(key, None)

    def cancel(self, key):
        task = self.jobs.pop(key, None)
        if task and not task.done():
            task.cancel()


activity_jobs = ActivityJobs()


async def await_request(aid, request_id):
    from . import local_executor

    while True:
        row = await asyncio.to_thread(local_executor.request_status, aid, request_id)
        state = row["state"]
        if state == "completed":
            return row.get("output", (row.get("result") or {}).get("output"))
        if state in {"failed", "interrupted", "execution_unknown", "canceled"}:
            raise ExecutorUnavailable("local_executor_" + state)
        await asyncio.sleep(1)


def request_identity(output, prompt, purpose, call_key):
    context = execution_context.get() or {}
    encoded = json.dumps(prompt, ensure_ascii=False, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    root = context.get("key") or "request"
    return ":".join((root, output.__name__, purpose or "default", call_key, fingerprint))


async def run_model(output, prompt, system, *, agent_id=None, purpose=None, call_key="primary", tools=()):
    from . import local_executor

    aid = agent_id or await asyncio.to_thread(local_executor.default_agent)
    if not aid:
        raise ExecutorUnavailable("local_executor_not_connected")
    context = execution_context.get() or {}
    task_id = context.get("task_id")
    session = context.get("session_key") or ("chat:" + str(aid) if output.__name__ == "Decision" else
        "task:" + str(task_id) if task_id else "model:" + str(aid) + ":" + output.__name__)
    # Public research is supplied by Codex's native web search. Python closures
    # are never serialized or executed on the user's computer.
    public_web = not isinstance(prompt, dict) or prompt.get("managed_tools", {}).get("public_web", True)
    payload = {
        "input": json.dumps(prompt, ensure_ascii=False, default=str),
        "instructions": system + "\nUse the complete native Codex runtime and available tools. "
            "Return the requested structured result. Preserve source attribution; do not invent business receipts.",
        "output_schema": output.model_json_schema(),
        "model": "gpt-6.1-sol", "effort": "medium", "session_key": session,
        "web_search": "live" if public_web else "disabled",
        "tools": [{"name": "lead_public_read", "description": "Read an actual public source and return its observed URL and full text for evidence.",
            "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}}] if public_web else [],
        "tool_context": {"kind": "model", "task_id": task_id,
            "workspace_request": agent_id is None},
    }
    request = await asyncio.to_thread(local_executor.submit, aid,
        request_identity(output, prompt, purpose, call_key), payload,
        session_key=session, task_id=task_id)
    result = await await_request(aid, str(request["id"]))
    if isinstance(result, str):
        result = json.loads(result)
    return output.model_validate(result)
