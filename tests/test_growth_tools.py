from datetime import timedelta

import pytest
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


@pytest.mark.asyncio
async def test_cold_start_no_gates(growth_agent, monkeypatch):
    from lead_app.growth import tools
    calls = []

    async def read(url, **kwargs):
        calls.append(url)
        return {"url": url, "title": "Product", "text": "A quote builder for independent makers.", "links": []}

    monkeypatch.setattr(tools, "fetch_product", read)
    with tx(growth_agent) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs)
          VALUES(lead.ctx_tenant(),%s,1,'https://example.com','Quote builder','{}','[]')""", (growth_agent,))
        ref = submit_work(c, growth_agent, TaskSpec(kind="research", purpose="product", review_at=utcnow()+timedelta(hours=1)))
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
    first = await tools.execute_tool(growth_agent, row, {})
    again = await tools.execute_tool(growth_agent, row, {})
    assert calls == ["https://example.com", "https://example.com"]
    assert first.outcome == "completed" and first.output["knowledge_progress"]
    assert not again.output["knowledge_progress"]
    assert first.evidence_refs[0].kind == "document"
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.knowledge_documents")["n"] == 1


@pytest.mark.asyncio
async def test_untrusted_tools(growth_agent):
    with pytest.raises(ValueError):
        with tx(growth_agent) as c:
            submit_work(c, growth_agent, TaskSpec(kind="research", purpose="demand",
                payload={"sql": "UPDATE authority", "urls": ["http://127.0.0.1/admin"]},
                review_at=utcnow()+timedelta(hours=1)))
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.research'")["n"]==0


@pytest.mark.asyncio
async def test_content_without_account_or_plan(growth_agent, monkeypatch):
    from lead_app.growth import tools

    async def draft(output, prompt, **kwargs):
        return output(body="Build and compare your quote in one place.", title="A simpler quote", claim_checks=[])

    monkeypatch.setattr(tools, "run_model", draft)
    with tx(growth_agent) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,product_url,description,facts,source_refs)
          VALUES(lead.ctx_tenant(),%s,1,'https://example.com','Quote builder',%s,'[]')""",
          (growth_agent, j({"capabilities": ["build and compare quotes"]})))
        ref = submit_work(c, growth_agent, TaskSpec(kind="content", purpose="draft", payload={"brief": "Explain quote comparison"},
            review_at=utcnow()+timedelta(hours=1)))
        row = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
    result = await tools.execute_tool(growth_agent, row, {})
    assert result.outcome == "completed"
    with tx(growth_agent) as c:
        content = one(c, "SELECT * FROM lead.content_items WHERE id=%s", (result.artifacts[0]["id"],))
        assert content["plan_id"] is None and content["target_id"] is None
        assert content["validation_result"]["publication_status"] == "draft"


async def test_inspecting_saved_source_is_bounded_and_not_new_evidence(growth_agent):
    from lead_app.growth import tools
    document,_ = tools._remember(growth_agent,{"url":"https://example.com/source","title":"Source","text":"Observed product evidence."})
    refs = []
    for purpose in ("inspect","renamed"):
        with tx(growth_agent) as c:
            ref = submit_work(c,growth_agent,TaskSpec(kind="research",purpose=purpose,
                payload={"document_ids":[str(document.id)]},review_at=utcnow()+timedelta(hours=1)))
            refs.append(ref)
            row = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
        result = await tools.execute_tool(growth_agent,row,{})
        assert result.outcome=="completed" and result.output["context_available"]
        assert not result.output["knowledge_progress"]
    assert refs[0]==refs[1]
