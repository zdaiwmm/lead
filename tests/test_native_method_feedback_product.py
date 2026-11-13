"""A-only actual SDK method failure, one allowed correction, canonical storage."""
import json
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_actual_sdk_method_feedback_allows_one_bounded_correction(monkeypatch):
    from lead_app import account_profiles, browser
    from lead_app.account_connection import discover
    from lead_app.config import STATE
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from native_connection_product import verify_login

    async def after_login(context):
        aid=context['aid']
        actor=await browser.gateway.get(aid,context['task']['input']['spec']['payload']['profile_id'])
        await actor.connection_pages[str(context['pending']['takeover_id'])].reconcile()
        with tx(aid) as c:
            ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),
                dedupe_key='method-safe-feedback'))
        admitted=advance(ref)
        assert admitted['state']=='ready'
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        before=len(context['model_calls'])
        context['workflow'].update(kind='discovery',step=0)
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
            'literal_in_method_workdir':secret_seen,'feedback_seen':[r.get('method_feedback_code_seen',False) for r in context['model_calls'][before:]]}
        (STATE/'method-feedback-product.json').write_text(json.dumps(safe,indent=2))
        assert safe['feedback_seen']==[False,True]
        assert safe['requests']==2 and safe['corrections']==1
        assert trace['steps'][0]['outcomes']==[{'status':'error','code':'browser_method_element_ambiguous'}]
        assert safe['method_stored'] and safe['worker_success'] and safe['state']=='succeeded'
        assert safe['target_count']==1 and not secret_seen
        return safe

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    result=await verify_login(monkeypatch,mode='method_feedback',owner_hook=after_login)
    assert result['privacy']=={'literal_in_model_request':False,'literal_in_workdir':False}
    assert not any(row['username_literal_seen'] or row['password_literal_seen'] for row in result['synthetic_model_diagnostics'])
    assert not result['oracle']['posts']
