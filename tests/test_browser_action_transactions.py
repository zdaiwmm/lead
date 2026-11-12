"""Real original DB transactions: immutable request identity and current authority."""
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import psycopg
import pytest
from lead_app.account_intents import set_intent
from lead_app.browser_actions import claim_request, mark_untrusted, prepare_binding
from lead_app.db import j, one, tx
from lead_app.growth.runtime import advance
from lead_app.growth.tools import prepare_channel
from test_growth_account_connection import account, login_work
from test_growth_channel_operation import channel_work


def native_work(aid):
    ref,data,social = channel_work(aid)
    claim = advance(ref)['claim']
    with tx(aid) as c:
        target = one(c,'SELECT * FROM lead.account_targets WHERE account_id=%s',(social['id'],))
        url = 'https://fixture.test/target/' + str(target['id'])
        c.execute('UPDATE lead.account_targets SET canonical_url=%s WHERE id=%s',(url,target['id']))
        method = {'version':'native-browser-v1','account_id':str(social['id']),
            'binding_version':social['binding_version'],'target_url':url,'self_url':url,
            'operations':['publish'],'role':'ordinary_member',
            'selectors':{key:'#'+key for key in ('self_subject','editor_target','editor_body','submit','receipt_author','receipt_body','receipt_url')},
            'request':{'url':url,'method':'POST','body_kind':'json','content_field':'body'},
            'evidence_refs':['fixture:self-and-rule-observation'],
            'platform_permission':{'state':'approved','evidence_refs':['fixture:local-site-permission']}}
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s',
                  (j({'browser_method':method}),target['id']))
        c.execute("UPDATE lead.profile_leases SET epoch=1,state='held',expires_at=now()+interval '10 minutes' WHERE profile_id=%s",(social['profile_id'],))
        session = one(c,"SELECT * FROM lead.browser_sessions WHERE profile_id=%s AND ended_at IS NULL",(social['profile_id'],))
        if not session:
            session = one(c,"""INSERT INTO lead.browser_sessions(tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state)
                SELECT lead.ctx_tenant(),%s,%s,'fixture','fixture',1,control_version,'ready' FROM lead.agents WHERE id=%s RETURNING *""",
                (aid,social['profile_id'],aid))
    action = prepare_channel(aid,ref['task_id'],data)
    actor = SimpleNamespace(aid=aid,profile={'id':social['profile_id']},epoch=1,session=session['id'])
    request = {'method':'POST','url':url,'hasPostData':True,'postData':json.dumps({'body':action['intent']['body']})}
    observed = {'subject':social['external_account_id'],'target_url':url,'body':action['intent']['body']}
    return actor,action,claim,request,observed,social


def test_current_request_claim_is_one_atomic_original_action(growth_agent):
    actor,action,claim,request,observed,_ = native_work(growth_agent)
    prepare_binding(actor,action['id'],claim,1)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _:claim_request(actor,action['id'],claim,1,request,observed),range(2)))
    assert sorted(results)==[False,True]
    with tx(growth_agent) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='dispatching'
        assert one(c,'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s',(action['id'],))['n']==1
    assert not claim_request(actor,action['id'],claim,1,request,observed)


def test_body_identity_generation_and_revocation_fail_before_claim(growth_agent):
    actor,action,claim,request,observed,social = native_work(growth_agent)
    prepare_binding(actor,action['id'],claim,1)
    for generation,facts,body in ((2,observed,request),(1,{**observed,'subject':'another-person'},request),
                                  (1,observed,{**request,'postData':json.dumps({'body':'changed'})})):
        with pytest.raises(ValueError):
            claim_request(actor,action['id'],claim,generation,body,facts)
    set_intent(growth_agent,social['id'],'paused',source='owner_command',source_ref='pause-native')
    set_intent(growth_agent,social['id'],'running',source='owner_command',source_ref='resume-native')
    try:
        assert not claim_request(actor,action['id'],claim,1,request,observed)
    except ValueError as error:
        assert str(error) in ('scope_changed','original_action_reconcile_only')
    with tx(growth_agent) as c:
        attempt=one(c,'SELECT observation FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
        assert not attempt['observation']['browser_request']['claimed']


def test_untrusted_prepared_window_keeps_original_action_unknown(growth_agent):
    actor,action,claim,request,observed,_ = native_work(growth_agent)
    prepare_binding(actor,action['id'],claim,1)
    assert mark_untrusted(actor,1)==1
    assert mark_untrusted(actor,1)==0
    assert not claim_request(actor,action['id'],claim,1,request,observed)
    with tx(growth_agent) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='unknown'
        assert one(c,'SELECT count(*) n FROM lead.actions')['n']==1


def test_another_stable_subject_cannot_replace_bound_account(growth_agent):
    from lead_app.account_connection import complete
    sid=account(growth_agent)
    row,snapshot,social=login_work(growth_agent,sid)
    with pytest.raises(psycopg.errors.RaiseException,match='connection_identity_ambiguous'):
        complete(growth_agent,row,snapshot,{'status':'authenticated','operating_subject':{
            'namespace':'lab','external_id':'another-stable-subject','id_kind':'stable'},'evidence_refs':['fixture:self']})
    with tx(growth_agent) as c:
        assert one(c,'SELECT count(*) n FROM lead.social_accounts')['n']==1
        assert one(c,'SELECT external_account_id FROM lead.social_accounts WHERE id=%s',(sid,))['external_account_id']==social['external_account_id']


def test_live_scope_rechecks_profile_lease_and_account_intent(growth_agent):
    from lead_app.browser_runtime import PersistentRuntime
    actor,action,claim,_,_,social = native_work(growth_agent)
    with tx(growth_agent) as c:
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(action['task_id'],))
        agent=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(growth_agent,))
        intent=one(c,'SELECT revision FROM lead.account_operating_intents WHERE account_id=%s',(social['id'],))
    scope={'task_id':str(task['id']),'control_version':agent['control_version'],
           'task_generation':task['input'].get('task_generation'),'claim':claim,
           'account_id':str(social['id']),'binding_version':social['binding_version'],
           'intent_revision':intent['revision']}
    runtime=PersistentRuntime(actor)
    assert runtime.current_scope(scope)['id']==task['id']
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.profile_leases SET expires_at=now()-interval '1 second' WHERE profile_id=%s",(social['profile_id'],))
    with pytest.raises(ValueError,match='browser_profile_lease_expired'):
        runtime.current_scope(scope)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.profile_leases SET expires_at=now()+interval '10 minutes' WHERE profile_id=%s",(social['profile_id'],))
    with pytest.raises(ValueError,match='browser_account_intent_changed'):
        runtime.current_scope({**scope,'intent_revision':intent['revision']-1})
    set_intent(growth_agent,social['id'],'paused',source='owner_command',source_ref='revoke-observation')
    with pytest.raises(ValueError):
        runtime.current_scope(scope)


def test_live_native_scope_cannot_follow_a_replaced_execution_route(growth_agent):
    from lead_app.browser_runtime import PersistentRuntime
    from lead_app.execution_routes import register
    actor,action,claim,_,_,_=native_work(growth_agent)
    with tx(growth_agent) as c:
        task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(action['task_id'],))
        agent=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(growth_agent,))
    scope={'task_id':str(task['id']),'control_version':agent['control_version'],
        'task_generation':task['input']['task_generation'],'claim':claim,'origins':['https://fixture.test']}
    runtime=PersistentRuntime(actor)
    assert runtime.current_scope(scope)['id']==task['id']
    register(growth_agent,actor.profile['id'],executor_ref='replacement-executor')
    with pytest.raises(ValueError,match='browser_execution_route_changed'):
        runtime.current_scope(scope)


def test_live_native_scope_stops_when_its_actual_managed_exit_is_disabled(growth_agent):
    from lead_app.browser_runtime import PersistentRuntime
    from lead_app.config import database_kwargs
    from lead_app.managed_network import add
    actor,action,claim,_,_,_=native_work(growth_agent)
    import uuid
    marker=uuid.uuid4()
    pool=add('native-exit-'+marker.hex,'http://127.0.0.1:19998',
        '93.184.'+str(marker.bytes[0])+'.'+str(marker.bytes[1]),agent_id=growth_agent)
    try:
        with tx(growth_agent) as c:
            task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(action['task_id'],))
            agent=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(growth_agent,))
        scope={'task_id':str(task['id']),'control_version':agent['control_version'],
            'task_generation':task['input']['task_generation'],'claim':claim,'origins':['https://fixture.test']}
        runtime=PersistentRuntime(actor)
        assert runtime.current_scope(scope)['id']==task['id']
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute("UPDATE lead.proxy_pool SET state='disabled' WHERE id=%s",(pool['id'],))
        with pytest.raises(ValueError,match='browser_exit_changed'):
            runtime.current_scope(scope)
    finally:
        # The isolated test pool must not affect a later test's automatic claim.
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute("UPDATE lead.proxy_assignments SET released_at=now() WHERE pool_id=%s",(pool['id'],))
            c.execute("UPDATE lead.proxy_pool SET state='retired' WHERE id=%s",(pool['id'],))


def test_control_loss_waits_for_late_prepared_attempt_and_invalidates_lease(growth_agent,monkeypatch):
    import threading

    from lead_app.growth import tools
    actor,action,claim,_,_,_=native_work(growth_agent)
    checked=threading.Event()
    release=threading.Event()
    original=tools.submission_gate
    def delayed(c,*args,**kwargs):
        value=original(c,*args,**kwargs)
        checked.set()
        assert release.wait(5)
        return value
    monkeypatch.setattr(tools,'submission_gate',delayed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        preparing=workers.submit(prepare_binding,actor,action['id'],claim,1)
        assert checked.wait(5)
        lost=workers.submit(mark_untrusted,actor,1)
        assert not lost.done()
        release.set()
        preparing.result(timeout=5)
        assert lost.result(timeout=5)==1
    with tx(growth_agent) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='unknown'
        assert one(c,'SELECT state FROM lead.profile_leases WHERE profile_id=%s',(actor.profile['id'],))['state']=='released'


async def test_normal_native_close_fences_prepared_action_and_records_unknown(growth_agent):
    from unittest.mock import AsyncMock

    from lead_app.browser_runtime import PersistentRuntime
    actor,action,claim,_,_,_=native_work(growth_agent)
    actor.forward_proxy=SimpleNamespace(close=AsyncMock())
    prepare_binding(actor,action['id'],claim,1)
    runtime=PersistentRuntime(actor)
    await runtime.close()
    with tx(growth_agent) as c:
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='unknown'
        assert one(c,'SELECT state,ended_at FROM lead.browser_sessions WHERE id=%s',(actor.session,))['ended_at']
