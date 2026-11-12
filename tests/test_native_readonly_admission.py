"""Canonical readonly actions share native slots without taking a write mutex."""
import json
from datetime import timedelta

from lead_app.account_intents import set_intent
from lead_app.browser_actions import claim_request, prepare_binding
from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import ChannelInput, prepare_channel
from test_browser_action_transactions import native_work


def independent_work(aid,action,number):
    with tx(aid) as c:
        account=one(c,'SELECT account_id FROM lead.account_targets WHERE id=%s',(action['target_id'],))['account_id']
        body={'body':'Independent native intention '+str(number),'title':'','format':'text','media':[]}
        content=one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
        data=ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
        ref=submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=account,target_id=action['target_id'],
            payload=data.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
    return ref,data


def unknown(aid,actor,action,claim):
    prepare_binding(actor,action['id'],claim,actor.epoch)
    method=action['intent']['browser_method']
    request={'method':'POST','url':method['request']['url'],'hasPostData':True,'postData':json.dumps({'body':action['intent']['body']})}
    with tx(aid) as c:
        subject=one(c,'SELECT external_account_id FROM lead.social_accounts WHERE id=%s',(method['account_id'],))['external_account_id']
    assert claim_request(actor,action['id'],claim,actor.epoch,request,
        {'subject':subject,'body':action['intent']['body'],'target_url':method['target_url']})
    finish(claim,WorkResult(outcome='unknown'))
    with tx(aid) as c:
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s AND state='dispatching'",(action['id'],))
        ref=one(c,'SELECT input FROM lead.tasks WHERE id=%s',(action['task_id'],))['input']
    return {'agent_id':aid,'task_id':str(action['task_id']),'task_generation':ref['task_generation']}


def setup(aid,provider='chromium'):
    actor,action,claim,*_=native_work(aid,provider=provider)
    return actor,action,unknown(aid,actor,action,claim)


def test_active_unknown_read_allows_one_independent_write_on_same_native_account(growth_agent):
    aid=growth_agent
    _,action,ref=setup(aid)
    reading=advance(ref,now=utcnow()+timedelta(minutes=6))
    assert reading['state']=='ready'
    write,_=independent_work(aid,action,1)
    admitted=advance(write)
    assert admitted['state']=='ready',admitted
    with tx(aid) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE state='running'")['n']==2
        assert one(c,'SELECT state FROM lead.actions WHERE id=%s',(action['id'],))['state']=='unknown'


def test_three_native_readonly_actions_take_only_two_actual_claims(growth_agent):
    aid=growth_agent
    actor,action,first=setup(aid)
    refs=[first]
    for number in (1,2):
        ref,data=independent_work(aid,action,number)
        claim=advance(ref)['claim']
        prepared=prepare_channel(aid,ref['task_id'],data)
        refs.append(unknown(aid,actor,prepared,claim))
    now=utcnow()+timedelta(minutes=6)
    admitted=[advance(ref,now=now) for ref in refs]
    assert [result['state'] for result in admitted]==['ready','ready','wait'],admitted
    with tx(aid) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE state='running'")['n']==2


def test_native_reconcile_also_waits_behind_owner_authentication_for_its_account(growth_agent):
    from test_growth_account_connection import login_work
    aid=growth_agent
    _,action,ref=setup(aid)
    login_work(aid,action['intent']['browser_method']['account_id'])
    result=advance(ref,now=utcnow()+timedelta(minutes=6))
    assert result['state']=='wait' and result['wait']['reason']=='resource_wait',result


def test_same_account_authentication_cannot_start_during_an_active_readonly_claim(growth_agent):
    from test_growth_account_connection import login_work
    aid=growth_agent
    _,action,ref=setup(aid)
    assert advance(ref,now=utcnow()+timedelta(minutes=6))['state']=='ready'
    task,_,_=login_work(aid,action['intent']['browser_method']['account_id'])
    result=advance({'agent_id':aid,'task_id':str(task['id']),'task_generation':task['input']['task_generation']})
    assert result['state']=='wait' and result['wait']['reason']=='resource_wait',result


def test_legacy_unknown_read_still_owns_its_whole_profile(growth_agent):
    aid=growth_agent
    _,action,ref=setup(aid,'camoufox')
    assert advance(ref,now=utcnow()+timedelta(minutes=6))['state']=='ready'
    other,_=independent_work(aid,action,1)
    assert advance(other)['state']=='wait'


def test_fake_runtime_readonly_flag_does_not_release_a_same_account_writer(growth_agent):
    aid=growth_agent
    _,action,claim,*_=native_work(aid,provider='chromium')
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,reconcile_only_action_id}',%s) WHERE id=%s",(j(str(action['id'])),claim['ref']['task_id']))
    other,_=independent_work(aid,action,1)
    assert advance(other)['state']=='wait'


def test_native_readonly_facts_survive_pause_and_review_expiry_without_new_write_permission(growth_agent):
    aid=growth_agent
    _,action,ref=setup(aid)
    set_intent(aid,action['intent']['browser_method']['account_id'],'paused',source='owner_command',source_ref='pause-readonly-admission')
    with tx(aid) as c:
        c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{spec,review_at}',%s) WHERE id=%s",
            (j((utcnow()-timedelta(hours=1)).isoformat()),ref['task_id']))
    assert advance(ref,now=utcnow()+timedelta(minutes=6))['state']=='ready'
