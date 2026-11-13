"""Original unknown actions get bounded active checks, never a new submission."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from lead_app import activities, budget, domain
from lead_app.browser_actions import claim_request, prepare_binding
from lead_app.db import j, one, tx, uid
from lead_app.growth import runtime
from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from lead_app.growth.store import submit_work
from lead_app.growth.tools import ChannelInput, prepare_channel
from test_growth_channel_operation import channel_work


@pytest.fixture
def clock(monkeypatch):
    clock=SimpleNamespace(now=utcnow())
    monkeypatch.setattr(runtime,'utcnow',lambda:clock.now)
    return clock


def task(aid,ref):
    with tx(aid) as c:
        return one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))


def checks(row):
    return row['result']['runtime'].get('receipt_reconciliation',{}).get('completed_checks',0)


def due(clock,row):
    clock.now=datetime.fromisoformat(row['result']['runtime']['wait']['check_at'])


def unknown_action(aid):
    ref,data,account=channel_work(aid)
    claim=runtime.advance(ref)['claim']
    action=prepare_channel(aid,ref['task_id'],data)
    with tx(aid) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action['id'],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action['id'],))
    assert runtime.finish(claim,WorkResult(outcome='unknown',output={
        'receipt_reconciliation':{'completed_checks':99}}))['accepted_control']
    return ref,data,account,action,claim


@pytest.mark.asyncio
async def test_three_completed_receipt_checks_then_hourly_keep_original_ledger(growth_agent,clock,monkeypatch):
    aid=growth_agent
    ref,_,_,action,_=unknown_action(aid)
    original=task(aid,ref)
    reservation=budget.reserve(aid,'synthetic-receipt-cadence-ledger',400)
    budget.settle(aid,reservation,actual=37,details={'source':'synthetic-local-ledger'})
    with tx(aid) as c:
        ledger=c.execute('SELECT id,state,units FROM lead.budget_reservations ORDER BY id').fetchall()
        usage=c.execute('SELECT id,reservation_id,units FROM lead.usage_events ORDER BY id').fetchall()
        task_count=one(c,'SELECT count(*) n FROM lead.tasks')['n']
    calls=[]
    async def read_original(path,body):
        assert path==f'/agents/{aid}/publish' and body['action_id']==str(action['id'])
        calls.append(body['action_id'])
        return {'state':'unknown'}
    monkeypatch.setattr(activities,'browser_rpc',read_original)
    assert checks(original)==0
    for completed in range(1,4):
        due(clock,task(aid,ref))
        admitted=runtime.advance(ref)
        assert admitted['state']=='ready' and admitted['claim']['operation_ref']==str(action['id'])
        result=await runtime.execute(admitted['claim'])
        assert runtime.finish(admitted['claim'],result)['accepted_control']
        current=task(aid,ref)
        delay=datetime.fromisoformat(current['result']['runtime']['wait']['check_at'])-clock.now
        expected_delay=timedelta(seconds=300) if completed<3 else timedelta(hours=1)
        assert delay==expected_delay
    assert len(calls)==3
    current=task(aid,ref)
    assert checks(current)==3
    wait=current['result']['runtime']['wait']
    assert wait['scope_ref']==str(action['id']) and wait['responsibility']=='platform'
    assert current['result']['runtime']['receipt_reconciliation']['action_id']==str(action['id'])
    clock.now=datetime.fromisoformat(wait['check_at'])-timedelta(seconds=1)
    assert runtime.advance(ref)['state']=='wait' and len(calls)==3
    clock.now+=timedelta(seconds=1)
    fourth=runtime.advance(ref)
    assert fourth['state']=='ready'
    assert runtime.finish(fourth['claim'],await runtime.execute(fourth['claim']))['accepted_control']
    current=task(aid,ref)
    assert checks(current)==4
    assert datetime.fromisoformat(current['result']['runtime']['wait']['check_at'])-clock.now==timedelta(hours=1)
    assert current['input']==original['input'] and current['result']['runtime']['attempts']==5
    with tx(aid) as c:
        assert c.execute('SELECT id,state,units FROM lead.budget_reservations ORDER BY id').fetchall()==ledger
        assert c.execute('SELECT id,reservation_id,units FROM lead.usage_events ORDER BY id').fetchall()==usage
        assert one(c,'SELECT count(*) n FROM lead.tasks')['n']==task_count
        actions=c.execute('SELECT id,state,intent FROM lead.actions').fetchall()
    assert len(actions)==1 and actions[0]['id']==action['id'] and actions[0]['state']=='unknown'
    assert actions[0]['intent']==action['intent']


def test_expired_and_duplicate_claims_do_not_count_as_completed_checks(growth_agent,clock):
    aid=growth_agent
    ref,_,_,_,initial=unknown_action(aid)
    due(clock,task(aid,ref))
    expired=runtime.advance(ref)['claim']
    clock.now=datetime.fromisoformat(expired['lease_until'])+timedelta(seconds=1)
    runtime.recover_due(aid,now=clock.now)
    assert not runtime.finish(expired,WorkResult(outcome='unknown'))['accepted_control']
    assert not runtime.finish(initial,WorkResult(outcome='unknown'))['accepted_control']
    assert checks(task(aid,ref))==0
    current=runtime.advance(ref)['claim']
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda context:context.run(runtime.finish,current,WorkResult(outcome='unknown')),
                              [copy_context(),copy_context()]))
    assert sorted(result['accepted_control'] for result in results)==[False,True]
    assert checks(task(aid,ref))==1
    assert task(aid,ref)['result']['runtime']['attempts']==3


def test_prepared_retry_becoming_unknown_is_not_a_receipt_check(growth_agent,clock):
    aid=growth_agent
    ref,data,_=channel_work(aid)
    initial=runtime.advance(ref)['claim']
    action=prepare_channel(aid,ref['task_id'],data)
    wait=Wait(reason='editor_not_ready',producer='channel_adapter',recovery_action='recheck',
        check_at=clock.now+timedelta(seconds=1),episode_root=ref['task_id'])
    assert runtime.finish(initial,WorkResult(outcome='waiting',wait=wait))['accepted_control']
    due(clock,task(aid,ref))
    sending=runtime.advance(ref)['claim']
    assert sending['operation_ref']==str(action['id'])
    with tx(aid) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action['id'],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action['id'],))
    assert runtime.finish(sending,WorkResult(outcome='unknown'))['accepted_control']
    current=task(aid,ref)
    assert current['result']['runtime']['attempts']==2 and checks(current)==0
    assert datetime.fromisoformat(current['result']['runtime']['wait']['check_at'])-clock.now==timedelta(minutes=5)


def test_pending_receipt_wait_after_three_checks_keeps_hourly_floor(growth_agent,clock):
    aid=growth_agent
    ref,_,_,action,_=unknown_action(aid)
    for _ in range(3):
        due(clock,task(aid,ref))
        claim=runtime.advance(ref)['claim']
        wait=Wait(reason='receipt_pending',producer='channel_adapter',recovery_action='reconcile',
            check_at=clock.now+timedelta(minutes=5),episode_root=ref['task_id'],scope_ref=str(action['id']),responsibility='platform')
        assert runtime.finish(claim,WorkResult(outcome='waiting',wait=wait))['accepted_control']
    current=task(aid,ref)
    assert checks(current)==3
    assert datetime.fromisoformat(current['result']['runtime']['wait']['check_at'])-clock.now==timedelta(hours=1)


def other_channel(aid,monkeypatch):
    from lead_app import account_profiles
    from lead_app.account_intents import set_intent
    from lead_app.adapters.catalog import SPECS
    from lead_app.execution_routes import register
    from lead_app.growth.store import digest
    monkeypatch.setattr(account_profiles,'native_available',lambda:False)
    profile=account_profiles.for_platform(aid,'facebook',create=True)
    sid=profile['account_id']
    with tx(aid) as c:
        c.execute("UPDATE lead.social_accounts SET state='connected',external_account_id=%s WHERE id=%s",(uid(),sid))
        target=one(c,"""INSERT INTO lead.account_targets(tenant_id,agent_id,account_id,target_type,external_target_id,
            canonical_url,display_name,verified_permissions,state) VALUES(lead.ctx_tenant(),%s,%s,'page',%s,
            'https://www.facebook.com/synthetic-independent','Synthetic other channel','{}','active') RETURNING id""",(aid,sid,uid()))
        body={'body':'An independent channel intention','title':'','format':'text','media':[]}
        content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING id""",(aid,j(body),digest(body)))
    revision=set_intent(aid,sid,'running',source='owner_command',source_ref='synthetic:independent-channel')
    register(aid,profile['id'])
    data=ChannelInput(content_id=content['id'],content_hash=digest(body))
    with tx(aid) as c:
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
            (j({'actions':['publish'],'adapter_version':SPECS['facebook'].version}),target['id']))
        c.execute("""INSERT INTO lead.authorization_grants(tenant_id,agent_id,target_id,version,allowed_actions,limits,granted_by)
            VALUES(lead.ctx_tenant(),%s,%s,1,'["publish"]',%s,'owner')""",
            (aid,target['id'],j({'formats':['text'],'intent_revision':revision,'account_id':str(sid),'binding_version':1})))
        ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=sid,target_id=target['id'],
            payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
    return ref,data


def independent_intention(aid,original):
    from lead_app.growth.store import digest
    body={'body':'A different current lab intention','title':'','format':'text','media':[]}
    with tx(aid) as c:
        content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING id""",(aid,j(body),digest(body)))
        data=ChannelInput(content_id=content['id'],content_hash=digest(body))
        spec=TaskSpec.model_validate(original['input']['spec']).model_copy(update={'payload':data.model_dump(mode='json')})
        return submit_work(c,aid,spec),data


def test_unknown_only_blocks_same_intention_and_paused_project_can_read(growth_agent,clock,monkeypatch):
    aid=growth_agent
    ref,data,account,action,_=unknown_action(aid)
    original=task(aid,ref)
    with tx(aid) as c:
        repeated=TaskSpec.model_validate(original['input']['spec']).model_copy(update={'dedupe_key':'renamed-same-intention'})
        assert submit_work(c,aid,repeated)==ref
    assert prepare_channel(aid,ref['task_id'],data)['id']==action['id']
    actor=SimpleNamespace(aid=aid,profile={'id':account['profile_id']},epoch=1)
    with pytest.raises(ValueError,match='original_action_reconcile_only'):
        prepare_binding(actor,action['id'],{},1)
    assert not claim_request(actor,action['id'],{},1,{},{})
    independent,independent_data=independent_intention(aid,original)
    assert independent!=ref and runtime.advance(independent)['state']=='ready'
    assert prepare_channel(aid,independent['task_id'],independent_data)['id']!=action['id']
    another,another_data=other_channel(aid,monkeypatch)
    assert runtime.advance(another)['state']=='ready'
    assert prepare_channel(aid,another['task_id'],another_data)['state']=='prepared'
    domain.set_control(aid,'pause')
    due(clock,task(aid,ref))
    ready=runtime.advance(ref)
    assert ready['state']=='ready' and ready['claim']['operation_ref']==str(action['id'])
    assert runtime.finish(ready['claim'],WorkResult(outcome='unknown'))['accepted_control']
    assert checks(task(aid,ref))==1
    with tx(aid) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='unknown'
        assert one(c,'SELECT count(*) n FROM lead.actions WHERE task_id=%s',(ref['task_id'],))['n']==1
