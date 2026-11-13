"""A changed live form repairs one untouched attempt and reaches its real receipt."""
import copy
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_original_untouched_attempt_uses_one_current_form_revision(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import derive_targets
    from lead_app.browser_capabilities import verified_operations
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel, prepare_channel
    from lead_app.native_action import NativeAction
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login

    monkeypatch.setattr(account_profiles,'native_available',lambda:True)

    async def check(context):
        aid,actor,board=context['aid'],context['actor'],context['board']
        target_id=context['discovery'].output['targets'][0]['target_id']
        body={'body':'One original attempt after an observed form correction','title':'','format':'text','media':[]}
        with tx(aid) as c:
            account=one(c,'SELECT * FROM lead.social_accounts WHERE id=%s',(context['pending']['account_id'],))
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=account['id'],target_id=target_id,
                payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        claim=advance(ref)['claim']
        original=prepare_channel(aid,ref['task_id'],data)
        first=NativeAction(actor,original['id'],claim)
        await first.start()
        await first.close()
        finish(claim,WorkResult(outcome='waiting',wait=Wait(reason='method_correction',producer='channel_adapter',
            recovery_action='recheck',check_at=utcnow()+timedelta(minutes=10),episode_root=ref['task_id'],scope_ref=str(original['id']))))
        with tx(aid) as c:
            before=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(original['id'],))
        assert not before['observation']['browser_request']['claimed'] and len(board.oracle()['posts'])==0
        # Only this test site's emitted HTML changes. The fixed fixture and all
        # product producers remain unchanged; the new selector must work on
        # both the observed editor and the independent fresh editor at claim.
        handler=board.server.RequestHandlerClass
        send=handler.send
        def changed_form(handler,status,body='',**kwargs):
            return send(handler,status,body.replace('<button type="submit">Publish post</button>',
                '<button id="current-submit-v2" type="submit">Publish post</button>'),**kwargs)
        monkeypatch.setattr(handler,'send',changed_form)
        with tx(aid) as c:
            discovery=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=account['id'],
                dedupe_key='current-form-correction',payload={'target_urls':[board.origin+'/home']},review_at=utcnow()+timedelta(hours=1)))
        observed_claim=advance(discovery)['claim']
        producer=NativeTask(actor,discovery['task_id'])
        try:
            await producer.start(board.origin+'/home')
            method=original['intent']['browser_method']
            selectors=copy.deepcopy(method['selectors'])
            selectors['submit']='#current-submit-v2'
            await producer.record_method({'target_url':method['target_url'],'operation':'publish','selectors':selectors,
                'self_subject_prefix':method['self_subject_prefix'],'self_subject_suffix':method['self_subject_suffix'],
                'permission_selector':method['platform_permission']['selector'],'permission_quote':method['platform_permission']['quote']})
            derived=derive_targets(aid,account,producer.observations)
            assert derived[0]['grant_id']==str(original['grant_id'])
        finally:
            await producer.close()
        finish(observed_claim,WorkResult(outcome='completed'))
        current=advance(ref)
        assert current['state']=='ready' and current['claim']['ref']==claim['ref']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='publish')
        start=len(context['model_calls'])
        result=await execute_channel(aid,task,data,current['claim'])
        finish(current['claim'],result)
        with tx(aid) as c:
            action=one(c,'SELECT * FROM lead.actions WHERE id=%s',(original['id'],))
            attempts=c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s',(original['id'],)).fetchall()
            assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
            assert verified_operations(c,account['id'])==['publish']
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='succeeded'
        assert result.outcome=='completed' and result.output['first_format_verified']
        assert action['intent']==original['intent'] and action['state']=='confirmed' and action['grant_id']==original['grant_id']
        assert len(attempts)==1 and attempts[0]['id']==before['id'] and attempts[0]['attempt']==1
        assert attempts[0]['observation']['method_repair']['revision']==1
        assert attempts[0]['observation']['effective_method']['selectors']['submit']=='#current-submit-v2'
        assert attempts[0]['observation']['browser_receipt']['body']==body['body']
        assert len([row for row in board.oracle()['requests'] if row['method']=='POST' and row['path']=='/posts'])==1
        assert len(context['model_calls'])-start==3
        return {'state':'confirmed','same_original_attempt':True,'fake_requests':3}

    result=await verify_login(monkeypatch,after_discovery=check)
    assert result['action_check']=={'state':'confirmed','same_original_attempt':True,'fake_requests':3}
