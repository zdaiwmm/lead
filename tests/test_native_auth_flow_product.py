"""An observed SSO popup returns to its original ConnectionRef and binds self."""
import asyncio
import os
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
@pytest.mark.parametrize('owner_entry',[False,True])
async def test_observed_popup_owner_identity_and_callback_complete_original_connection(monkeypatch,owner_entry):
    import test_native_boundary_product as boundary
    from lead_app import api, browser
    from lead_app.account_connection import execute_account
    from lead_app.db import one, tx
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.tools import AccountInput
    from lead_app.security import internal_token, token
    route=next(route for route in browser.app.routes if getattr(route,'path',None)=='/agents/{aid}/input')
    original_input=route.dependant.call
    async def observed_input(aid,body):
        try:
            return await original_input(aid,body)
        except ValueError as error:
            print('AUTH_OWNER_INPUT_REJECTED',type(error).__name__,str(error))
            raise
    monkeypatch.setattr(route.dependant,'call',observed_input)

    async def check(data):
        actor,controller,page,site=(data[key] for key in ('actor','controller','page','site'))
        aid=actor.aid
        task,claim,pending=data['task'],data['claim'],data['pending']
        connection=str(pending['takeover_id'])
        initial_calls=len(data['context']['model_calls'])
        await actor.context.clear_cookies(name='board_session',domain=site.hosts['main'])
        await page.goto(site.origin()+'/home',wait_until='domcontentloaded')
        # An unowned opener in the same managed context has no ConnectionRef.
        # It must not consume a first-hop SSO request before a role is reserved.
        outsider=await actor.context.new_page()
        try:
            await outsider.evaluate('(url)=>{window.open(url,"wrong-opener");}',site.origin()+'/sso/start')
            await asyncio.sleep(.15)
            assert data['source_count']('GET','/sso/start')==0
        finally:
            await outsider.close()
        if owner_entry:
            await controller.handoff('请在原窗口选择身份登录并完成本人步骤。')
            waiting=await execute_account(aid,task,AccountInput.model_validate(task['input']['spec']['payload']))
            finish(claim,waiting)
            with tx(aid) as c:
                version=one(c,'SELECT challenge_version FROM lead.takeover_sessions WHERE id=%s',(connection,))['challenge_version']
            bounds=await page.get_by_role('button',name='Use identity sign-in').bounding_box()
            async with actor.context.expect_page() as opened:
                async with httpx.AsyncClient(base_url=data['context']['gateway_endpoint'],headers={'Authorization':'Bearer '+internal_token()}) as gateway:
                    selected=await gateway.post(f'/agents/{aid}/input',json={'takeover':connection,'kind':'click','challenge_version':version,
                        'x':bounds['x']+bounds['width']/2,'y':bounds['y']+bounds['height']/2})
                    assert selected.status_code==200,selected.text
        else:
            cdp=await actor.context.new_cdp_session(page)
            try:
                document=(await cdp.send('DOM.getDocument'))['root']['nodeId']
                selected=await cdp.send('DOM.querySelector',{'nodeId':document,'selector':'button[type=button]'})
                node=(await cdp.send('DOM.describeNode',{'nodeId':selected['nodeId']}))['node']
                authorized=await controller.authorize({'name':'click','values':{},'node':{
                    'target_id':controller.lease['target_id'],'backend_node_id':node['backendNodeId']}})
                assert authorized.get('allowed'),authorized
                async with actor.context.expect_page() as opened:
                    await page.get_by_role('button',name='Use identity sign-in').click()
            finally:
                await cdp.detach()
        popup=await opened.value
        await popup.wait_for_url(site.origin('identity')+'/authorize?*')
        assert popup is await controller.owner_page()
        assert controller.lease['auth_flow']['popup_target'] not in (await actor.native_runtime.scope_view(controller.lease['target_id']))['targets']
        assert data['source_count']('POST','/authorize')==0
        if not owner_entry:
            await controller.handoff('请在原身份站点窗口完成登录；本站保存的密码不会发送给该身份站点。')
            waiting=await execute_account(aid,task,AccountInput.model_validate(task['input']['spec']['payload']))
            assert waiting.outcome=='waiting' and waiting.wait.responsibility=='owner'
            finish(claim,waiting)
        with tx(aid) as c:
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(task['id'],))['state']=='waiting_user'
        async with httpx.AsyncClient(base_url=data['context']['gateway_endpoint'],headers={'Authorization':'Bearer '+internal_token()},timeout=20) as gateway, \
            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://127.0.0.1:8088',cookies={'lead_session':token('owner')},
                headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'},timeout=20) as owner:
            async def current():
                result=await owner.get(f'/api/agents/{aid}/connections/{connection}')
                assert result.status_code==200,result.text
                return result.json()
            async def send(kind,**values):
                status=await current()
                result=await gateway.post(f'/agents/{aid}/input',json={'takeover':connection,'challenge_version':status['challenge_version'],'kind':kind,**values})
                assert result.status_code==200,result.text
            for field,value in (('username',site.username),('password',site.password)):
                bounds=await popup.locator('input[name='+field+']').bounding_box()
                await send('click',x=bounds['x']+bounds['width']/2,y=bounds['y']+bounds['height']/2)
                await send('text',text=value)
            await send('key',text='Enter')
            try:
                await popup.wait_for_url(site.origin()+'/session/complete?*',timeout=5000)
            except Exception:
                flow=controller.lease['auth_flow']
                print('AUTH_FLOW_DIAGNOSTICS',{'popup_path':urlsplit(popup.url).path,'hops':flow['hops'],
                    'paths':[urlsplit(url).path for url in flow['urls']], 'callback':flow['callback'],
                    'owner_proof_present':bool(flow.get('owner_form')),'owner_proof_used':flow.get('owner_form',{}).get('used'),
                    'source_idp_posts':data['source_count']('POST','/authorize'),
                    'source_callbacks':data['source_count']('GET','/session/complete'),
                    'decisions':[{'path':urlsplit(row['url']).path,'method':row['method'],'allowed':row['allowed'],'error':row['gate_error']}
                        for row in data['decisions']]})
                raise
            assert data['source_count']('POST','/authorize')==1 and site.oracle()['counts']['sso_completions']==1
            for callback in (popup.url,popup.url+'&unrelated=changed'):
                denied=await page.evaluate('(url)=>fetch(url).then(()=>false).catch(()=>true)',callback)
                assert denied and data['source_count']('GET','/session/complete')==1
            await asyncio.sleep(2.1)
            status=await current()
            stale=await owner.post(f'/api/agents/{aid}/connections/{connection}/control',json={'action':'resume','challenge_version':status['challenge_version']-1})
            assert stale.status_code==409
            resumed=await owner.post(f'/api/agents/{aid}/connections/{connection}/control',json={'action':'resume','challenge_version':status['challenge_version']})
            assert resumed.status_code==200,resumed.text
        ref={'agent_id':aid,'task_id':str(task['id']),'task_generation':task['input']['task_generation']}
        admitted=advance(ref)
        assert admitted['state']=='ready',admitted
        await controller.start()
        assert controller.page is page and popup.is_closed() and controller.lease['authentication_dispatched']
        await controller.inspect_self({'self_link_selector':'a[rel~=me]','acting_subject_selector':'main > p:first-of-type',
            'self_subject_prefix':'Signed in as ','self_subject_suffix':''})
        observed=await controller.report_identity({'subject_selector':'main > p'})
        assert observed['status']=='authenticated' and observed['operating_subject']['external_id']==site.username
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(task['id'],))
        result=await execute_account(aid,task,AccountInput.model_validate(task['input']['spec']['payload']))
        finish(admitted['claim'],result)
        assert result.outcome=='completed'
        with tx(aid) as c:
            account=one(c,'SELECT state,external_account_id FROM lead.social_accounts WHERE id=%s',(pending['account_id'],))
            assert account=={'state':'connected','external_account_id':site.username}
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(task['id'],))['state']=='succeeded'
        assert len(data['context']['model_calls'])==initial_calls
        assert not site.oracle()['posts']
        callback=next(url for url in controller.lease['auth_flow']['urls'] if urlsplit(url).path=='/session/complete')
        before_callbacks=data['source_count']('GET','/session/complete')
        worker=next(worker for worker in actor.context.service_workers if worker.url==site.origin()+'/sw.js')
        await worker.evaluate('(url)=>fetch(url).then(response=>response.status).catch(error=>error.name)',callback)
        assert data['source_count']('GET','/session/complete')==before_callbacks==1
        return {'original_connection_completed':True,'owner_idp_post':1,'extra_model_requests':0}

    await boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch,boundary_hook=check)
