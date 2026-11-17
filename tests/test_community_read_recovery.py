"""Actual scoped observations cannot repair an unrelated target or outbound result."""
import json
from datetime import timedelta

import httpx
import pytest
from lead_app import account_connection, activities, browser
from lead_app.db import j, one, tx, uid
from lead_app.growth import runtime
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import RecoverInput
from lead_app.scope import service_scope
from lead_app.security import internal_token
from test_community_read_proof import observed as observed
from test_community_read_proof import produce
from test_growth_telegram_community import community as community
from test_growth_telegram_community import inspected, source, work


def search_task(env, query='relevant privacy tools'):
    with tx(env.aid) as c:
        return submit_work(c, env.aid, TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
            payload={'community_operation':'search', 'queries':[query]}, dedupe_key=uid(),
            review_at=utcnow()+timedelta(hours=1)))


def failed_read(env, operation='inspect'):
    predecessor = source(env) if operation == 'inspect' else None
    ref = work(env, operation, predecessor) if predecessor else search_task(env)
    for n in range(3):
        claim = runtime.advance(ref, now=utcnow()+timedelta(minutes=2*n))['claim']
        runtime.finish(claim, WorkResult(outcome='failed', failure_code='HTTPStatusError'))
    with tx(env.aid) as c:
        row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    return ref, row, predecessor


def corrupt_source(env, predecessor):
    with tx(env.aid) as c:
        row = one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))
        value = row['result']
        group = value['output']['output']['groups'][0]
        group.update(canonical_url='https://t.me/batchat', subtitle='@batchat_1234, 3 039 members\n蝙',
            title='蝙蝠App-加密聊天/通讯工具/加密软件交流群@batchat')
        c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(value), predecessor['task_id']))
        return digest(one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],)))


@pytest.mark.parametrize('expired', [False, True])
async def test_new_proof_releases_read_fact_but_never_retries_old_bad_source(observed, expired):
    ref, root, predecessor = failed_read(observed)
    source_hash = corrupt_source(observed, predecessor)
    with tx(observed.aid) as c:
        if expired:
            frozen = root['input']
            frozen['spec']['review_at'] = (utcnow()-timedelta(minutes=1)).isoformat()
            episode = root['result']['runtime']['recovery']
            episode.update(probe_deadline=(utcnow()-timedelta(minutes=1)).isoformat(),
                probes_stopped=True, reason='recovery_deadline')
            c.execute('UPDATE lead.tasks SET input=%s,result=%s WHERE id=%s', (j(frozen), j(root['result']), ref['task_id']))
        before = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    proof = await produce(observed)
    with service_scope():
        first = runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])
        again = runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])
        assert runtime.resume_task(observed.aid, ref['task_id'], proof_ref=proof['proof_ref']) is None
    assert first['recovered_episodes'] == 1 and first['resumed_count'] == 0
    assert again['recovered_episodes'] == again['resumed_count'] == 0
    with tx(observed.aid) as c:
        after = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert after['state'] == before['state'] == 'failed' and after['input'] == before['input']
        for key in ('attempts', 'failure_attempts', 'condition_revision'):
            assert after['result']['runtime'][key] == before['result']['runtime'][key]
        for key in ('probes', 'attempts', 'probe_deadline', 'hard_deadline'):
            assert after['result']['runtime']['recovery'][key] == before['result']['runtime']['recovery'][key]
        assert digest(one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))) == source_hash
        wakes = c.execute("SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'").fetchall()
        assert len(wakes) == 1 and wakes[0]['payload']['scope'] == 'community_read_v1'
    assert observed.calls == ['measurement', 'open_self', 'inspect_self']


async def test_bad_source_fails_before_rpc_without_recording_a_profile_episode(observed, monkeypatch):
    predecessor = source(observed)
    ref = work(observed, 'inspect', predecessor)
    saved = corrupt_source(observed, predecessor)
    with pytest.raises(ValueError, match='community_source_binding_invalid'):
        work(observed, 'inspect', predecessor)
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError('No browser access for an invalid source binding')

    monkeypatch.setattr(activities, 'browser_rpc', forbidden)
    claim = runtime.advance(ref)['claim']
    with service_scope():
        result = await runtime.execute(claim)
    assert result['failure_code'] == 'community_source_binding_invalid'
    runtime.finish(claim, result)
    assert calls == []
    with tx(observed.aid) as c:
        row = one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert row['state'] == 'failed' and not row['result']['runtime'].get('recovery')
        assert digest(one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))) == saved


@pytest.mark.parametrize('entrypoint', ['proof_consumer', 'environment_check'])
async def test_read_proof_does_not_resume_join_channel_or_original_unknown_action(observed, monkeypatch, entrypoint):
    original, _ = inspected(observed)
    join = work(observed, 'join', original)
    with tx(observed.aid) as c:
        target = one(c, 'SELECT id FROM lead.account_targets WHERE account_id=%s ORDER BY id LIMIT 1', (observed.account['id'],))
        channel = submit_work(c, observed.aid, TaskSpec(kind='channel', purpose='retained-outbound',
            account_id=observed.account['id'], target_id=target['id'], review_at=utcnow()+timedelta(hours=1),
            payload={'content_id': uid(), 'content_hash': 'f'*64}))
    ref, root, _ = failed_read(observed, 'search')
    unknown = search_task(observed, 'independent private conversations')
    assert unknown['task_id'] != ref['task_id']
    protected = [join['task_id'], channel['task_id'], unknown['task_id']]
    with tx(observed.aid) as c:
        for task_id in protected:
            result = json.loads(json.dumps(root['result']))
            if task_id == join['task_id']:
                result['runtime']['community'] = {'state':'unknown', 'operation_key':'original-member-result'}
            c.execute("UPDATE lead.tasks SET state='failed',result=%s WHERE id=%s", (j(result), task_id))
        grant = one(c, 'SELECT id,target_id FROM lead.authorization_grants WHERE revoked_at IS NULL ORDER BY id LIMIT 1')
        one(c, """INSERT INTO lead.actions(tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,
            expected_control_version,expected_config_revision,state) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,%s,'reply','{"growth_runtime":"1"}',1,1,'unknown')
            RETURNING id""", (observed.aid, unknown['task_id'], grant['target_id'], grant['id'], uid()))
        before = digest(c.execute('SELECT id,state,input,result FROM lead.tasks WHERE id=ANY(%s::uuid[]) ORDER BY id', (protected,)).fetchall())
        action = digest(c.execute('SELECT * FROM lead.actions').fetchall())
    if entrypoint == 'environment_check':
        response = await explicit_environment_check(observed, monkeypatch)
        response.raise_for_status()
        result = response.json()['read_recovery']
    else:
        proof = await produce(observed)
        with service_scope():
            result = runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])
    assert result['resumed_count'] == 1
    with tx(observed.aid) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'planned'
        assert digest(c.execute('SELECT id,state,input,result FROM lead.tasks WHERE id=ANY(%s::uuid[]) ORDER BY id', (protected,)).fetchall()) == before
        assert digest(c.execute('SELECT * FROM lead.actions').fetchall()) == action


@pytest.mark.parametrize('change', ['metadata', 'stale', 'binding', 'route', 'session', 'config'])
async def test_recovery_requires_fresh_current_exact_proof(observed, monkeypatch, change):
    early = await produce(observed) if change in ('metadata', 'stale') else None
    ref, _, _ = failed_read(observed, 'search')
    proof = early or await produce(observed)
    with tx(observed.aid) as c:
        before = digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],)))
        if change == 'metadata':
            row = one(c, 'SELECT checks FROM lead.identity_checks WHERE id=%s', (proof['proof_ref'].split(':')[1],))
            proof['proof_ref'] = 'identity-check:'+row['checks']['community_read']['device_check_id']
        elif change == 'binding':
            c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s', (observed.account['id'],))
        elif change == 'session':
            c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE id=%s", (observed.actor.session,))
        elif change == 'config':
            c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (observed.aid,))
    if change == 'route':
        from lead_app import execution_routes
        monkeypatch.setattr(execution_routes, 'transports', lambda: {'shared':'http://127.0.0.1:18089'})
    with service_scope():
        if change == 'stale':
            assert runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])['recovered_episodes'] == 0
        else:
            with pytest.raises(ValueError, match='community_read_proof_invalid'):
                runtime.consume_community_read_proof(observed.aid, proof['proof_ref'])
    with tx(observed.aid) as c:
        assert digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))) == before


async def test_original_probe_uses_exact_internal_http_producer_without_replaying_group(observed, monkeypatch):
    ref, root, _ = failed_read(observed, 'search')
    requests = []

    async def actor(*args, **kwargs):
        return observed.actor

    async def rpc(aid, profile_id, path, body=None, **kwargs):
        requests.append((aid, profile_id, path, body))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser.app), base_url='http://fixture') as client:
            response = await client.post('/agents/'+aid+'/'+path, json=body, headers={'Authorization':'Bearer '+internal_token()})
            response.raise_for_status()
            return response.json()

    monkeypatch.setattr(browser.gateway, 'get', actor)
    monkeypatch.setattr(account_connection, 'rpc', rpc)
    data = RecoverInput(episode_root=ref['task_id'], condition_revision=root['result']['runtime']['condition_revision'])
    with service_scope():
        result = await runtime.probe_episode(observed.aid, root, data)
    assert result.output['state'] == 'recovered' and result.output['resumed_count'] == 1
    assert len(requests) == 1 and requests[0][2] == 'account/inspect'
    assert requests[0][3]['community_read_proof'] is True
    assert requests[0][3]['execution_ref'] == observed.actor.execution_ref.model_dump(mode='json')
    with tx(observed.aid) as c:
        episode = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result']['runtime']['recovery']
        assert episode['probes'] == root['result']['runtime']['recovery']['probes'] + 1
        assert episode['probe_deadline'] == root['result']['runtime']['recovery']['probe_deadline']


async def test_http_failure_keeps_only_status_and_fixed_community_operation(observed, monkeypatch):
    ref = search_task(observed)
    claim = runtime.advance(ref)['claim']

    async def failed(*args, **kwargs):
        request = httpx.Request('POST', 'https://private.invalid/secret')
        response = httpx.Response(409, request=request, headers={'authorization':'NEVER_EXPORT'}, text='NEVER_EXPORT')
        response.raise_for_status()

    monkeypatch.setattr(activities, 'browser_rpc', failed)
    with service_scope():
        result = await runtime.execute(claim)
    assert result['failure_code'] == 'HTTPStatusError'
    assert result['output'] == {'http_diagnostic': {'status_code':409, 'operation':'search'}}
    assert 'NEVER_EXPORT' not in json.dumps(result) and 'private.invalid' not in json.dumps(result)


async def explicit_environment_check(env, monkeypatch):
    async def actor(aid, profile_id):
        assert aid == env.aid and str(profile_id) == str(env.actor.profile['id'])
        return env.actor

    monkeypatch.setattr(browser.gateway, 'get', actor)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser.app), base_url='http://fixture') as client:
        return await client.post('/agents/'+env.aid+'/identity/check',
            json={'profile_id': str(env.actor.profile['id'])},
            headers={'Authorization': 'Bearer '+internal_token()})


async def test_explicit_environment_check_consumes_fresh_read_proof_without_resetting_stops(observed, monkeypatch):
    ref, root, predecessor = failed_read(observed)
    source_hash = corrupt_source(observed, predecessor)
    episode = root['result']['runtime']['recovery']
    episode.update(probes=8, probes_stopped=True, paid_probes_stopped=True,
        probe_deadline=(utcnow()-timedelta(minutes=1)).isoformat(), reason='recovery_deadline')
    root['input']['spec']['review_at'] = (utcnow()-timedelta(minutes=1)).isoformat()
    with tx(observed.aid) as c:
        c.execute('UPDATE lead.tasks SET input=%s,result=%s WHERE id=%s',
            (j(root['input']), j(root['result']), ref['task_id']))
        before = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    response = await explicit_environment_check(observed, monkeypatch)
    response.raise_for_status()
    result = response.json()
    assert result['verified'] is True
    assert result['read_recovery']['recovered_episodes'] == 1
    assert result['read_recovery']['resumed_count'] == 0
    assert observed.calls == ['measurement', 'open_self', 'inspect_self']
    with tx(observed.aid) as c:
        after = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert after['state'] == before['state'] == 'failed' and after['input'] == before['input']
        assert after['result']['runtime']['recovery']['status'] == 'recovered'
        for key in ('probes', 'attempts', 'probe_deadline', 'hard_deadline', 'probes_stopped', 'paid_probes_stopped'):
            assert after['result']['runtime']['recovery'][key] == before['result']['runtime']['recovery'][key]
        assert digest(one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))) == source_hash
        wakes = c.execute("SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'").fetchall()
        assert len(wakes) == 1 and wakes[0]['payload']['scope'] == 'community_read_v1'


async def test_explicit_check_recovers_a_read_once_without_touching_another_platform(observed, monkeypatch):
    from lead_app.account_intents import set_intent
    from lead_app.account_profiles import for_platform
    from lead_app.execution_routes import register

    reddit = for_platform(observed.aid, 'reddit', create=True, new_binding=True)
    with tx(observed.aid) as c:
        c.execute("UPDATE lead.social_accounts SET state='connected',external_account_id=%s,automation_enabled=true WHERE id=%s",
            ('fixture-reddit-'+uid(), reddit['account_id']))
    set_intent(observed.aid, reddit['account_id'], 'running', source='owner_command', source_ref='fixture:reddit-owner')
    register(observed.aid, reddit['id'])
    with tx(observed.aid) as c:
        old = submit_work(c, observed.aid, TaskSpec(kind='account', purpose='reddit_venue_and_rule_scan',
            account_id=reddit['account_id'], payload={'desired_operation':'reply'},
            review_at=utcnow()+timedelta(hours=1)))
    for n in range(3):
        claim = runtime.advance(old, now=utcnow()+timedelta(minutes=2*n))['claim']
        runtime.finish(claim, WorkResult(outcome='failed', failure_code='HTTPStatusError'))
    with tx(observed.aid) as c:
        row = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (old['task_id'],))
        row['result']['runtime']['recovery'].update(probes=47, attempts=3, probes_stopped=True, paid_probes_stopped=True)
        c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(row['result']), old['task_id']))
        before = digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (old['task_id'],)))
    ref, root, _ = failed_read(observed, 'search')
    root['result']['runtime']['recovery'].update(probes=8, probes_stopped=True, paid_probes_stopped=True)
    with tx(observed.aid) as c:
        c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(root['result']), ref['task_id']))
    first = await explicit_environment_check(observed, monkeypatch)
    first.raise_for_status()
    assert first.json()['read_recovery']['resumed_count'] == 1
    second = await explicit_environment_check(observed, monkeypatch)
    second.raise_for_status()
    assert 'read_recovery' not in second.json()
    assert observed.calls == ['measurement', 'open_self', 'inspect_self', 'measurement']
    with tx(observed.aid) as c:
        assert digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (old['task_id'],))) == before
        resumed = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert resumed['state'] == 'planned' and resumed['input']['task_generation'] == 2
        for key in ('probes', 'attempts', 'probe_deadline', 'hard_deadline', 'probes_stopped', 'paid_probes_stopped'):
            assert resumed['result']['runtime']['recovery'][key] == root['result']['runtime']['recovery'][key]
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'")['n'] == 1


@pytest.mark.parametrize('case', ['no_episode', 'paused', 'disabled', 'automation_revoked', 'ambiguous_account'])
async def test_ordinary_environment_checks_never_manufacture_read_recovery(observed, monkeypatch, case):
    ref = None
    if case != 'no_episode':
        ref, _, _ = failed_read(observed, 'search')
    with tx(observed.aid) as c:
        if case == 'paused':
            c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (observed.aid,))
        elif case == 'disabled':
            c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{community_growth,enabled}','false') WHERE id=%s", (observed.aid,))
        elif case == 'automation_revoked':
            c.execute('UPDATE lead.social_accounts SET automation_enabled=false WHERE id=%s', (observed.account['id'],))
        elif case == 'ambiguous_account':
            c.execute("""INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled)
                VALUES(lead.ctx_tenant(),%s,%s,'telegram',%s,'connected',true)""",
                (observed.aid, observed.actor.profile['id'], 'other-self-'+uid()))
        before = digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))) if ref else None
    response = await explicit_environment_check(observed, monkeypatch)
    response.raise_for_status()
    assert response.json()['verified'] is True and 'read_recovery' not in response.json()
    assert observed.calls == ['measurement']
    with tx(observed.aid) as c:
        assert one(c, "SELECT count(*) n FROM lead.identity_checks WHERE checks ? 'community_read'")['n'] == 0
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'")['n'] == 0
        if ref:
            assert digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))) == before


@pytest.mark.parametrize('change', ['binding', 'route', 'session', 'config', 'delegation'])
async def test_explicit_check_rejects_a_changed_scope_before_consuming_proof(observed, monkeypatch, change):
    ref, _, _ = failed_read(observed, 'search')
    with tx(observed.aid) as c:
        before = digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],)))

    def mutate():
        if change == 'route':
            from lead_app import execution_routes
            monkeypatch.setattr(execution_routes, 'transports', lambda: {'shared':'http://127.0.0.1:18089'})
            return
        with tx(observed.aid) as c:
            if change == 'binding':
                c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s', (observed.account['id'],))
            elif change == 'session':
                c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE id=%s", (observed.actor.session,))
            elif change == 'config':
                c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (observed.aid,))
            else:
                c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s", (observed.card['id'],))

    observed.mutate = mutate
    response = await explicit_environment_check(observed, monkeypatch)
    assert response.status_code == 409
    assert observed.calls == ['measurement', 'open_self', 'inspect_self']
    with tx(observed.aid) as c:
        assert digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))) == before
        assert one(c, "SELECT count(*) n FROM lead.identity_checks WHERE checks ? 'community_read'")['n'] == 0
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'reason'='resource_recovered'")['n'] == 0


async def test_explicit_check_requires_a_real_verified_device_before_self_read(observed, monkeypatch):
    ref, _, _ = failed_read(observed, 'search')
    observed.device_mode = 'failed'
    with tx(observed.aid) as c:
        before = digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],)))
    response = await explicit_environment_check(observed, monkeypatch)
    assert response.status_code == 409 and observed.calls == ['measurement']
    with tx(observed.aid) as c:
        assert digest(one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))) == before
        assert one(c, "SELECT count(*) n FROM lead.identity_checks WHERE checks ? 'community_read'")['n'] == 0
