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
        for mode,expected,reason in [('no_progress',3,'no_progress'),('correction',2,'one_correction_exhausted')]:
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
            assert result.output['targets']==[] and result.output['missing_conditions']
            with tx(aid) as c:
                progress=one(c,'SELECT result FROM lead.tasks WHERE id=%s',(ref['task_id'],))['result']['runtime']['native_progress']
                assert progress['stopped_reason']==reason
                assert c.execute('SELECT * FROM lead.account_targets ORDER BY id').fetchall()==targets
                assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()==grants
            assert progress['no_progress']==3 if mode=='no_progress' else progress['corrections']==2
            assert len(progress['state_hash'])==64
            checks[mode]=expected
        return checks
    result=await verify_login(monkeypatch,after_discovery=bounded)
    assert result['action_check']=={'no_progress':3,'correction':2}


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
        for mode,expected in [('clock',3),('reload',3),('scroll_cycle',4)]:
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
            assert progress['stopped_reason']=='no_progress' and progress['no_progress']==3
            counts[mode]=len(context['model_calls'])-start
            assert counts[mode]==expected
            assert len(progress['seen_states'])<=2
        return counts
    result=await verify_login(monkeypatch,after_discovery=bounded)
    assert result['action_check']=={'clock':3,'reload':3,'scroll_cycle':4}
