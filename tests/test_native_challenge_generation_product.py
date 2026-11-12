"""Real ConnectionRef generation and owner input; synthetic visible QR/passkey UI."""
import os

import httpx
import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['qr','passkey','device'])
async def test_actual_person_surface_changes_reject_old_owner_input_and_resume(monkeypatch,kind):
    from lead_app import account_profiles, api, browser
    from lead_app.db import one, tx
    from lead_app.security import internal_token, token
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def check(context):
        aid=context['aid']
        pending=context['pending']
        connection=str(pending['takeover_id'])
        actor=await browser.gateway.get(aid,context['task']['input']['spec']['payload']['profile_id'],allow_challenge=True)
        controller=actor.connection_pages[connection]
        page=controller.page
        def state():
            with tx(aid) as c:
                return one(c,'SELECT * FROM lead.takeover_sessions WHERE id=%s',(connection,))
        original=state()['challenge_version']
        calls=len(context['model_calls'])
        heading={'qr':'Scan QR code to sign in','passkey':'Use your passkey','device':'Check your phone'}[kind]
        await page.set_content('<main><h1>'+heading+'</h1><section><input type=hidden name=challenge_id value=first>'
            '<canvas aria-label="Sign-in QR code" width=80 height=80></canvas><label>Owner note<input name=note></label>'
            '<button id=refresh>Refresh challenge</button><span role=timer>30 seconds</span></section></main>')
        await page.locator('#refresh').evaluate("node=>node.onclick=()=>{document.querySelector('[name=challenge_id]').value='second';const canvas=document.querySelector('canvas');canvas.getContext('2d').fillRect(0,0,10,10)}")
        async with httpx.AsyncClient(base_url=context['gateway_endpoint'],headers={'Authorization':'Bearer '+internal_token()}) as gateway, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://127.0.0.1:8088',
                    cookies={'lead_session':token('owner')},headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'}) as owner:
            async def input(kind,version,**values):
                return await gateway.post(f'/agents/{aid}/input',json={'takeover':connection,'challenge_version':version,'kind':kind,**values})
            observed=await owner.get(f'/api/agents/{aid}/connections/{connection}')
            assert observed.status_code==200
            current=observed.json()['challenge_version']
            assert current>original and state()['connection_data']['owner_challenge']['kind']==kind
            assert (await input('text',original,text='late original OTP')).status_code==409
            box=await page.locator('[name=note]').bounding_box()
            assert (await input('click',current,x=box['x']+3,y=box['y']+3)).status_code==200
            assert (await input('text',current,text='ordinary owner typing')).status_code==200
            await page.locator('[role=timer]').evaluate("node=>node.textContent='29 seconds'")
            assert (await owner.get(f'/api/agents/{aid}/connections/{connection}')).json()['challenge_version']==current
            box=await page.locator('#refresh').bounding_box()
            assert (await input('click',current,x=box['x']+3,y=box['y']+3)).status_code==200
            changed=state()['challenge_version']
            assert changed>current
            assert (await input('text',current,text='late old challenge')).status_code==409
            assert await page.locator('[name=note]').input_value()=='ordinary owner typing'
            stale=await owner.post(f'/api/agents/{aid}/connections/{connection}/control',json={'action':'resume','challenge_version':current})
            assert stale.status_code==409
            assert (await input('reload',changed)).status_code==200
            reloaded=state()['challenge_version']
            assert reloaded>changed and state()['connection_data']['owner_challenge']['kind']=='otp'
            assert (await input('text',changed,text='late before reload')).status_code==409
            assert len(context['model_calls'])==calls
            cancelled=await owner.post(f'/api/agents/{aid}/connections/{connection}/control',json={'action':'cancel','challenge_version':reloaded})
            assert cancelled.status_code==200 and state()['encrypted_login'] is None
        return {'kind':kind,'generation_fenced':True,'new_model_requests':0}
    result=await verify_login(monkeypatch,mode='owner_otp',owner_hook=check)
    assert result['owner_check']=={'kind':kind,'generation_fenced':True,'new_model_requests':0}
