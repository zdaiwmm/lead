"""A real SW-cached private A page cannot certify the current server subject B."""
import json
import os
import re
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_private_identity_probe_bypasses_stale_worker_cache_without_disabling_business_worker(monkeypatch):
    import test_native_boundary_product as boundary
    from lead_app.account_connection import execute_account
    from lead_app.browser_tools import BrowserTools
    from lead_app.db import all_rows, one, tx
    from lead_app.growth import runtime, tools
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.store import submit_work
    from lead_app.growth.tools import AccountInput
    from native_browser_boundary_fixture import BoundarySite

    worker=BoundarySite._worker
    monkeypatch.setattr(BoundarySite,'_worker',staticmethod(lambda:worker()+'''
self.addEventListener('fetch',event=>{if(new URL(event.request.url).pathname==='/account')
 event.respondWith(caches.open('private-self-test').then(async cache=>{
   const old=await cache.match(event.request);if(old)return old;
   const fresh=await fetch(event.request);await cache.put(event.request,fresh.clone());return fresh;
 }));});'''))
    handler=BoundarySite._handler
    def labeled_account(site):
        current=handler(site)
        send=current.send
        def labeled(current,status,body='',**kwargs):
            body=re.sub(r'<p>Account ID: ([^<]+)</p>',r'<p>Account ID: <strong id="private-current-subject">\1</strong></p>',body)
            return send(current,status,body,**kwargs)
        current.send=labeled
        return current
    monkeypatch.setattr(BoundarySite,'_handler',labeled_account)

    async def check(data):
        actor,controller,page,site=(data[key] for key in ('actor','controller','page','site'))
        aid=actor.aid
        await controller.inspect_self({'self_link_selector':'a[rel~=me]','acting_subject_selector':'main > p:first-of-type',
            'self_subject_prefix':'Signed in as ','self_subject_suffix':''})
        proof=await controller.report_identity({'subject_selector':'#private-current-subject'})
        assert proof['status']=='authenticated'
        before=data['source_count']('GET','/account')
        cached=await page.evaluate("fetch('/account').then(response=>response.text())")
        assert site.username in cached and data['source_count']('GET','/account')==before
        result=await execute_account(aid,data['task'],AccountInput.model_validate(data['task']['input']['spec']['payload']))
        assert result.outcome=='completed'
        runtime.finish(data['claim'],result)
        with tx(aid) as c:
            account=one(c,'SELECT * FROM lead.social_accounts WHERE id=%s',(data['pending']['account_id'],))
            intent=one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(account['id'],))
        assert account['state']=='connected' and account['external_account_id']==site.username
        changed='different-current-private-subject'
        with site._lock:
            saved=dict(site._sessions)
            site._sessions={key:changed for key in saved}
        calls=[]
        models=[]
        original=BrowserTools.command
        async def observed(tool,operation,args=None):
            result=await original(tool,operation,args)
            calls.append(result)
            return result
        async def forbidden_model(*args,**kwargs):
            models.append(True)
            raise AssertionError('A stale cached private subject must be rejected before a research model')
        monkeypatch.setattr(BrowserTools,'command',observed)
        monkeypatch.setattr(tools,'run_model',forbidden_model)
        try:
            with tx(aid) as c:
                ref=submit_work(c,aid,TaskSpec(kind='research',purpose='fresh private identity after cached self',account_id=account['id'],
                    payload={'access_mode':'account','account_id':str(account['id']),'urls':[site.origin()+'/account'],
                        'question':'Read the current account surface'},review_at=utcnow()+timedelta(hours=1)))
            claim=runtime.advance(ref)['claim']
            result=await runtime.execute(claim)
            runtime.finish(claim,result)
            with tx(aid) as c:
                current=one(c,'SELECT * FROM lead.social_accounts WHERE id=%s',(account['id'],))
                task=one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))
                documents=all_rows(c,'SELECT id FROM lead.knowledge_documents WHERE source_ref=%s',(site.origin()+'/account',))
                final_intent=one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(account['id'],))
            diagnostics={'source_private_reads_before':before,'source_private_reads_after':data['source_count']('GET','/account'),
                'dto_error':calls[0].get('error') if calls else None,'model_attempts':len(models),'health':current['state'],
                'source_documents':len(documents),'stale_cache_still_contains_A':site.username in await page.evaluate("caches.open('private-self-test').then(c=>c.match('/account')).then(r=>r.text())")}
            print('PRIVATE_CACHE_DIAGNOSTICS',json.dumps(diagnostics,sort_keys=True))
            assert diagnostics['source_private_reads_after']>before and diagnostics['stale_cache_still_contains_A']
            assert current['state']=='needs_auth' and current['binding_version']==account['binding_version']
            assert current['external_account_id']==account['external_account_id'] and final_intent==intent
            assert calls[0]['error']=='ACCOUNT_SUBJECT_CHANGED' and changed not in json.dumps(calls)
            assert task['state']!='succeeded' and models==[] and documents==[]
            return diagnostics
        finally:
            with site._lock:
                site._sessions=saved

    await boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch,boundary_hook=check)
