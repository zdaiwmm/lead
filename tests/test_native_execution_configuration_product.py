"""Actual A-only SDK batches retain each original action fence and request budget."""
import json
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.parametrize('mode',['batch_login','batch_navigation','batch_owner','batch_dom_change'])
@pytest.mark.asyncio
async def test_actual_sdk_two_action_batch_and_boundaries(monkeypatch,mode):
    from lead_app import account_profiles, browser
    from lead_app.account_connection import discover
    from lead_app.config import STATE
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from lead_app.native_connection import NativeConnection
    from native_connection_product import verify_login

    original_start=NativeConnection.start
    original_authorize=NativeConnection.authorize
    original_command=NativeConnection.command
    actions=[]
    owner_changed=False
    async def start(self):
        result=await original_start(self)
        if mode=='batch_dom_change':
            await self.page.evaluate('''()=>document.querySelector('input[autocomplete=username]').addEventListener('input',()=>{
                const old=document.querySelector('input[type=password]');old.replaceWith(old.cloneNode());
            },{once:true})''')
        return result
    async def authorize(self,args):
        result=await original_authorize(self,args)
        node=args.get('node') or {}
        actions.append({'name':args['name'],'role':'password' if node.get('attributes',{}).get('type')=='password' else 'other',
            'allowed':result.get('allowed') is True})
        return result
    async def command(self,operation,args):
        nonlocal owner_changed
        if mode=='batch_owner' and not owner_changed and operation=='scope' and actions:
            filled=await self.page.locator('input[autocomplete=username]').evaluate('node=>!!node.value')
            if filled:
                owner_changed=True
                await self.handoff('Controlled owner boundary changed between actions.')
        return await original_command(self,operation,args)
    monkeypatch.setattr(NativeConnection,'start',start)
    monkeypatch.setattr(NativeConnection,'authorize',authorize)
    monkeypatch.setattr(NativeConnection,'command',command)
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)

    async def after_login(context):
        if mode!='batch_login' or context['result'].outcome!='completed':
            return None
        aid=context['aid']
        actor=await browser.gateway.get(aid,context['task']['input']['spec']['payload']['profile_id'])
        await actor.connection_pages[str(context['pending']['takeover_id'])].reconcile()
        with tx(aid) as c:
            ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),dedupe_key='batch-context'))
        admitted=advance(ref)
        assert admitted['state']=='ready'
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='discovery',step=0)
        calls=len(context['model_calls'])
        result=await discover(aid,task)
        finish(admitted['claim'],result)
        return {'calls':len(context['model_calls'])-calls}

    result=await verify_login(monkeypatch,mode=mode,owner_hook=after_login)
    folder=next((STATE/'browser-use-runs').glob('connection-*'))
    trace=json.loads((folder/'safe-step-trace.json').read_text())
    posts=len([r for r in result['oracle']['requests'] if r['method']=='POST'])
    safe={'mode':mode,'actions':actions,'trace':trace,'usage':result['usage'],'posts':posts,
        'owner_changed':owner_changed,'account_state':result['account']['state'],
        'privacy':result['privacy'],'sdk_contracts':[r['sdk_contract'] for r in result['synthetic_model_diagnostics']],
        'discovery_context_seen':[r['discovery_context_seen'] for r in result['synthetic_model_diagnostics']]}
    (STATE/('execution-config-'+mode+'.json')).write_text(json.dumps(safe,indent=2))
    assert all(r['thinking_field'] and r['planning_fields'] and r['normal_required'] and r['provider_thinking_disabled']
        and r['output_limit']==4000 for r in safe['sdk_contracts'])
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    if mode=='batch_login':
        assert result['account']['state']=='connected' and result['usage']['request_count']==4
        assert len(trace['steps'][0]['actions'])==2 and trace['steps'][0]['outcomes']==[{'status':'ok'},{'status':'ok'}]
        assert posts==1 and result['owner_check']['calls']==2
        assert any(r['operation_enum'] for r in safe['sdk_contracts'])
        assert safe['discovery_context_seen'][-2:]==[True,True]
    else:
        assert posts==0 and result['account']['state']!='connected'
        assert not any(r['name']=='input' and r['role']=='password' and r['allowed'] for r in actions)
        if mode=='batch_owner':
            assert owner_changed
