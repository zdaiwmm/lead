"""Real original native action through manager, SDK, proxy and scoped SQL."""
import os
from datetime import timedelta

import pytest

pytestmark=pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_native_first_publication_uses_actual_dom_receipt(monkeypatch):
    from lead_app import account_profiles
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def publish(context):
        aid=context['aid']
        body={'body':'Controlled first native publication','title':'','format':'text','media':[]}
        with tx(aid) as c:
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
            target=context['discovery'].output['targets'][0]['target_id']
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=context['pending']['account_id'],target_id=target,
                payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        claim=advance(ref)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='publish')
        result=await execute_channel(aid,task,data,claim)
        finish(claim,result)
        with tx(aid) as c:
            action=one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(task['id'],))
            attempt=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
            print('ACTION_DIAGNOSTICS',{'state':action['state'],'transport':attempt['observation'].get('browser_transport'),
                'claimed':attempt['observation'].get('browser_request',{}).get('claimed'),
                'fixture_posts':len(context['board'].oracle()['posts']),'fake_requests':context['workflow']['step']})
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(task['id'],))['state']=='succeeded'
            assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
        assert result.outcome=='completed' and result.output['first_format_verified']
        assert action['state']=='confirmed' and attempt['outcome']=='confirmed'
        assert attempt['observation']['browser_receipt']['body']==body['body']
        assert len(context['board'].oracle()['posts'])==1
        assert len([row for row in context['board'].oracle()['requests'] if row['method']=='POST' and row['path']=='/posts'])==1
        return {'state':'confirmed','action_id':str(action['id']),'requests':context['workflow']['step']}
    result=await verify_login(monkeypatch,after_discovery=publish)
    assert result['action_check']['state']=='confirmed' and result['action_check']['requests']==3


@pytest.mark.asyncio
async def test_native_reply_uses_independent_method_and_original_parent_receipt(monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_connection import discover
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def reply(context):
        aid=context['aid']
        board=context['board']
        parent=board.origin+'/posts/1'
        board.state.posts.append({'id':1,'author':'another-member','body':'Existing member question','parent_url':None})
        with tx(aid) as c:
            target_id=context['discovery'].output['targets'][0]['target_id']
            original=one(c,'SELECT verified_permissions FROM lead.account_targets WHERE id=%s',(target_id,))['verified_permissions']['browser_methods']['publish']
            work=submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=context['pending']['account_id'],
                payload={'target_urls':[parent]},review_at=utcnow()+timedelta(hours=1)))
        claim=advance(work)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(work['task_id'],))
        context['workflow'].update(kind='discovery',step=0,operation='reply',editor_url=parent)
        learned=await discover(aid,task)
        finish(claim,learned)
        assert learned.outcome=='completed' and len(learned.output['targets'])==1
        with tx(aid) as c:
            methods=one(c,'SELECT verified_permissions FROM lead.account_targets WHERE id=%s',(target_id,))['verified_permissions']['browser_methods']
            assert methods['publish']==original and methods['reply']['operations']==['reply']
            assert methods['reply']['request']['url']==board.origin+'/replies'
            body={'body':'Controlled reply to the original question','title':'','format':'text','media':[]}
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'],operation='reply',parent_url=parent)
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='reply',account_id=context['pending']['account_id'],target_id=target_id,
                payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        claim=advance(ref)['claim']
        with tx(aid) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='reply')
        result=await execute_channel(aid,task,data,claim)
        finish(claim,result)
        with tx(aid) as c:
            action=one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(task['id'],))
            attempt=one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
            assert action['state']=='confirmed' and attempt['outcome']=='confirmed'
            assert one(c,'SELECT lead.growth_receipt_valid(%s) ok',(action['id'],))['ok']
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(task['id'],))['state']=='succeeded'
        receipt=attempt['observation']['browser_receipt']
        assert receipt['author']==board.state.username and receipt['body']==body['body'] and receipt['parent_url']==parent
        assert result.outcome=='completed' and result.output['first_format_verified']
        assert len(board.oracle()['posts'])==2 and board.oracle()['posts'][-1]['parent_url']==parent
        assert len([row for row in board.oracle()['requests'] if row['method']=='POST' and row['path']=='/replies'])==1
        return {'state':'confirmed','requests':context['workflow']['step']}
    result=await verify_login(monkeypatch,after_discovery=reply)
    assert result['action_check']=={'state':'confirmed','requests':3}


@pytest.mark.asyncio
async def test_unknown_native_action_reads_original_receipt_without_resubmission_after_pause(monkeypatch):
    from lead_app import account_profiles, domain
    from lead_app.account_intents import set_intent
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from native_connection_product import verify_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    async def reconcile(context):
        aid=context['aid']
        board=context['board']
        body={'body':'Controlled identical old and new publication','title':'','format':'text','media':[]}
        board.state.posts.append({'id':1,'author':board.state.username,'body':body['body'],'parent_url':None})
        board.state.drop_next_post_response=True
        with tx(aid) as c:
            target_id=context['discovery'].output['targets'][0]['target_id']
            content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
            data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
            ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=context['pending']['account_id'],target_id=target_id,
                payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
        async def execute(*,duplicate=False):
            admitted=advance(ref,now=utcnow()+timedelta(minutes=6))
            assert admitted['state']=='ready',admitted
            with tx(aid) as c:
                task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            if duplicate:
                import asyncio
                results=await asyncio.gather(*(execute_channel(aid,task,data,admitted['claim']) for _ in range(2)))
                assert all(item.outcome=='unknown' for item in results)
                result=results[0]
            else:
                result=await execute_channel(aid,task,data,admitted['claim'])
            finish(admitted['claim'],result)
            with tx(aid) as c:
                action=one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(ref['task_id'],))
                attempts=c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],)).fetchall()
            return result,action,attempts
        context['workflow'].update(kind='action',step=0,body=body['body'],operation='publish')
        start=len(context['model_calls'])
        first,original,attempts=await execute(duplicate=True)
        assert len(context['model_calls'])-start==4 and not context['actor'].action_operations
        assert first.outcome=='unknown' and original['state']=='unknown' and len(attempts)==1
        assert attempts[0]['observation']['browser_request']['claimed']
        assert board.origin+'/posts/1' in attempts[0]['observation']['browser_baseline']['urls']
        assert len(board.oracle()['posts'])==2
        set_intent(aid,context['pending']['account_id'],'paused',source='owner_command',source_ref='synthetic-pause')
        domain.set_control(aid,'pause')
        with tx(aid) as c:
            frozen_intent=one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(context['pending']['account_id'],))
            frozen_grants=c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()
        context['workflow'].update(kind='reconcile',step=0,choose_old=True)
        old,action,attempts=await execute()
        assert old.outcome=='unknown' and action['id']==original['id'] and len(attempts)==1
        assert action['state']=='unknown' and not attempts[0]['observation'].get('browser_receipt')
        context['workflow'].update(kind='reconcile',step=0,choose_old=False)
        current,action,attempts=await execute()
        assert current.outcome=='completed' and action['state']=='confirmed'
        assert action['id']==original['id'] and len(attempts)==1 and attempts[0]['attempt']==1
        assert action['intent']==original['intent'] and attempts[0]['outcome']=='confirmed'
        assert attempts[0]['observation']['browser_receipt']['url']==board.origin+'/posts/2'
        assert len(board.oracle()['posts'])==2
        assert len([row for row in board.oracle()['requests'] if row['method']=='POST' and row['path']=='/posts'])==1
        with tx(aid) as c:
            assert one(c,'SELECT state FROM lead.tasks WHERE id=%s',(ref['task_id'],))['state']=='succeeded'
            assert one(c,'SELECT desired_state FROM lead.agents WHERE id=%s',(aid,))['desired_state']=='paused'
            assert one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(context['pending']['account_id'],))==frozen_intent
            assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()==frozen_grants
        return {'state':'confirmed','reconcile_requests':context['workflow']['step']}
    result=await verify_login(monkeypatch,after_discovery=reconcile)
    assert result['action_check']=={'state':'confirmed','reconcile_requests':2}
