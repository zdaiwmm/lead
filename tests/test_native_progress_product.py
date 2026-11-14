"""The official SDK loop stops on actual unchanged state and one correction."""
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_native_sdk_no_progress_and_one_directed_correction_are_bounded(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import discover
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def bounded(context):
        aid=context['aid']
        checks={}
        # This fixture proposes an unproved permission quote, so it receives
        # one correction rather than discovery's eight selector-only repairs.
        for mode,expected,reason in [('no_progress',8,None),('correction',2,'one_correction_exhausted')]:
            with tx(aid) as c:
                targets=c.execute('SELECT * FROM lead.account_targets ORDER BY id').fetchall()
                grants=c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()
                ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                    payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),dedupe_key='native-bound-'+mode))
            claim=advance(ref)['claim']
            with tx(aid) as c:
                task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            start=len(context['model_calls'])
            context['workflow'].update(kind=mode,step=0)
            result=await discover(aid,task)
            finish(claim,result)
            assert len(context['model_calls'])-start==expected
            assert not result.output.get('targets')
            if mode=='no_progress':
                assert result.outcome=='waiting' and result.checkpoint['stage']=='native_discovery'
            else:
                assert result.output['missing_conditions']
            with tx(aid) as c:
                progress=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['native_progress']
                assert progress.get('stopped_reason')==reason
                assert c.execute('SELECT * FROM lead.account_targets ORDER BY id').fetchall()==targets
                assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()==grants
            assert progress['no_progress']>=3 if mode=='no_progress' else progress['corrections']==2
            assert len(progress['state_hash'])==64
            checks[mode]=expected
        return checks
    result=await verify_login(monkeypatch,after_discovery=bounded)
    assert result['action_check']=={'no_progress':8,'correction':2}


@pytest.mark.asyncio
async def test_native_clocks_reload_and_revisited_scroll_do_not_extend_progress(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import discover
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import submit_work
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def bounded(context):
        aid=context['aid']
        original_start=NativeTask.start
        original_observe=NativeTask.observe_progress
        async def decorate(controller,*args,**kwargs):
            value=await original_start(controller,*args,**kwargs)
            if context['workflow']['kind']=='clock':
                await controller.page.evaluate("""()=>{const node=document.createElement('time');document.body.prepend(node);setInterval(()=>node.textContent=new Date().toISOString(),20)}""")
            if context['workflow']['kind']=='scroll_cycle':
                await controller.page.evaluate("""()=>{const section=document.createElement('section');for(let i=0;i<80;i++){const p=document.createElement('p');p.textContent='Controlled useful discussion section '+i;p.style.height='100px';section.append(p)}document.querySelector('main').append(section)}""")
            return value
        async def observe(controller):
            if context['workflow']['kind']=='reload':
                await controller.page.reload(wait_until='domcontentloaded')
            return await original_observe(controller)
        monkeypatch.setattr(NativeTask,'start',decorate)
        monkeypatch.setattr(NativeTask,'observe_progress',observe)
        counts={}
        for mode,expected in [('clock',8),('reload',8),('scroll_cycle',8)]:
            with tx(aid) as c:
                ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                    payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),dedupe_key='native-semantic-'+mode))
            claim=advance(ref)['claim']
            with tx(aid) as c:
                task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            start=len(context['model_calls'])
            context['workflow'].update(kind=mode,step=0)
            result=await discover(aid,task)
            finish(claim,result)
            with tx(aid) as c:
                progress=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['native_progress']
            assert result.outcome=='waiting' and result.checkpoint['stage']=='native_discovery'
            assert progress.get('stopped_reason')!='no_progress' and progress['no_progress']>=3
            counts[mode]=len(context['model_calls'])-start
            assert counts[mode]==expected
            assert len(progress['seen_states'])<=2
        return counts
    result=await verify_login(monkeypatch,after_discovery=bounded)
    assert result['action_check']=={'clock':8,'reload':8,'scroll_cycle':8}


@pytest.mark.asyncio
async def test_useful_native_fragment_releases_page_and_resumes_original_due_task(monkeypatch):
    import asyncio
    from datetime import datetime

    from lead_app import account_profiles
    from lead_app.account_connection import discover
    from lead_app.browser_runtime import PersistentRuntime
    from lead_app.db import one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish, recover_due
    from lead_app.growth.store import submit_work
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def fragmented(context):
        aid=context['aid']
        with tx(aid) as c:
            ref=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                payload={'target_urls':[context['board'].origin+'/home']},review_at=utcnow()+timedelta(hours=1),dedupe_key='native-useful-fragments'))
        original_acquire=PersistentRuntime.acquire_page
        original_start=NativeTask.start
        starts=[]
        async def long_document(runtime,scope,url,**kwargs):
            lease=await original_acquire(runtime,scope,url,**kwargs)
            if scope.get('task_id')==str(ref['task_id']) and not kwargs.get('internal_parent'):
                await runtime.pages[lease['target_id']]['page'].evaluate("""()=>{
                    const section=document.createElement('section');for(let i=0;i<120;i++){const p=document.createElement('p');p.textContent='Controlled substantive discussion paragraph '+i;p.style.height='100px';section.append(p)}document.querySelector('main').append(section)}""")
            return lease
        async def start(controller,*args,**kwargs):
            value=await original_start(controller,*args,**kwargs)
            if controller.task_id==str(ref['task_id']):
                starts.append(await controller.page.evaluate('()=>scrollY'))
            return value
        monkeypatch.setattr(PersistentRuntime,'acquire_page',long_document)
        monkeypatch.setattr(NativeTask,'start',start)
        claim=advance(ref)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='fragment',step=0)
        initial_requests=len(context['model_calls'])
        first=await discover(aid,task)
        finish(claim,first)
        assert first.outcome=='waiting' and first.wait.reason=='bounded_browser_fragment'
        assert len(context['model_calls'])-initial_requests==8
        with tx(aid) as c:
            waiting=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert waiting['state']=='planned' and waiting['result']['runtime']['attempt_token'] is None
        assert waiting['result']['runtime']['checkpoint']['scroll'][1]>0
        assert waiting['result']['runtime']['native_progress']['no_progress']==0
        assert not any(value['scope']['task_id']==str(ref['task_id']) for value in context['actor'].native_runtime.pages.values())
        assert str(ref['task_id']) not in context['actor'].task_pages
        await asyncio.sleep(max(0,(datetime.fromisoformat(waiting['result']['runtime']['wait']['check_at'])-utcnow()).total_seconds())+.05)
        with tx(aid) as c:
            previous={row['id'] for row in c.execute("SELECT id FROM lead.outbox_events WHERE event_type='growth.work'").fetchall()}
        recover_due(aid)
        with tx(aid) as c:
            emitted=c.execute("SELECT id,payload FROM lead.outbox_events WHERE event_type='growth.work' AND payload->>'task_id'=%s",(str(ref['task_id']),)).fetchall()
            assert any(row['id'] not in previous and row['payload']==ref for row in emitted)
        resumed=advance(ref)['claim']
        assert resumed['ref']==claim['ref'] and resumed['attempt_token']!=claim['attempt_token']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='discovery',step=0)
        second=await discover(aid,task)
        finish(resumed,second)
        assert second.outcome=='completed' and second.output['targets']
        with tx(aid) as c:
            final=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert final['state']=='succeeded' and final['input']['task_generation']==ref['task_generation']
        assert final['result']['runtime']['native_progress']['fragments']==2
        assert starts[0]==0 and starts[1]>0
        return {'first_requests':8,'continuation_requests':context['workflow']['step'],'same_task':True,'released':True}
    result=await verify_login(monkeypatch,after_discovery=fragmented)
    assert result['action_check']=={'first_requests':8,'continuation_requests':2,'same_task':True,'released':True}
