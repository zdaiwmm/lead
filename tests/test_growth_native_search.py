"""Native discovery supplies candidates; actual reads remain the evidence boundary."""
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app import admin_settings, browser_tools, local_executor_client
from lead_app.db import all_rows, one, tx
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work

SOURCE = "Independent makers need to compare quote line items without retyping each offer."


def search_work(aid, payload):
    new_work(aid)
    with tx(aid) as c:
        ref = submit_work(c, aid, TaskSpec(kind="research", purpose="demand", payload=payload,
            review_at=utcnow()+timedelta(hours=1)))
        return ref, one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))


@pytest.mark.asyncio
async def test_native_queries_discover_with_original_task_then_store_only_actual_source(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)
    calls = []

    async def discover(output, prompt, **kwargs):
        calls.append((prompt, kwargs, dict(local_executor_client.execution_context.get())))
        return output(urls=["https://forum.example/original"])

    async def read(url, **kwargs):
        assert "duckduckgo.com" not in url
        return {"url": url, "title": "Original request", "text": SOURCE, "links": []}

    monkeypatch.setattr(tools, "run_model", discover)
    monkeypatch.setattr(tools, "fetch_product", read)
    ref, _ = search_work(growth_agent, {"question": "Who needs quote comparison?", "queries": ["quote comparison needs"]})
    claim = runtime.advance(ref)["claim"]
    result = WorkResult.model_validate(await runtime.execute(claim))
    assert result.outcome == "completed" and result.evidence_refs
    assert len(calls) == 1
    assert calls[0][0]["queries"] == ["quote comparison needs"]
    assert calls[0][1]["agent_id"] == growth_agent
    assert calls[0][2]["task_id"] == ref["task_id"]
    assert calls[0][2]["key"] == "task:"+ref["task_id"]+":1"
    with tx(growth_agent) as c:
        documents = all_rows(c, "SELECT source_ref FROM lead.knowledge_documents")
        assert documents == [{"source_ref": "https://forum.example/original"}]
    assert not result.output["learning_progress"] and not result.output["business_progress"]


@pytest.mark.asyncio
async def test_account_query_uses_public_discovery_and_keeps_explicit_account_source(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)
    account_id = uuid4()
    reads = []

    async def discover(output, prompt, **kwargs):
        assert "account_id" not in prompt
        return output(urls=["https://forum.example/public"])

    async def direct(url, **kwargs):
        reads.append(("public", url))
        return {"url": url, "title": "Public request", "text": SOURCE, "links": []}

    class AccountBrowser:
        def __init__(self, *args, **kwargs):
            pass

        async def command(self, operation, args):
            reads.append(("account", args["url"]))
            return {"url": args["url"], "title": "Account request", "text": SOURCE+" Account context."}

        async def finish(self, state):
            pass

    def allowed(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "run_model", discover)
    monkeypatch.setattr(tools, "fetch_product", direct)
    monkeypatch.setattr(browser_tools, "BrowserTools", AccountBrowser)
    monkeypatch.setattr(runtime, "check_research_resource", allowed)
    spec = TaskSpec(kind="research", purpose="demand", account_id=account_id,
        payload={"question": "Who needs quote comparison?", "queries": ["quote comparison needs"],
            "access_mode": "account", "account_id": str(account_id), "urls": ["https://reddit.example/thread"]},
        review_at=utcnow()+timedelta(hours=1))
    result = await tools.research(growth_agent, {"id": uuid4()}, spec)
    assert result.outcome == "completed"
    assert reads == [("account", "https://reddit.example/thread"), ("public", "https://forum.example/public")]
    with tx(growth_agent) as c:
        assert all_rows(c, "SELECT source_ref,source_type FROM lead.knowledge_documents ORDER BY source_ref") == [
            {"source_ref": "https://forum.example/public", "source_type": "public_read"},
            {"source_ref": "https://reddit.example/thread", "source_type": "account_read"}]


@pytest.mark.asyncio
async def test_native_search_candidate_blocked_page_is_not_evidence(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)

    async def discover(output, prompt, **kwargs):
        return output(urls=["https://forum.example/blocked"])

    async def blocked(url, **kwargs):
        return {"url": url, "title": "Blocked", "text": "You've been blocked by network security."}

    monkeypatch.setattr(tools, "run_model", discover)
    monkeypatch.setattr(tools, "fetch_product", blocked)
    _, row = search_work(growth_agent, {"queries": ["quote comparison needs"]})
    result = await tools.execute_tool(growth_agent, row, {})
    assert result.outcome == "failed" and result.failure_code == "source_unavailable"
    assert result.output["read_errors"][0]["failure_scope"]["origin"] == "https://forum.example"
    assert result.output["read_errors"][0]["code"] == "access_denied"
    assert not result.evidence_refs
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.knowledge_documents")["n"] == 0


@pytest.mark.asyncio
async def test_native_search_failure_retains_independent_actual_source(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)

    async def unavailable(*args, **kwargs):
        raise OSError("Synthetic discovery service outage")

    async def read(url, **kwargs):
        return {"url": url, "title": "Original request", "text": SOURCE, "links": []}

    monkeypatch.setattr(tools, "run_model", unavailable)
    monkeypatch.setattr(tools, "fetch_product", read)
    _, row = search_work(growth_agent, {"queries": ["quote comparison needs"],
        "urls": ["https://forum.example/original"]})
    result = await tools.execute_tool(growth_agent, row, {})
    assert result.outcome == "completed" and result.evidence_refs
    assert result.output["read_errors"] == [{"code": "search_model_unavailable", "error_type": "OSError"}]
    with tx(growth_agent) as c:
        assert all_rows(c, "SELECT source_ref FROM lead.knowledge_documents") == [
            {"source_ref": "https://forum.example/original"}]


@pytest.mark.asyncio
async def test_native_search_failure_without_actual_source_keeps_original_failure(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)

    async def unavailable(*args, **kwargs):
        raise OSError("Synthetic discovery service outage")

    monkeypatch.setattr(tools, "run_model", unavailable)
    _, row = search_work(growth_agent, {"queries": ["quote comparison needs"]})
    with pytest.raises(OSError, match="Synthetic discovery service outage"):
        await tools.execute_tool(growth_agent, row, {})


@pytest.mark.asyncio
async def test_disabled_public_web_never_starts_native_search(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: True)
    original = admin_settings.for_agent

    def settings(aid, key):
        body, revision = original(aid, key)
        return {**body, "tools": {**body["tools"], "public_web": False}}, revision

    async def forbidden(*args, **kwargs):
        pytest.fail("Disabled public search must not call model or network")

    monkeypatch.setattr(admin_settings, "for_agent", settings)
    monkeypatch.setattr(tools, "run_model", forbidden)
    monkeypatch.setattr(tools, "fetch_product", forbidden)
    _, row = search_work(growth_agent, {"queries": ["quote comparison needs"]})
    result = await tools.execute_tool(growth_agent, row, {})
    assert result.outcome == "failed" and not result.evidence_refs


@pytest.mark.asyncio
async def test_legacy_account_search_cannot_fall_back_into_account_browser(growth_agent, monkeypatch):
    monkeypatch.setattr(local_executor_client, "uses_native", lambda *args, **kwargs: False)
    account_id = uuid4()
    browser_urls = []

    async def index(url, **kwargs):
        assert url.startswith("https://html.duckduckgo.com/")
        return {"url": url, "title": "Search", "text": "JavaScript loading", "requires_javascript": True}

    class AccountBrowser:
        def __init__(self, *args, **kwargs):
            pass

        async def command(self, operation, args):
            browser_urls.append(args["url"])
            return {"url": args["url"], "title": "Account request", "text": SOURCE}

        async def finish(self, state):
            pass

    def allowed(*args, **kwargs):
        return None

    monkeypatch.setattr(tools, "fetch_product", index)
    monkeypatch.setattr(browser_tools, "BrowserTools", AccountBrowser)
    monkeypatch.setattr(runtime, "check_research_resource", allowed)
    spec = TaskSpec(kind="research", purpose="demand", account_id=account_id,
        payload={"queries": ["quote comparison needs"], "access_mode": "account", "account_id": str(account_id),
            "urls": ["https://reddit.example/thread"]}, review_at=utcnow()+timedelta(hours=1))
    result = await tools.research(growth_agent, {"id": uuid4()}, spec)
    assert result.outcome == "completed" and browser_urls == ["https://reddit.example/thread"]
    assert result.output["read_errors"][0]["code"] == "capability_gap"
    assert result.output["read_errors"][0]["failure_scope"]["access_mode"] == "public"
