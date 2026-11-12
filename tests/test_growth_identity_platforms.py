import asyncio
from uuid import UUID

import pytest
from lead_app import browser
from lead_app.account_connection import observe_identity
from lead_app.account_profiles import public_research
from lead_app.db import one, tx, uid
from test_adapters import TARGETS, FixtureSite


@pytest.mark.asyncio
async def test_eleven_platform_identity_contracts(growth_agent):
    gateway = browser.Gateway()
    profile = public_research(growth_agent)
    try:
        actor = await gateway.get(growth_agent,profile["id"])
        results = {}
        for platform,url in TARGETS.items():
            context = await actor.context.browser.new_context()
            page = await context.new_page()
            site = FixtureSite(platform)
            await context.route("**/*",site.route)
            if platform=="facebook":
                await context.add_cookies([{"name":"c_user","value":"10001","url":"https://www.facebook.com"}])
            await page.goto("https://web.whatsapp.com/" if platform=="whatsapp" else url)
            observed = await observe_identity(page,platform,actor.session)
            assert observed.status=="authenticated",(platform,observed)
            assert observed.operating_subject.namespace==platform and observed.evidence_refs
            assert observed.operating_subject.external_id
            assert not site.posts and not site.uploads
            results[platform] = observed.operating_subject.id_kind
            await context.close()
        assert len(results)==11
    finally:
        await gateway.close(growth_agent)


@pytest.mark.asyncio
async def test_slow_identity_read_does_not_lock_human_input(growth_agent,monkeypatch):
    from lead_app import adapters, domain
    domain.connect_lab(growth_agent)
    with tx(growth_agent) as c:
        social = one(c,"SELECT * FROM lead.social_accounts WHERE platform='lab'")
        c.execute("UPDATE lead.social_accounts SET platform='facebook' WHERE id=%s",(social["id"],))
    gateway = browser.Gateway()
    monkeypatch.setattr(browser,"gateway",gateway)
    actor = await gateway.get(growth_agent,social["profile_id"])
    await actor.context.route("**/*",lambda route:route.fulfill(body='<textarea aria-label="Human input"></textarea>',content_type="text/html"))
    await actor.page.goto('https://www.facebook.com/')
    with tx(growth_agent) as c:
        takeover = one(c,"""INSERT INTO lead.takeover_sessions(tenant_id,agent_id,browser_session_id,profile_id,
            token_hash,control_version,purpose,state,expires_at) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,1,'login','active',now()+interval '30 minutes') RETURNING id""",
            (growth_agent,actor.session,social["profile_id"],uid()))["id"]
    entered,release = asyncio.Event(),asyncio.Event()
    class SlowReader:
        async def account(self,page):
            entered.set()
            await release.wait()
            return {"platform":"facebook","external_account_id":"10001"}
    monkeypatch.setattr(adapters,"get_adapter",lambda platform:SlowReader())
    observing = asyncio.create_task(browser.observe_account(UUID(growth_agent),browser.OpenRequest(platform="facebook",account_id=social["id"],takeover=takeover)))
    try:
        await asyncio.wait_for(entered.wait(),3)
        async def human():
            async with browser.manual_control(actor):
                await actor.page.get_by_label("Human input").fill("最后一段输入")
        await asyncio.wait_for(human(),.5)
        assert await actor.page.get_by_label("Human input").input_value()=="最后一段输入"
        release.set()
        assert (await observing)["status"]=="authenticated"
    finally:
        release.set()
        await asyncio.gather(observing,return_exceptions=True)
        await gateway.close(growth_agent)
