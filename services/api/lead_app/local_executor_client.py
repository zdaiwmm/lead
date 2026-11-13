"""Codex request compatibility and asynchronous Activity continuations.

This module schedules no model loop. The official local runtime owns reasoning,
tools, compaction and recovery; business workflows own their original evidence.
"""
import asyncio
import hashlib
import json
from contextvars import ContextVar
from functools import wraps
from uuid import NAMESPACE_URL, uuid5

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
            task.cancel("lead_business_cancel")


activity_jobs = ActivityJobs()


def uses_native(aid=None, *, task_id=None, key=None):
    from . import local_executor
    context = execution_context.get() or {}
    selected = aid or context.get("agent_id")
    if local_executor.enabled(selected):
        return True
    return bool(selected and getattr(local_executor, "bound", lambda *args, **kwargs: False)(selected,
        task_id=task_id or context.get("task_id"), key=key or context.get("key")))


def native_continuation(lane):
    """Let existing business Activities retain their contracts without a long slot."""
    def decorate(function):
        @wraps(function)
        async def run(args):
            aid = args["agent_id"]
            identity = str(args.get("tick") or args.get("cycle_id") or args.get("task_id"))
            key = lane + ":" + str(aid) + ":" + identity
            if not uses_native(aid, key=key, task_id=args.get("task_id") if lane == "publish" else None):
                return await function(args)
            async def continue_work():
                context = execution_context.set({"key": key, "task_id": str(args["task_id"]) if lane == "publish" and args.get("task_id") else None,
                    "session_key": lane + ":" + str(aid) + ":" + identity})
                try:
                    return await function(args)
                finally:
                    execution_context.reset(context)
            return await activity_jobs.step(key, continue_work)
        return run
    return decorate


async def interrupt_request(aid, request_id):
    from . import local_executor
    try:
        await asyncio.shield(asyncio.to_thread(local_executor.command, aid, request_id, "interrupt", {},
            str(uuid5(NAMESPACE_URL, "lead-codex-cancel:" + str(request_id)))))
    except Exception:
        # The original business fence remains authoritative even if the device
        # or database is unavailable; late receipts must remain admissible.
        pass


async def await_request(aid, request_id):
    from . import local_executor

    try:
        while True:
            row = await asyncio.to_thread(local_executor.request_status, aid, request_id)
            state = row["state"]
            if state == "completed":
                return row.get("output", (row.get("result") or {}).get("output"))
            if state in {"failed", "interrupted", "execution_unknown", "canceled"}:
                raise ExecutorUnavailable("local_executor_" + state)
            await asyncio.sleep(1)
    except asyncio.CancelledError as error:
        if error.args and error.args[0] == "lead_business_cancel":
            await interrupt_request(aid, request_id)
        raise


def request_identity(output, prompt, purpose, call_key):
    context = execution_context.get() or {}
    encoded = json.dumps(prompt, ensure_ascii=False, sort_keys=True, default=str)
    fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
    root = context.get("key") or "request:" + fingerprint
    return ":".join((root, output.__name__, purpose or "default", call_key))


def result_transport(output):
    """Use the SDK's strict schema without losing dynamic business dictionaries.

    Strict native output cannot represent arbitrary object keys. Such contracts
    use a JSON string envelope, followed by the unchanged Pydantic validation.
    The original schema travels with the request, never a closed/empty payload.
    """
    from openai import pydantic_function_tool

    schema = pydantic_function_tool(output)["function"]["parameters"]

    def has_open_object(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and node.get("additionalProperties") is not False:
                return True
            return any(has_open_object(value) for value in node.values())
        return isinstance(node, list) and any(has_open_object(value) for value in node)

    if schema.get("type") == "object" and not has_open_object(schema):
        return schema, "structured"
    return {"type": "object", "properties": {"result_json": {"type": "string"}},
        "required": ["result_json"], "additionalProperties": False}, "json_string_v1"


async def run_model(output, prompt, system, *, agent_id=None, purpose=None, call_key="primary", tools=()):
    from . import local_executor

    context = execution_context.get() or {}
    aid = agent_id or context.get("agent_id") or await asyncio.to_thread(local_executor.default_agent)
    if not aid:
        raise ExecutorUnavailable("local_executor_not_connected")
    task_id = context.get("task_id")
    session = context.get("session_key") or ("chat:" + str(aid) if output.__name__ == "Decision" else
        "task:" + str(task_id) if task_id else "model:" + str(aid) + ":" + output.__name__)
    # Public research is supplied by Codex's native web search. Python closures
    # are never serialized or executed on the user's computer.
    public_web = not isinstance(prompt, dict) or prompt.get("managed_tools", {}).get("public_web", True)
    result_schema = output.model_json_schema()
    output_schema, codec = result_transport(output)
    payload = {
        "input": json.dumps(prompt, ensure_ascii=False, default=str),
        "instructions": system + "\nUse the complete native Codex runtime and available tools. "
            "Return the requested structured result. Preserve source attribution; do not invent business receipts. "
            "Register source pages you rely on with lead_public_read so Lead retains the actual evidence.",
        "output_schema": output_schema, "result_schema": result_schema, "result_codec": codec,
        "model": "gpt-6.1-sol", "effort": "medium", "session_key": session,
        "web_search": "live" if public_web else "disabled",
        "tools": [{"name": "lead_public_read", "description": "Read an actual public source and return its observed URL and full text for evidence.",
            "inputSchema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"], "additionalProperties": False}}] if public_web else [],
        "tool_context": {"kind": "model", "task_id": task_id,
            "workspace_request": agent_id is None, **getattr(tools, "local_executor_context", {})},
    }
    if codec == "json_string_v1":
        payload["instructions"] += ("\nThe final response envelope has a result_json string. Encode the COMPLETE "
            "business result as JSON in that string, preserving all dynamic keys and nested values. "
            "The decoded business result must satisfy this original schema:\n" + json.dumps(result_schema, ensure_ascii=False))
    if agent_id is not None:
        payload['tools'].extend({'name':name, 'description':description,
            'inputSchema':{'type':'object','properties':{'id':{'type':'string'}},'required':['id'],'additionalProperties':False}}
            for name,description in (
                ('lead_document_read','Read the complete current source document by its actual evidence ID, including all chunks and source metadata.'),
                ('lead_task_read','Read the original task specification and complete business output by its actual task ID.')))
    key = request_identity(output, prompt, purpose, call_key)
    lookup = getattr(local_executor, "request_by_key", lambda *args: None)
    request = await asyncio.to_thread(lookup, aid, key)
    if request:
        if (request["session_key"] != session or str(request.get("task_id") or "") != str(task_id or "")
                or request["payload"].get("result_schema", request["payload"].get("output_schema")) != result_schema):
            raise ExecutorUnavailable("local_executor_request_contract_changed")
    else:
        request = await asyncio.to_thread(local_executor.submit, aid, key, payload,
            session_key=session, task_id=task_id, resume=True)
    if hasattr(tools, "resume"):
        tools.resume(request.get("payload", {}).get("tool_context") or {})
    result = await await_request(aid, str(request["id"]))
    if isinstance(result, str):
        result = json.loads(result)
    if request.get("payload", payload).get("result_codec") == "json_string_v1":
        result = json.loads(result["result_json"])
    return output.model_validate(result)
