"""A narrow profile proof needs real scoped measurement and a fresh self read."""
import hashlib
import os
from datetime import timedelta
from uuid import UUID

import pytest
from lead_app import browser, domain, execution_routes, identity, telegram
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx, uid
from lead_app.growth.contracts import utcnow
from lead_app.scope import service_scope
from test_growth_telegram_community import community as community
from test_native_declarative_forms_dom import common_page as common_page

REAL_OPEN_ACCOUNT = telegram.open_account
REAL_INSPECT_ACCOUNT = telegram.inspect_account


def proof_count(aid):
    with tx(aid) as c:
        return one(c, "SELECT count(*) n FROM lead.identity_checks WHERE checks ? 'community_read'")["n"]


@pytest.fixture
def observed(community, monkeypatch, tmp_path):
    env = community
    env.calls = []
    env.mutate = None
    env.subject = env.account['external_account_id']
    env.device_mode = 'passed'
    monkeypatch.setattr(identity, 'STATE', tmp_path)

    async def measurement(actor):
        env.calls.append('measurement')
        check_id = uid()
        if env.device_mode == 'missing':
            return {'id': check_id, 'verified': True}
        if env.device_mode == 'failed':
            return {'id': check_id, 'verified': False}
        with tx(env.aid) as c:
            profile = one(c, 'SELECT * FROM lead.browser_profiles WHERE id=%s', (actor.profile['id'],))
            ip = '23.' + '.'.join(str(x) for x in UUID(env.aid).bytes[:3])
            c.execute("""UPDATE lead.network_identities SET mode='dedicated',state='verified',expected_ip=%s,
                verified_at=now(),valid_until=now()+interval '15 minutes' WHERE id=%s""", (ip, profile['network_identity_id']))
            one(c, """INSERT INTO lead.identity_checks(id,tenant_id,agent_id,profile_id,network_identity_id,
                engine_version,config_hash,observed_ip,result,checks,checked_at,valid_until)
                VALUES(%s,lead.ctx_tenant(),%s,%s,%s,%s,%s,%s,'passed',%s,%s,now()+interval '15 minutes') RETURNING id""",
                (check_id, env.aid, profile['id'], profile['network_identity_id'], profile['engine_version'],
                 profile['config_hash'], ip, j({'scope': 'fixed_exit_v1', 'fixture_measurement': True}),
                 utcnow() - timedelta(hours=1) if env.device_mode == 'stale' else utcnow()))
        return {'id': check_id, 'verified': True}

    async def open_account(page, expected):
        assert page is env.actor.page and expected == env.account['external_account_id']
        env.calls.append('open_self')

    async def inspect_account(page):
        env.calls.append('inspect_self')
        if env.mutate:
            env.mutate()
        return {'platform': 'telegram', 'external_account_id': env.subject, 'display_name': 'Fixture self'}

    monkeypatch.setattr(identity, 'check', measurement)
    monkeypatch.setattr(telegram, 'open_account', open_account)
    monkeypatch.setattr(telegram, 'inspect_account', inspect_account)
    return env


async def produce(env):
    with service_scope():
        return await identity.community_read_check(env.actor, env.account['id'], execution_ref=env.actor.execution_ref)


async def test_metadata_only_or_stale_measurement_never_becomes_community_proof(observed):
    for mode in ('failed', 'missing', 'stale'):
        observed.device_mode = mode
        with pytest.raises(ValueError, match='community_read_(environment_unverified|device_proof_changed)'):
            await produce(observed)
        assert proof_count(observed.aid) == 0


async def test_proof_is_server_generated_scoped_and_does_not_change_existing_work(observed):
    with tx(observed.aid) as c:
        before = one(c, 'SELECT control_version,config_revision,authorization_version FROM lead.agents WHERE id=%s', (observed.aid,))
        tasks = c.execute('SELECT id,state,input,result FROM lead.tasks ORDER BY id').fetchall()
    result = await produce(observed)
    assert set(result) == {'verified', 'scope', 'proof_ref'} and result['verified'] is True
    assert result['scope'] == 'community_read_v1' and result['proof_ref'].startswith('identity-check:')
    with tx(observed.aid) as c:
        row = one(c, 'SELECT * FROM lead.identity_checks WHERE id=%s', (result['proof_ref'].split(':', 1)[1],))
        proof = row['checks']['community_read']
        assert row['result'] == 'passed' and row['evidence_ref']
        assert proof['agent_id'] == observed.aid and proof['account_id'] == str(observed.account['id'])
        assert proof['profile_id'] == str(observed.actor.profile['id'])
        assert proof['binding_version'] == observed.account['binding_version']
        assert proof['external_self_sha256'] == hashlib.sha256(observed.subject.encode()).hexdigest()
        assert proof['session_id'] == str(observed.actor.session) and proof['epoch'] == observed.actor.epoch
        assert proof['execution_ref'] == observed.actor.execution_ref.model_dump(mode='json')
        assert proof['operations'] == ['search', 'inspect'] and proof['device_check_id'] != str(row['id'])
        assert proof['source'] == 'server_verification'
        device = one(c, 'SELECT valid_until,checked_at FROM lead.identity_checks WHERE id=%s', (proof['device_check_id'],))
        assert row['valid_until'] == device['valid_until'] and row['checked_at'] >= device['checked_at']
        assert one(c, 'SELECT control_version,config_revision,authorization_version FROM lead.agents WHERE id=%s', (observed.aid,)) == before
        assert c.execute('SELECT id,state,input,result FROM lead.tasks ORDER BY id').fetchall() == tasks
        audit = one(c, "SELECT actor_type,payload FROM lead.audit_events WHERE type='community.read_proof_observed'")
        assert audit['actor_type'] == 'service' and audit['payload']['proof_ref'] == result['proof_ref']
    assert observed.calls == ['measurement', 'open_self', 'inspect_self']


@pytest.mark.parametrize('change', ['paused', 'revoked', 'binding', 'route', 'session', 'profile'])
async def test_live_scope_drift_after_self_observation_refuses_the_proof(observed, monkeypatch, change):
    def mutate():
        if change == 'paused':
            domain.set_control(observed.aid, 'pause')
        elif change == 'revoked':
            set_intent(observed.aid, observed.account['id'], 'revoked', source='owner_command', source_ref='fixture:revoke-read')
        elif change == 'route':
            monkeypatch.setattr(execution_routes, 'transports', lambda: {'shared': 'http://127.0.0.1:18089'})
        elif change == 'profile':
            observed.actor.profile = {'id': uid()}
        else:
            with tx(observed.aid) as c:
                if change == 'binding':
                    c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s', (observed.account['id'],))
                else:
                    c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE id=%s", (observed.actor.session,))
    observed.mutate = mutate
    with pytest.raises(ValueError, match='community_'):
        await produce(observed)
    assert proof_count(observed.aid) == 0


async def test_wrong_current_self_and_untrusted_service_context_cannot_supply_proof(observed):
    with pytest.raises(ValueError, match='community_read_service_scope_required'):
        await identity.community_read_check(observed.actor, observed.account['id'], execution_ref=observed.actor.execution_ref)
    assert not observed.calls
    observed.subject = 'different-current-self'
    with pytest.raises(ValueError, match='community_read_observed_account_changed'):
        await produce(observed)
    assert proof_count(observed.aid) == 0


async def test_missing_owner_read_scope_refuses_before_measurement_or_self_navigation(observed):
    with tx(observed.aid) as c:
        c.execute("UPDATE lead.interaction_cards SET payload=jsonb_set(payload,'{allowed_actions}',%s) WHERE id=%s",
                  (j(['join_public_group', 'contextual_reply']), observed.card['id']))
    with pytest.raises(ValueError, match='community_owner_scope_missing'):
        await produce(observed)
    assert not observed.calls and proof_count(observed.aid) == 0


async def test_existing_inspect_output_is_compatible_and_proof_mode_needs_exact_reference(observed, monkeypatch):
    async def get(*args):
        return observed.actor
    monkeypatch.setattr(browser.gateway, 'get', get)
    body = browser.OpenRequest(platform='telegram', account_id=observed.account['id'])
    ordinary = await browser.inspect_account(UUID(observed.aid), body)
    assert ordinary == {'platform': 'telegram', 'external_account_id': observed.subject, 'display_name': 'Fixture self'}
    assert proof_count(observed.aid) == 0 and 'measurement' not in observed.calls
    with pytest.raises(ValueError, match='community_read_exact_account_required'):
        await browser.inspect_account(UUID(observed.aid), browser.AccountInspectRequest(platform='telegram',
            account_id=observed.account['id'], community_read_proof=True))
    with service_scope():
        result = await browser.inspect_account(UUID(observed.aid), browser.AccountInspectRequest(platform='telegram',
            account_id=observed.account['id'], community_read_proof=True, execution_ref=observed.actor.execution_ref))
    assert result['verified'] and proof_count(observed.aid) == 1


@pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS') != '1', reason='Isolated local Telegram DOM required')
@pytest.mark.parametrize('matching_self', [True, False])
async def test_real_current_telegram_dom_is_observed_without_group_or_message_navigation(observed, common_page, monkeypatch, matching_self):
    page, _, requests = common_page
    with tx(observed.aid) as c:
        c.execute("UPDATE lead.social_accounts SET external_account_id='10001' WHERE id=%s", (observed.account['id'],))
        c.execute("UPDATE lead.interaction_cards SET payload=jsonb_set(payload,'{external_account_id}',%s) WHERE id=%s",
                  (j('10001'), observed.card['id']))
    observed.account['external_account_id'] = '10001'
    await page.goto('https://web.telegram.org/k/')
    await page.set_content('<div class="chatlist" style="width:200px;height:80px">Current chats</div>')
    await page.evaluate("subject=>localStorage.setItem('account1',JSON.stringify({userId:subject}))", '10001' if matching_self else '10002')
    observed.actor.page = page
    # Restore the original observers: all browser requests are fulfilled locally.
    monkeypatch.setattr(telegram, 'open_account', REAL_OPEN_ACCOUNT)
    monkeypatch.setattr(telegram, 'inspect_account', REAL_INSPECT_ACCOUNT)
    if matching_self:
        result = await produce(observed)
        assert result['verified'] and proof_count(observed.aid) == 1
    else:
        with pytest.raises(telegram.TelegramNeedsAuth):
            await produce(observed)
        assert proof_count(observed.aid) == 0
    assert set(requests) == {'GET'} and page.url == 'https://web.telegram.org/k/'
