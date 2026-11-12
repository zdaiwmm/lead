from datetime import timedelta

from fastapi.testclient import TestClient
from lead_app import api
from lead_app.connection_control import transition
from lead_app.db import all_rows, one, tx
from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from lead_app.growth.runtime import _resource_condition, advance, finish
from lead_app.security import token
from test_growth_account_connection import account, login_work


def waiting_connection(aid):
    identifier=account(aid)
    row,connection,_=login_work(aid,identifier)
    with tx(aid) as c:
        c.execute("UPDATE lead.browser_profiles SET provider='chromium' WHERE id=%s",(connection['profile_id'],))
    ref={'agent_id':aid,'task_id':str(row['id']),'task_generation':row['input']['task_generation']}
    claim=advance(ref)['claim']
    owner=transition(aid,connection['id'],connection['challenge_version'],'owner')
    finish(claim,WorkResult(outcome='waiting',wait=Wait(reason='challenge',producer='account_connection',
        recovery_action='owner',episode_root=row['id'],check_at=utcnow()+timedelta(minutes=10))))
    return ref,connection,owner,claim


def control(aid,connection,owner,action):
    with TestClient(api.app) as client:
        client.cookies.set('lead_session',token('owner'))
        return client.post(f"/api/agents/{aid}/connections/{connection['id']}/control",
            headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'},
            json={'action':action,'challenge_version':owner['challenge_version']})


def condition(aid,ref):
    with tx(aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        agent=one(c,'SELECT * FROM lead.agents WHERE id=%s',(aid,))
        return _resource_condition(c,agent,row,TaskSpec.model_validate(row['input']['spec']))


def test_owner_handoff_changes_condition_and_wakes_only_original_wait(growth_agent):
    ref,connection,owner,claim=waiting_connection(growth_agent)
    before=condition(growth_agent,ref)
    with tx(growth_agent) as c:
        events={str(row['id']) for row in all_rows(c,"SELECT id FROM lead.outbox_events WHERE event_type='growth.work'")}
    response=control(growth_agent,connection,owner,'resume')
    assert response.status_code==200,response.text
    assert condition(growth_agent,ref)!=before
    with tx(growth_agent) as c:
        work=[row for row in all_rows(c,"SELECT * FROM lead.outbox_events WHERE event_type='growth.work'") if str(row['id']) not in events]
        assert [row['payload'] for row in work]==[ref]
        task=one(c,'SELECT input,result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        assert task['input']['task_generation']==ref['task_generation']
        assert task['result']['runtime']['attempts']==1
    ready=advance(ref)
    assert ready['state']=='ready' and ready['claim']['attempt_token']!=claim['attempt_token']
    assert control(growth_agent,connection,owner,'resume').status_code==409


def test_cancel_ends_original_wait_without_restarting_authentication(growth_agent):
    ref,connection,owner,_=waiting_connection(growth_agent)
    response=control(growth_agent,connection,owner,'cancel')
    assert response.status_code==200,response.text
    assert advance(ref)['state']=='terminal'
    with tx(growth_agent) as c:
        task=one(c,'SELECT state,result FROM lead.tasks WHERE id=%s',(ref['task_id'],))
    assert task['state']=='canceled'
    assert task['result']['runtime']['attempt_token'] is None


def test_actual_same_account_reverification_changes_read_condition(growth_agent):
    from lead_app.growth.runtime import _current_research_resource
    identifier=account(growth_agent)
    scope={'provider':'account_browser','operation':'read','origin':'https://fixture.test',
           'access_mode':'account','account_id':identifier}
    with tx(growth_agent) as c:
        before=_current_research_resource(c,scope)
        c.execute("UPDATE lead.social_accounts SET verified_at=verified_at+interval '1 second' WHERE id=%s",(identifier,))
        assert _current_research_resource(c,scope)!=before
