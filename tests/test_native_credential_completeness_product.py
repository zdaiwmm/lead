"""Synthetic SDK inputs expose static field roles and boolean completeness only."""
import json
import os

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.parametrize('style',['bare','tagged'])
@pytest.mark.asyncio
async def test_incomplete_login_submit_is_denied_before_native_click(monkeypatch,style):
    import native_connection_product as helper
    from lead_app import account_profiles
    from lead_app.config import STATE
    from lead_app.native_connection import NativeConnection

    samples=[]
    original=NativeConnection.authorize
    credentials={}
    original_board=helper.NativeBoard
    def board(*args,**kwargs):
        value=original_board(*args,**kwargs)
        credentials.update(username=value.state.username,password=value.state.password)
        return value
    monkeypatch.setattr(helper,'NativeBoard',board)
    async def observed(self,args):
        values=await self.page.evaluate('''expected=>{
            const form=document.querySelector('form:has(input[type=password])');
            if(!form)return null;
            const password=form.querySelector('input[type=password]');
            const username=Array.from(form.elements).find(n=>n.autocomplete==='username');
            return {username_present:!!username?.value,password_present:!!password?.value,
                username_matches_saved:username?.value===expected.username,
                password_matches_saved:password?.value===expected.password,
                valid:!Array.from(form.elements).some(n=>n.willValidate&&!n.validity.valid)};
        }''',credentials)
        decision=await original(self,args)
        if values:
            samples.append({'action':args['name'],'before':values,'allowed':decision.get('allowed') is True})
        return decision
    monkeypatch.setattr(NativeConnection,'authorize',observed)
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await helper.verify_login(monkeypatch,mode='credential_form_probe_'+style)
    folder=next((STATE/'browser-use-runs').glob('connection-*'))
    trace=json.loads((folder/'safe-step-trace.json').read_text())
    safe={'samples':samples,'trace':trace,'observations':[
        row.get('credential_controls',[]) for row in result['synthetic_model_diagnostics']],
        'privacy':result['privacy'],'request_count':result['usage']['request_count'],
        'authentication_posts':len([r for r in result['oracle']['requests'] if r['method']=='POST' and r['path']=='/session']),
        'task_state':result['task_state']}
    (STATE/('credential-completeness-'+style+'.json')).write_text(json.dumps(safe,indent=2))
    assert samples[1]['before']['username_matches_saved']
    assert samples[2]['before']=={'username_present':True,'password_present':False,
        'username_matches_saved':True,'password_matches_saved':False,'valid':False}
    assert not samples[2]['allowed'], 'An incomplete required credential form must not report click success.'
    assert trace['steps'][2]['outcomes']==[{'status':'denied','code':'credential_password_missing'}]
    assert samples[4]['before']['username_matches_saved'] and samples[4]['before']['password_matches_saved']
    assert result['task_state']=='succeeded' and safe['authentication_posts']==1
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    assert not any(row['trace_in_model_prompt'] for row in result['synthetic_model_diagnostics'])
