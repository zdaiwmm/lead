"""Durable owner evidence never outlives its original identity/exit/instance."""

import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app.db import j, one, tx
from lead_app.growth.contracts import utcnow
from test_browser_action_transactions import native_work
from test_growth_account_connection import login_work


@pytest.mark.parametrize('change',[None,'withdraw','expired','binding','intent','control','session','epoch','profile','route','claim','generation'])
def test_canonical_frame_role_is_bound_to_original_authority(growth_agent,change):
    from lead_app.native_frame_roles import canonical_roles, digest
    actor,action,claim,*_=native_work(growth_agent,provider='chromium')
    method=action['intent']['browser_method']
    _,connection,_=login_work(growth_agent,method['account_id'])
    runtime=SimpleNamespace(actor=actor,execution_ref=copy.deepcopy(action['intent']['execution_ref']))
    scope={'task_id':str(action['task_id']),'task_generation':claim['ref']['task_generation'],
        'claim':{'attempt_token':claim['attempt_token']},'generation':actor.epoch,'account_id':method['account_id'],
        'binding_version':action['intent']['binding_version'],'intent_revision':action['intent']['intent_revision'],
        'control_version':action['expected_control_version'],'origins':[method['target_url']]}
    source={'state':'approved','candidate_id':'a'*64,'resource_url':'https://reference.example/public',
        'parent_url':method['target_url'],'expires_at':(utcnow()+timedelta(minutes=10)).isoformat(),
        'profile_id':str(actor.profile['id']),'execution_ref':runtime.execution_ref,
        'bound':{'account_id':str(method['account_id']),'binding_version':scope['binding_version'],
            'intent_revision':scope['intent_revision'],'control_version':scope['control_version'],
            'browser_session_id':str(actor.session),'browser_epoch':actor.epoch}}
    source['source_proof']=digest(source)
    role={key:source[key] for key in ('source_proof','candidate_id','resource_url','parent_url','expires_at')}
    role.update({key:copy.deepcopy(scope[key]) for key in ('task_id','task_generation','claim','generation')})
    scope['frame_roles']=[role]
    if change=='withdraw':
        source['state']='refused'
    elif change=='expired':
        source['expires_at']=(utcnow()-timedelta(seconds=1)).isoformat()
    elif change in ('binding','intent','control','epoch'):
        key={'binding':'binding_version','intent':'intent_revision','control':'control_version','epoch':'browser_epoch'}[change]
        source['bound'][key]+=1
    elif change=='session':
        source['bound']['browser_session_id']='other-session'
    elif change=='profile':
        source['profile_id']='other-profile'
    elif change=='route':
        source['execution_ref']=dict(source['execution_ref'],route_generation=source['execution_ref']['route_generation']+1)
    elif change=='claim':
        role['claim']={'attempt_token':'old-claim'}
    elif change=='generation':
        role['task_generation']+=1
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now(),completed_account_id=%s,connection_data=connection_data||%s WHERE id=%s",
            (method['account_id'],j({'read_dependencies':[source]}),connection['id']))
        if change is None:
            assert canonical_roles(c,runtime,scope)==[role]
        else:
            with pytest.raises(ValueError,match='frame_source_expired'):
                canonical_roles(c,runtime,scope)


def test_owner_dependency_endpoints_use_account_management_permission():
    from lead_app.admin_permissions import workbench_permission
    for verb in ('GET','POST'):
        assert workbench_permission(verb,'/api/agents/a/connections/c/read-dependencies')=='accounts.manage'


@pytest.mark.parametrize('url,authentication',[
    ('https://reference.example/public?lang=en',False),('https://reference.example/oauth/authorize',True),
    ('https://reference.example/embed?access_token=synthetic-secret',True),
    ('https://reference.example/embed?signature=synthetic-signature',True),
    ('https://reference.example/frame?session=synthetic-session',True)])
def test_authentication_dependencies_are_not_public_resource_candidates(url,authentication):
    from lead_app.native_frame_roles import authentication_resource, refused_address
    assert authentication_resource(url) is authentication
    assert '?' not in refused_address(url) and 'synthetic' not in refused_address(url)


def test_owner_protocol_capability_is_separate_from_phase(growth_agent):
    from lead_app.account_connection import connection_reply
    actor,action,*_=native_work(growth_agent,provider='chromium')
    _,connection,_=login_work(growth_agent,action['intent']['browser_method']['account_id'])
    with tx(growth_agent) as c:
        row=one(c,'SELECT * FROM lead.takeover_sessions WHERE id=%s',(connection['id'],))
    assert connection_reply(row,'account','web',native=True)['can_review_read_dependencies'] is True
    row['control_mode']='automatic'
    assert connection_reply(row,'account','web',native=True)['can_review_read_dependencies'] is False
    row['control_mode']='owner'
    assert connection_reply(row,'account','web',native=False)['can_review_read_dependencies'] is False


def test_read_dependency_http_requires_account_permission_and_ignores_rpc_dto(growth_agent,monkeypatch):
    from fastapi.testclient import TestClient
    from lead_app import account_connection, admin_permissions
    from lead_app.api import app
    actor,action,*_=native_work(growth_agent,provider='chromium')
    _,connection,_=login_work(growth_agent,action['intent']['browser_method']['account_id'])
    path=f"/api/agents/{growth_agent}/connections/{connection['id']}/read-dependencies"
    calls=[]
    async def rpc(aid,profile_id,path,body,**kwargs):
        calls.append((aid,str(profile_id),body['operation']))
        return {'connection_id':'invented','challenge_version':999,'candidates':[{'state':'approved'}]}
    monkeypatch.setattr(account_connection,'rpc',rpc)
    monkeypatch.setenv('LEAD_AUTH_MODE','local_owner')
    with TestClient(app,headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'}) as client:
        assert client.post('/api/session',json={}).status_code==200
        monkeypatch.setattr(admin_permissions,'resolve',lambda _: {'role':'operator','capabilities':['agents.control'],
            'agent_ids':None,'access_resolved':True})
        assert client.get(path).status_code==403
        assert client.post(path,json={'challenge_version':1,'candidate_id':'a'*64,'decision':'approve'}).status_code==403
        assert not calls
        monkeypatch.setattr(admin_permissions,'resolve',lambda _: {'role':'operator','capabilities':['accounts.manage'],
            'agent_ids':None,'access_resolved':True})
        response=client.get(path)
        assert response.status_code==200
        assert response.json()=={'connection_id':str(connection['id']),'challenge_version':1,'candidates':[]}
        assert calls==[(growth_agent,str(actor.profile['id']),'read_dependencies')]
