from datetime import timedelta

import pytest
from cryptography.exceptions import InvalidTag
from lead_app.connection_control import LoginInput, seal, transition, unseal
from lead_app.db import one, tx
from lead_app.growth.contracts import utcnow
from test_growth_account_connection import account, login_work


def test_temporary_login_is_not_plaintext_and_cannot_cross_connection(growth_agent):
    login=LoginInput(username='synthetic-user',password='synthetic-pass')
    assert 'synthetic-pass' not in repr(login)
    encrypted=seal(growth_agent,'first',login)
    assert b'synthetic-pass' not in encrypted
    assert unseal(growth_agent,'first',encrypted)=={'username':'synthetic-user','password':'synthetic-pass'}
    with pytest.raises(InvalidTag):
        unseal(growth_agent,'second',encrypted)


def test_challenge_versions_reject_delayed_answers_and_cancel_clears_login(growth_agent):
    sid=account(growth_agent)
    _,snap,_=login_work(growth_agent,sid)
    cipher=seal(growth_agent,snap['id'],{'username':'user','password':'secret'})
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET control_mode='automatic',encrypted_login=%s WHERE id=%s",(cipher,snap['id']))
    owner=transition(growth_agent,snap['id'],1,'owner',reason='Complete the current OTP')
    assert owner['challenge_version']==2 and owner['control_mode']=='owner'
    with pytest.raises(ValueError,match='challenge_changed'):
        transition(growth_agent,snap['id'],1,'automatic')
    resumed=transition(growth_agent,snap['id'],2,'automatic')
    assert resumed['challenge_version']==3
    transition(growth_agent,snap['id'],3,'automatic',cancel=True)
    with tx(growth_agent) as c:
        assert one(c,'SELECT state,encrypted_login FROM lead.takeover_sessions WHERE id=%s',(snap['id'],))=={'state':'ended','encrypted_login':None}


def test_expired_connection_cannot_resume_and_ends_credential_lifetime(growth_agent):
    sid=account(growth_agent)
    _,snap,_=login_work(growth_agent,sid)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET expires_at=%s WHERE id=%s",(utcnow()-timedelta(seconds=1),snap['id']))
    assert transition(growth_agent,snap['id'],1,'automatic')['state']=='expired'


def test_native_profiles_share_other_origins_but_split_same_site_accounts(growth_agent,monkeypatch):
    from lead_app import account_profiles
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    first=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://a.example/login')
    second=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://b.example/auth')
    third=account_profiles.for_platform(growth_agent,'web',create=True,new_binding=True,site_url='https://a.example/login')
    assert first['provider']=='chromium' and first['id']==second['id']
    assert first['account_id'] != second['account_id']
    assert third['id'] != first['id']
    repeated=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://b.example/another-path')
    assert repeated['account_id']==second['account_id']


def test_account_read_does_not_inherit_another_shared_account_origin(growth_agent,monkeypatch):
    from lead_app import account_profiles
    from lead_app.research import account_read_origins
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    first=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://a.example/login')
    second=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://b.example/login')
    assert first['id']==second['id']
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.social_accounts SET state='connected',external_account_id='synthetic-self' WHERE id=ANY(%s::uuid[])",([str(first['account_id']),str(second['account_id'])],))
        assert account_read_origins(c,first['account_id'],1)==['https://a.example']
        assert account_read_origins(c,second['account_id'],1)==['https://b.example']


def test_due_recovery_clears_expired_login_even_after_task_review(growth_agent):
    from lead_app.growth.runtime import recover_due
    sid=account(growth_agent)
    row,snap,_=login_work(growth_agent,sid)
    cipher=seal(growth_agent,snap['id'],{'username':'fixture','password':'temporary'})
    deadline=utcnow()-timedelta(seconds=1)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET encrypted_login=%s,expires_at=%s WHERE id=%s",(cipher,deadline,snap['id']))
        c.execute("UPDATE lead.tasks SET state='failed',completed_at=now() WHERE id=%s",(row['id'],))
    recover_due(growth_agent)
    recover_due(growth_agent)
    with tx(growth_agent) as c:
        ended=one(c,'SELECT * FROM lead.takeover_sessions WHERE id=%s',(snap['id'],))
        assert ended['state']=='expired' and ended['encrypted_login'] is None
        assert ended['challenge_version']==snap['challenge_version']+1
        assert ended['input_seq']==snap['input_seq']+1
        assert one(c,"SELECT count(*) n FROM lead.outbox_events WHERE payload->>'reason'='connection_expired'")['n']==1


def test_web_origin_canonicalization_preserves_nondefault_protocol_ports():
    from lead_app.account_profiles import connection_origin
    assert connection_origin('http://example.com:443/login')=='http://example.com:443'
    assert connection_origin('https://example.com:80/login')=='https://example.com:80'
    assert connection_origin('https://example.com:443/login')=='https://example.com'


def test_same_site_cookie_domains_do_not_share_an_identity_group(growth_agent,monkeypatch):
    from lead_app import account_profiles
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    first=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://one.example.com/login')
    second=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://two.example.com/login')
    assert first['id']!=second['id']


def test_connection_control_uses_account_permission():
    from lead_app.admin_permissions import workbench_permission
    assert workbench_permission('POST','/api/agents/fixture/connections/connection/control')=='accounts.manage'


def test_old_connection_identity_proof_does_not_cross_challenge_version(growth_agent):
    import psycopg
    from lead_app.account_connection import complete
    sid=account(growth_agent)
    row,snapshot,social=login_work(growth_agent,sid)
    transition(growth_agent,snapshot['id'],1,'automatic')
    observed={'status':'authenticated','operating_subject':{'namespace':'lab','external_id':social['external_account_id'],
        'id_kind':'stable'},'evidence_refs':['fixture:old-proof']}
    with pytest.raises(psycopg.errors.RaiseException,match='connection_changed'):
        complete(growth_agent,row,snapshot,observed)


def test_human_input_must_finish_before_automatic_control_can_resume(growth_agent):
    sid=account(growth_agent)
    _,snapshot,_=login_work(growth_agent,sid)
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.takeover_sessions SET input_inflight=true WHERE id=%s',(snapshot['id'],))
    with pytest.raises(ValueError,match='input_in_progress'):
        transition(growth_agent,snapshot['id'],1,'automatic')


@pytest.mark.parametrize('login',[
    {'username':'fixture','password':'SENSITIVE-PASSWORD-'+('x'*2100)},
    {'username':'fixture','password':{'SENSITIVE-PASSWORD':'invalid-type'}},
    {'username':'fixture','password':'SENSITIVE-PASSWORD','unexpected':'SENSITIVE-EXTRA'},
])
def test_login_validation_never_echoes_rejected_secret(growth_agent,login):
    from fastapi.testclient import TestClient
    from lead_app import api
    from lead_app.security import token
    with TestClient(api.app) as client:
        client.cookies.set('lead_session',token('owner'))
        response=client.post(f'/api/agents/{growth_agent}/connect',json={'platform':'web','site_url':'https://fixture.example/login','login':login},
            headers={'Origin':'http://127.0.0.1:3100','X-Lead-Request':'1'})
    assert response.status_code==422
    assert 'SENSITIVE-' not in response.text and 'input' not in response.json()['detail'][0]


async def test_begin_reuses_only_same_account_connection_in_shared_instance(growth_agent,monkeypatch):
    from lead_app import account_connection, account_profiles
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    calls=[]
    async def opened(aid,profile_id,path,body=None,**kwargs):
        calls.append((profile_id,path))
        with tx(aid) as c:
            session=one(c,"SELECT id FROM lead.browser_sessions WHERE profile_id=%s AND ended_at IS NULL",(profile_id,))
            if not session:
                session=one(c,"""INSERT INTO lead.browser_sessions(tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state)
                    VALUES(lead.ctx_tenant(),%s,%s,'synthetic','fixture',1,1,'ready') RETURNING id""",(aid,profile_id))
        return {'session_id':session['id'],'profile_id':str(profile_id)}
    monkeypatch.setattr(account_connection,'rpc',opened)
    first=await account_connection.begin(growth_agent,'web',site_url='https://a.example/login')
    second=await account_connection.begin(growth_agent,'web',site_url='https://b.example/login')
    repeated=await account_connection.begin(growth_agent,'web',site_url='https://a.example/login')
    assert first['takeover_id']!=second['takeover_id'] and first['account_id']!=second['account_id']
    assert repeated['takeover_id']==first['takeover_id']
    assert len(calls)==2
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.takeover_sessions WHERE state='active'")['n']==2


def test_archiving_original_project_clears_login_credentials(growth_agent):
    from lead_app import domain
    sid=account(growth_agent)
    _,snapshot,_=login_work(growth_agent,sid)
    cipher=seal(growth_agent,snapshot['id'],{'username':'synthetic','password':'password'})
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.takeover_sessions SET encrypted_login=%s WHERE id=%s',(cipher,snapshot['id']))
    domain.set_control(growth_agent,'archive')
    with tx(growth_agent) as c:
        assert one(c,'SELECT state,encrypted_login FROM lead.takeover_sessions WHERE id=%s',(snapshot['id'],))=={'state':'expired','encrypted_login':None}


def test_native_owner_connection_keeps_versioned_protocol_capability():
    from lead_app.account_connection import connection_reply
    value=connection_reply({'id':'fixture','completed_account_id':None,'control_mode':'owner',
        'challenge_version':2,'connection_data':{'owner_action':'OTP'},'expires_at':utcnow()},'account','web',native=True)
    assert value['automatic_connection'] is True and value['control_mode']=='owner' and value['challenge_version']==2


def test_native_shared_profiles_keep_transport_and_exit_compatibility(growth_agent,monkeypatch):
    import json

    from lead_app import account_profiles
    from lead_app.execution_routes import register, resolve
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    monkeypatch.setenv('LEAD_BROWSER_TRANSPORTS',json.dumps({'shared':'http://127.0.0.1:19001','other':'http://127.0.0.1:19002'}))
    monkeypatch.setenv('LEAD_BROWSER_ROUTE_DEFAULTS',json.dumps({'facebook':'other'}))
    first=account_profiles.for_platform(growth_agent,'web',create=True,site_url='https://a.example/login')
    original=register(growth_agent,first['id'])
    facebook=account_profiles.for_platform(growth_agent,'facebook',create=True)
    reddit=account_profiles.for_platform(growth_agent,'reddit',create=True)
    assert first['id']!=facebook['id'] and first['id']!=reddit['id']
    assert resolve(growth_agent,profile_id=first['id'])==original


def test_native_public_and_social_slots_preserve_original_registered_exit(growth_agent,monkeypatch):
    from lead_app import account_profiles, reddit_login
    monkeypatch.setattr(account_profiles,'native_available',lambda:True)
    login=account_profiles.for_platform(growth_agent,'reddit',create=True)
    public=account_profiles.public_research(growth_agent)
    assert public['provider']=='chromium' and public['slot']=='native-public-research'
    assert reddit_login.assign_exit(growth_agent,public) is None
    monkeypatch.setenv('LEAD_REDDIT_IPV4_'+growth_agent.replace('-','').upper(),'35.72.111.159')
    assert reddit_login.assign_exit(growth_agent,login)=={'server':reddit_login.SOCKET}
    assert reddit_login.assign_exit(growth_agent,public)=={'server':reddit_login.SOCKET}
    assert login['network_identity_id']==public['network_identity_id'] and login['storage_ref']!=public['storage_ref']


def test_live_challenge_observation_rotates_only_changed_marker_and_fences_old_input(growth_agent):
    from lead_app.connection_control import refresh_owner_challenge
    sid=account(growth_agent)
    _,snapshot,_=login_work(growth_agent,sid)
    version=transition(growth_agent,snapshot['id'],1,'owner')
    marker={'kind':'qr','fingerprint':'old-fingerprint','document':['frame','loader-1','https://site.test/challenge']}
    first=refresh_owner_challenge(growth_agent,snapshot['id'],version['challenge_version'],version['input_seq'],marker)
    same=refresh_owner_challenge(growth_agent,snapshot['id'],first['challenge_version'],first['input_seq'],marker)
    assert same['challenge_version']==first['challenge_version']
    second=refresh_owner_challenge(growth_agent,snapshot['id'],first['challenge_version'],first['input_seq'],{**marker,'fingerprint':'new-fingerprint'})
    assert second['challenge_version']==first['challenge_version']+1 and second['input_seq']==first['input_seq']+1
    with pytest.raises(ValueError,match='observation_expired'):
        refresh_owner_challenge(growth_agent,snapshot['id'],first['challenge_version'],first['input_seq'],marker)
    with pytest.raises(ValueError,match='challenge_changed'):
        transition(growth_agent,snapshot['id'],first['challenge_version'],'automatic')
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.takeover_sessions SET input_inflight=true WHERE id=%s',(snapshot['id'],))
    with pytest.raises(ValueError,match='observation_expired'):
        refresh_owner_challenge(growth_agent,snapshot['id'],second['challenge_version'],second['input_seq'],marker)


def test_changed_challenge_cancels_only_the_old_composition_generation(growth_agent):
    from lead_app.connection_control import refresh_owner_challenge
    sid=account(growth_agent)
    _,snapshot,_=login_work(growth_agent,sid)
    owner=transition(growth_agent,snapshot['id'],1,'owner')
    first={'kind':'otp','fingerprint':'first','document':['frame','loader','url']}
    refresh_owner_challenge(growth_agent,snapshot['id'],owner['challenge_version'],owner['input_seq'],first)
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.takeover_sessions SET composing=true WHERE id=%s',(snapshot['id'],))
    same=refresh_owner_challenge(growth_agent,snapshot['id'],owner['challenge_version'],owner['input_seq'],first)
    with pytest.raises(ValueError,match='input_in_progress'):
        transition(growth_agent,snapshot['id'],same['challenge_version'],'automatic')
    changed=refresh_owner_challenge(growth_agent,snapshot['id'],same['challenge_version'],same['input_seq'],{**first,'fingerprint':'second'})
    assert changed['challenge_version']>same['challenge_version']
    with tx(growth_agent) as c:
        assert one(c,'SELECT composing FROM lead.takeover_sessions WHERE id=%s',(snapshot['id'],))['composing'] is False
