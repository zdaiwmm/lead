"""A-only actual SDK method failure, one allowed correction, canonical storage."""
import json
import os
import shlex
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
@pytest.mark.parametrize('variant',('repair_three','selector_eight','permission_two'))
async def test_actual_sdk_discovery_selector_repair_budget(monkeypatch,variant):
    from lead_app import account_profiles, browser
    from lead_app.account_connection import discover
    from lead_app.config import STATE
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from native_connection_product import verify_login

    python=os.environ['LEAD_BROWSER_RUNTIME_PYTHON']
    bootstrap=STATE/'operation-feedback-bootstrap.py'
    interpreter=STATE/'operation-feedback-python'
    def transport(loopback):
        bootstrap.write_text('''import runpy,sys,httpx
real=httpx.AsyncHTTPTransport
class LocalOnly(httpx.AsyncBaseTransport):
 async def handle_async_request(self,request):
  if request.url.scheme=='http' and request.url.host=='127.0.0.1':
   async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
    return await client.send(request)
  assert request.url.scheme=='https' and request.url.host=='api.deepseek.com'
  async with httpx.AsyncClient(transport=real(trust_env=False),trust_env=False,timeout=45) as client:
   response=await client.post(LOOPBACK,content=request.content,headers={'Content-Type':'application/json'})
   return httpx.Response(response.status_code,headers=response.headers,content=response.content,request=request)
httpx.AsyncHTTPTransport=lambda **kwargs:LocalOnly()
path=next(value for value in sys.argv[1:] if value not in ('-B','-S'))
runpy.run_path(path,run_name='__main__')
'''.replace('LOOPBACK',repr(loopback)))
        bootstrap.chmod(0o600)
        interpreter.write_text('#!/bin/sh\nexec '+shlex.quote(python)+' -B -S '+shlex.quote(str(bootstrap))+' "$@"\n')
        interpreter.chmod(0o700)
        monkeypatch.setenv('LEAD_BROWSER_RUNTIME_PYTHON',str(interpreter))
        return {'provider':'deepseek','key':'synthetic-transport-only','model':'deepseek-flash','base_url':'https://api.deepseek.com'}

    async def after_login(context):
        aid=context['aid']
        actor=await browser.gateway.get(aid,context['task']['input']['spec']['payload']['profile_id'])
        await actor.connection_pages[str(context['pending']['takeover_id'])].reconcile()
        with tx(aid) as c:
            ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),
                dedupe_key='method-safe-feedback-'+variant))
        admitted=advance(ref)
        assert admitted['state']=='ready'
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        before=len(context['model_calls'])
        context['workflow'].update(kind='discovery',step=0,selector_errors=3 if variant=='repair_three' else 8 if variant=='selector_eight' else 0,permission_errors=variant=='permission_two',omit_identity_hints=True)
        result=await discover(aid,task)
        finish(admitted['claim'],result)
        with tx(aid) as c:
            row=one(c,'SELECT state,result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            targets=c.execute('SELECT verified_permissions FROM lead.account_targets WHERE account_id=%s',
                (context['pending']['account_id'],)).fetchall()
        folder=next((STATE/'browser-use-runs').glob('task-'+str(ref['task_id'])+'-*'))
        worker=json.loads((folder/'result.json').read_text())
        trace=json.loads((folder/'safe-step-trace.json').read_text())
        secret_seen=any(secret.encode() in path.read_bytes() for path in folder.rglob('*') if path.is_file()
            for secret in (context['board'].state.username,context['board'].state.password))
        safe={'requests':len(context['model_calls'])-before,'trace':trace,'worker_success':worker['success'],
            'usage':worker['usage'],'state':row['state'],'corrections':row['result']['runtime']['native_progress']['corrections'],
            'target_count':len(targets),'method_stored':any('publish' in t['verified_permissions'].get('browser_methods',{}) for t in targets),
            'literal_in_method_workdir':secret_seen,'feedback_seen':[r.get('method_feedback_code_seen',False) for r in context['model_calls'][before:]],
            'feedback_fields_seen':[r.get('method_feedback_fields_seen',[]) for r in context['model_calls'][before:]],
            'selector_schemas':[r['method_selector_schemas'] for r in context['model_calls'][before:]],
            'parameter_schemas':[r['method_parameter_schemas'] for r in context['model_calls'][before:]],
            'progress':row['result']['runtime']['native_progress'],
            'images':[r['image_parts'] for r in context['model_calls'][before:]],
            'sdk_contracts':[r['sdk_contract'] for r in context['model_calls'][before:]],
            'current_subject_observations':[r['current_subject_observation'] for r in context['model_calls'][before:]],
            'submit_observations':[r['submit_observation'] for r in context['model_calls'][before:]],
            'provider':'deepseek','model':'deepseek-flash'}
        (STATE/('discovery-selector-budget-'+variant+'.json')).write_text(json.dumps(safe,indent=2))
        expected_requests=4 if variant=='repair_three' else 8 if variant=='selector_eight' else 2
        assert safe['requests']==expected_requests
        assert safe['images']==[1]*expected_requests and safe['usage']['vision_images_sent']==expected_requests
        assert all(contract['thinking_field'] and contract['planning_fields'] and contract['normal_required']
            and contract['provider_thinking_disabled'] and contract['output_limit']==4000 for contract in safe['sdk_contracts'])
        assert not secret_seen
        if variant=='repair_three':
            assert safe['feedback_seen']==[False,True,True,True] and safe['corrections']==3
            assert safe['method_stored'] and safe['worker_success'] and safe['state']=='succeeded'
            assert safe['target_count']==1
            assert safe['feedback_fields_seen']==[[],['submit'],['submit'],['submit']]
        else:
            assert not safe['method_stored'] and not safe['worker_success'] and safe['target_count']==0
            assert safe['corrections']==expected_requests
            if variant=='permission_two':
                assert safe['progress']['stopped_reason']=='one_correction_exhausted'
        selector_steps=3 if variant=='repair_three' else 8 if variant=='selector_eight' else 0
        for step in trace['steps'][:selector_steps]:
            assert step['outcomes']==[{'status':'error','code':'browser_method_element_ambiguous',
                'selector_issues':[{'field':'submit','issue':'not_found'}]}]
        expected={frozenset(('editor_target','editor_body','submit')),
            frozenset(('editor_target','editor_body','submit','editor_parent'))}
        assert all({frozenset(row['required']) for row in shapes}==expected
            and all(row['extra_forbidden'] and row['required']==row['properties'] for row in shapes) for shapes in safe['selector_schemas'])
        arguments={'target_url','operation','selectors','permission_selector','permission_quote'}
        assert all(shapes and all(set(row['properties'])==arguments and set(row['required'])==arguments
            and row['extra_forbidden'] for row in shapes) for shapes in safe['parameter_schemas'])
        return safe

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await verify_login(monkeypatch,mode='method_feedback',owner_hook=after_login,model_transport=transport)
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    assert not any(row['username_literal_seen'] or row['password_literal_seen'] for row in result['synthetic_model_diagnostics'])
    assert not result['oracle']['posts']
