import pytest
from lead_app.growth import tools


@pytest.mark.asyncio
async def test_browser_transport_failure_keeps_browser_scope(monkeypatch):
    from lead_app import browser_tools

    async def failed(*args, **kwargs):
        raise OSError("fixture transport unavailable")

    class Browser:
        def __init__(self, *args, **kwargs):
            pass

        command = failed

        async def finish(self, state):
            return []

    monkeypatch.setattr(tools, "fetch_product", failed)
    monkeypatch.setattr(browser_tools, "BrowserTools", Browser)
    with pytest.raises(ValueError) as caught:
        await tools._read_source("fixture", {"id": "task"}, tools.ResearchInput(), "https://forum.example/topic")
    assert caught.value.failure_scope["provider"] == "browser"


def test_reply_requires_original_parent_and_keeps_operation():
    from uuid import uuid4

    args = {"content_id": uuid4(), "content_hash": "a" * 64, "operation": "reply"}
    with pytest.raises(ValueError):
        tools.ChannelInput(**args)
    value = tools.ChannelInput(**args, parent_url="https://forum.example/post/1")
    assert value.operation == "reply"
    assert value.parent_url == "https://forum.example/post/1"


def test_experience_failure_occurs_after_durable_task_completion(growth_agent, monkeypatch):
    from lead_app import experiences
    from lead_app.db import one, tx
    from lead_app.growth import runtime
    from lead_app.growth.contracts import WorkResult
    from test_growth_runtime import new_work

    calls = []

    def unavailable(aid, task_id):
        with tx(aid) as c:
            assert one(c, "SELECT state FROM lead.tasks WHERE id=%s", (task_id,))["state"] == "succeeded"
        calls.append(task_id)
        raise OSError("optional extraction unavailable")

    monkeypatch.setattr(experiences, "extract_task", unavailable)
    ref = new_work(growth_agent)
    claim = runtime.advance(ref)["claim"]
    result = WorkResult(outcome="completed")
    assert runtime.finish(claim, result)["accepted_control"]
    assert calls == [ref["task_id"]]
    assert not runtime.finish(claim, result)["accepted_control"]
    assert calls == [ref["task_id"]]


@pytest.mark.asyncio
async def test_optional_provider_failure_keeps_other_source_and_reason(growth_agent, monkeypatch):
    from datetime import timedelta

    from lead_app import research_providers
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.store import submit_work

    calls = []

    async def unavailable(aid, task_id, provider, operation, args, call_key, **kwargs):
        calls.append(call_key)
        return {"state": "unavailable", "sources": [], "reason": "missing_key",
                "failure_scope": {"provider": provider, "operation": operation,
                                  "endpoint": "https://api.tavily.com/search", "access_mode": "provider_public"}}

    async def read(url, **kwargs):
        return {"url": url, "title": "Independent source", "text": "The product creates quotes for small teams."}

    monkeypatch.setattr(research_providers, "research_call", unavailable)
    monkeypatch.setattr(tools, "fetch_product", read)
    spec = TaskSpec(kind="research", purpose="product", review_at=utcnow()+timedelta(hours=1),
                    payload={"urls": ["https://example.com"], "provider_calls": [
                        {"provider": "tavily", "operation": "web.search", "args": {"query": "quotes"}}]})
    with tx(growth_agent) as c:
        task = submit_work(c, growth_agent, spec)
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task["task_id"],))
    first = await tools.research(growth_agent, row, spec)
    second = await tools.research(growth_agent, row, spec)
    assert first.outcome == second.outcome == "completed"
    assert first.output["read_errors"][0]["failure_scope"]["provider"] == "tavily"
    assert first.output["pages"][0]["url"] == "https://example.com"
    assert calls[0] == calls[1]


@pytest.mark.asyncio
async def test_same_post_from_two_providers_is_one_cited_source(growth_agent, monkeypatch):
    from datetime import timedelta

    from lead_app import research_providers
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.store import submit_work

    async def response(aid, task_id, provider, operation, args, call_key, **kwargs):
        return {"state": "complete", "sources": [{"source_id": "same-original", "url": "https://example.com/post/1",
                "provider": provider, "body": "Original source evidence " + provider, "excerpt": "Original post"}]}

    monkeypatch.setattr(research_providers, "research_call", response)
    spec = TaskSpec(kind="research", purpose="product", review_at=utcnow()+timedelta(hours=1), payload={"provider_calls": [
        {"provider": "tavily", "operation": "web.search", "args": {"query": "quotes"}},
        {"provider": "tikhub", "operation": "social.search", "args": {"platform": "reddit", "query": "quotes"}}]})
    with tx(growth_agent) as c:
        task = submit_work(c, growth_agent, spec)
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task["task_id"],))
    result = await tools.research(growth_agent, row, spec)
    assert result.outcome == "completed"
    assert len(result.evidence_refs) == len(result.output["pages"]) == 1
    assert result.output["pages"][0]["independently_verified"] is False
    with tx(growth_agent) as c:
        document = one(c, "SELECT source_type FROM lead.knowledge_documents WHERE id=%s", (result.evidence_refs[0].id,))
    assert document["source_type"] == "provider:tavily"


@pytest.mark.asyncio
async def test_blocked_source_keeps_actual_origin_and_access_mode(monkeypatch):
    async def blocked(url, **kwargs):
        return {"url": url, "title": "Access denied", "text": "Blocked by network security"}

    monkeypatch.setattr(tools, "fetch_product", blocked)
    with pytest.raises(ValueError) as caught:
        await tools._read_source("fixture", {"id": "task"}, tools.ResearchInput(), "https://forum.example/topic")
    scope = caught.value.failure_scope
    assert scope["origin"] == "https://forum.example"
    assert scope["operation"] == "read"
    assert scope["access_mode"] == "public"
    assert scope["condition"] == "access_denied"


@pytest.mark.asyncio
async def test_empty_source_is_not_successful_recovery(monkeypatch):
    async def empty(url, **kwargs):
        return {"url": url, "title": "", "text": ""}

    monkeypatch.setattr(tools, "fetch_product", empty)
    with pytest.raises(ValueError) as caught:
        await tools._read_source("fixture", {"id": "task"}, tools.ResearchInput(), "https://forum.example/topic")
    assert caught.value.failure_scope["condition"] == "empty"


@pytest.mark.asyncio
async def test_account_read_never_silently_uses_public_fetch(monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Account request must not read an anonymous HTTP session")

    from lead_app import browser_tools
    calls=[]
    class AccountBrowser:
        def __init__(self, aid, question, **kwargs):
            calls.append(kwargs)
        async def command(self, operation, args):
            return {"url":args["url"],"text":"Bound account source content","title":"Account page"}
        async def finish(self, state):
            calls.append(state)
    monkeypatch.setattr(browser_tools,"BrowserTools",AccountBrowser)
    monkeypatch.setattr(tools, "fetch_product", unexpected)
    data = tools.ResearchInput(access_mode="account", account_id="00000000-0000-0000-0000-000000000001")
    page=await tools._read_source("fixture", {"id": "task"}, data, "https://forum.example/topic")
    assert page["attempt_scope"]["provider"] == "account_browser"
    assert page["access_mode"] == "account"
    assert calls == [{"task_id":"task","mode":"autonomous"},"succeeded"]
