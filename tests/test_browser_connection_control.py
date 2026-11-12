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
