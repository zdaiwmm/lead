import asyncio
import time
from datetime import timedelta
from types import SimpleNamespace

from camoufox.pkgman import launch_path
from lead_app.account_intents import set_intent
from lead_app.account_profiles import for_platform
from lead_app.db import one, tx, uid
from lead_app.execution_routes import register
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.runtime import advance
from lead_app.growth.store import submit_work
from lead_app.growth.tools import prepare_channel, submission_gate
from playwright.async_api import async_playwright
from test_growth_channel_operation import channel_work


async def test_blocked_research_does_not_delay_content_or_receipt_observation(growth_agent,monkeypatch):
    import json

    from lead_app.config import STATE
    from lead_app.growth import tools
    from lead_app.growth.runtime import execute, finish
    from test_growth_runtime import new_work
    new_work(growth_agent)
    entered,release=asyncio.Event(),asyncio.Event()
    async def fetch(url,**kwargs):
        entered.set()
        await release.wait()
        return {'url':url,'title':'Original source','text':'A bounded original public source.','links':[]}
    async def draft(schema,*args,**kwargs):
        return schema(body='A factual explanation of the supplied product.',title='Product explanation',claim_checks=[])
    monkeypatch.setattr(tools,'fetch_product',fetch)
    monkeypatch.setattr(tools,'run_model',draft)
    with tx(growth_agent) as c:
        refs=[submit_work(c,growth_agent,TaskSpec(kind=kind,purpose=purpose,payload=payload,
            review_at=utcnow()+timedelta(hours=1))) for kind,purpose,payload in [
                ('research','blocked-source',{'urls':['https://original.example/source']}),
                ('content','explain',{'brief':'Explain the supplied product without unsupported claims'}),
                ('observe','outcomes',{})]]
    claims=[advance(ref)['claim'] for ref in refs]
    blocked=asyncio.create_task(execute(claims[0]))
    try:
        await asyncio.wait_for(entered.wait(),3)
        started=time.monotonic()
        results=await asyncio.wait_for(asyncio.gather(*(execute(claim) for claim in claims[1:])),5)
        delay=time.monotonic()-started
        assert not blocked.done() and all(result['outcome']=='completed' for result in results)
        for claim,result in zip(claims[1:],results,strict=True):
            assert finish(claim,result)['accepted_control']
        report=STATE.parent/'reports'
        report.mkdir(exist_ok=True)
        (report/'blocked-independence.json').write_text(json.dumps({'independent_completion_seconds':delay,
            'blocked_research_still_running':not blocked.done(),'resources':['public_source','content_model','outcome_ledger'],
            'original_task_refs':refs,'external_social_actions':0}))
    finally:
        release.set()
        result=await asyncio.wait_for(blocked,3)
        finish(claims[0],result)


async def test_parallel_pause_order(growth_agent):
    ref,data,first = channel_work(growth_agent)
    extra = for_platform(growth_agent,"telegram",create=True,new_binding=True)
    with tx(growth_agent) as c:
        second = one(c,"""UPDATE lead.social_accounts SET platform='lab',state='connected',external_account_id=%s
            WHERE id=%s RETURNING *""",("synthetic-"+uid(),extra["account_id"]))
        target = one(c,"""INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
          canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,'channel','second',
          'http://local-fixture/second','synthetic second','{}','active') RETURNING id""",(growth_agent,second["id"]))
        c.execute("""INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by)
          VALUES(lead.ctx_tenant(),%s,%s,1,'["publish"]','{}','synthetic owner')""",(growth_agent,target["id"]))
    set_intent(growth_agent,second["id"],"running",source="owner_command",source_ref="synthetic:second")
    register(growth_agent,second["profile_id"])
    with tx(growth_agent) as c:
        other = submit_work(c,growth_agent,TaskSpec(kind="channel",purpose="publish",account_id=second["id"],target_id=target["id"],
            payload=data.model_dump(mode="json"),review_at=utcnow()+timedelta(hours=1)))
        c.execute("""UPDATE lead.profile_leases SET state='held',epoch=1,expires_at=now()+interval '10 minutes'
          WHERE profile_id=ANY(%s)""",([first["profile_id"],second["profile_id"]],))
    actions,claims = [],[]
    for item in (ref,other):
        claimed=advance(item)
        assert claimed["state"] == "ready"
        claims.append(claimed["claim"])
        actions.append(prepare_channel(growth_agent,item["task_id"],data))
    actors = [SimpleNamespace(aid=growth_agent,profile={"id":a["profile_id"]},epoch=1) for a in (first,second)]
    barrier = asyncio.Barrier(2)
    intervals, pauses = {}, []
    async with async_playwright() as playwright:
        browser = await playwright.firefox.launch(executable_path=launch_path(),headless=True)
        try:
            contexts = [await browser.new_context() for _ in actors]
            pages = []
            for context in contexts:
                await context.route("**/*",lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('<button data-submissions="0" onclick="this.dataset.submissions=String(Number(this.dataset.submissions)+1)">Publish fixture</button>')
                pages.append(page)
            async def commit(index):
                with tx(growth_agent) as c:
                    submission_gate(c,actors[index],actions[index],claims[index])
                    intervals[index] = [time.monotonic(),None]
                    await asyncio.wait_for(barrier.wait(),5)
                    await pages[index].get_by_role("button",name="Publish fixture").click()
                    await asyncio.sleep(.15)
                    intervals[index][1] = time.monotonic()
            async def pause():
                while len(intervals)<2:
                    await asyncio.sleep(.01)
                await asyncio.to_thread(set_intent,growth_agent,first["id"],"paused",source="owner_command",source_ref="synthetic:pause")
                pauses.append(time.monotonic())
            await asyncio.wait_for(asyncio.gather(commit(0),commit(1),pause()),12)
            assert max(intervals[0][0],intervals[1][0]) < min(intervals[0][1],intervals[1][1])
            assert pauses[0] >= intervals[0][1]
            assert [await page.get_by_role("button").get_attribute("data-submissions") for page in pages] == ["1","1"]
            import json

            from lead_app.config import STATE
            report = STATE.parent / "reports"
            report.mkdir(exist_ok=True)
            (report/"parallel-pause.json").write_text(json.dumps({"intervals":intervals,"pause_completed_at":pauses[0],
                "local_clicks":[1,1],"external_actions":0,"independent_browser_contexts":2}))
        finally:
            await browser.close()
