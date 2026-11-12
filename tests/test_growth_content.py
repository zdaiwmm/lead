from datetime import timedelta

import pytest
from lead_app import assets
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.runtime import advance
from lead_app.growth.store import submit_work


async def content_work(aid,monkeypatch,kind="image",optional=False):
    from lead_app.growth import tools
    async def draft(output,*args,**kwargs):
        return output(body="Build and compare quotes in one place.",title="Compare quotes",claim_checks=[])
    monkeypatch.setattr(tools,"run_model",draft)
    with tx(aid) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs)
            VALUES(lead.ctx_tenant(),%s,1,'A quote builder',%s,'[]')""",(aid,j({"capabilities":["compare quotes"]})))
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(aid,))
        ref = submit_work(c,aid,TaskSpec(kind="content",purpose="explain",payload={"brief":"Explain comparison",
            "format":kind,"platform":"facebook","optional_media":optional},review_at=utcnow()+timedelta(hours=1)))
    advance(ref)
    with tx(aid) as c:
        row = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
    return await tools.execute_tool(aid,row,{})


@pytest.mark.asyncio
async def test_real_render_is_inside_content_macro(growth_agent,monkeypatch):
    result = await content_work(growth_agent,monkeypatch)
    assert result.outcome=="completed"
    with tx(growth_agent) as c:
        content = one(c,"SELECT * FROM lead.content_items WHERE id=%s",(result.artifacts[0]["id"],))
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='media.generate'")["n"]==0
        assert content["body"]["format"]=="image" and len(content["body"]["media"])==1
    item = content["body"]["media"][0]
    asset,path = assets.resolve(growth_agent,item["id"],item["sha256"])
    from PIL import Image
    with Image.open(path) as picture:
        assert min(picture.size)>=600
        picture.verify()
    assert asset["rights"]["ai_generated"] is True


@pytest.mark.asyncio
async def test_optional_video_failure_keeps_text_path(growth_agent,monkeypatch):
    from lead_app import creative_pipeline
    async def fail(*args,**kwargs):
        raise ValueError("synthetic video renderer failure")
    monkeypatch.setattr(creative_pipeline,"produce",fail)
    result = await content_work(growth_agent,monkeypatch,"video",True)
    assert result.outcome=="completed"
    with tx(growth_agent) as c:
        content = one(c,"SELECT body,validation_result FROM lead.content_items WHERE id=%s",(result.artifacts[0]["id"],))
        assert content["body"]["format"]=="text" and not content["body"]["media"]
        assert content["validation_result"]["optional_media_missing"]


@pytest.mark.parametrize("body",["Increase conversions by 80% with this tool.","A customer said this tool changed their business."])
async def test_empty_claim_list_cannot_bless_generated_efficacy_or_testimonial(growth_agent,monkeypatch,body):
    from lead_app.growth import tools
    async def draft(output,*args,**kwargs):
        return output(body=body,title="Unsupported",claim_checks=[])
    monkeypatch.setattr(tools,"run_model",draft)
    with tx(growth_agent) as c:
        ref = submit_work(c,growth_agent,TaskSpec(kind="content",purpose="explain",payload={"brief":"Explain the product"},review_at=utcnow()+timedelta(hours=1)))
        row = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
    with pytest.raises(ValueError,match="unsupported_content_claim"):
        await tools.execute_tool(growth_agent,row,{})
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.content_items")["n"]==0
