"""Observed context keeps original stop scope and current source-bound candidates."""
import json
from datetime import timedelta
from uuid import uuid4

import psycopg
import pytest
from lead_app import activities, admin_config, domain
from lead_app.account_connection import validate_community_source
from lead_app.account_intents import set_intent
from lead_app.account_profiles import for_platform
from lead_app.admin_controls import ChannelRule
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx
from lead_app.growth import agent, runtime
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.store import accept_commands, digest, submit_work
from lead_app.scope import tenant_scope
from test_growth_telegram_community import community  # noqa: F401
from test_local_executor import connected


def group(number=1, **changes):
    return {'group_id': str(-880000-number), 'canonical_url': f'https://t.me/observed_{number}',
        'title': f'Observed community {number}', 'subtitle': f'@observed_{number}, 20 members', **changes}


def search(env, groups, query='a bounded original observation'):
    with tx(env.aid) as c:
        ref = submit_work(c, env.aid, TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
            payload={'community_operation': 'search', 'queries': [query]}, review_at=utcnow()+timedelta(hours=1)))
    runtime.finish(runtime.advance(ref)['claim'], WorkResult(outcome='completed', output={'groups': groups}))
    return ref


def inspect(env, source, peer):
    return TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
        required_refs=[{'task_id': source['task_id'], 'reason': 'Use this exact original public-group observation.'}],
        payload={'community_operation': 'inspect', 'group_task_id': source['task_id'], 'group_id': peer},
        review_at=utcnow()+timedelta(hours=1))


def pad_recent_work(env, count=26):
    for number in range(count):
        with tx(env.aid) as c:
            ref = submit_work(c, env.aid, TaskSpec(kind='research', purpose=f'later-independent-observation-{number}',
                payload={}, review_at=utcnow()+timedelta(hours=1)))
        runtime.finish(runtime.advance(ref)['claim'], WorkResult(outcome='completed'))


def stopped(env, *, shared_profile=False):
    account = env.account
    if not shared_profile:
        binding = for_platform(env.aid, 'telegram', create=True, new_binding=True)
        with tx(env.aid) as c:
            account = one(c, "UPDATE lead.social_accounts SET platform='lab',state='connected',external_account_id='old-source-fixture' WHERE id=%s RETURNING *",
                (binding['account_id'],))
        set_intent(env.aid, account['id'], 'running', source='owner_command', source_ref='fixture:original-source')
    with tx(env.aid) as c:
        ref = submit_work(c, env.aid, TaskSpec(kind='research', purpose='reddit_venue_and_rule_scan',
            account_id=account['id'], payload={'urls': ['https://original.example/venue']},
            review_at=utcnow()+timedelta(hours=1)))
    for number in range(3):
        runtime.finish(runtime.advance(ref, now=utcnow()+timedelta(minutes=number))['claim'],
            WorkResult(outcome='failed', failure_code='source_unavailable'))
    with tx(env.aid) as c:
        c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',
            result#>'{runtime,recovery}' || '{"probes":8,"probes_stopped":true}'::jsonb) WHERE id=%s""", (ref['task_id'],))
    return ref, account


def test_old_stopped_task_keeps_exact_original_scope_and_unknowns(community):  # noqa: F811
    ref, original_account = stopped(community)
    with tx(community.aid) as c:
        c.execute("UPDATE lead.tasks SET input=input-'profile_id' WHERE id=%s", (ref['task_id'],))
    pad_recent_work(community)
    with tx(community.aid) as c:
        before = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result']
        events = one(c, 'SELECT count(*) n FROM lead.outbox_events')['n']
    context = agent.build_context(community.aid)
    assert not any(str(row['id']) == ref['task_id'] for row in context['work'])
    blocked = next(item for item in context['blocked_resources'] if str(item['id']) == ref['task_id'])
    assert blocked['task_ref'] == ref
    assert blocked['task_scope'] == {'kind': 'research', 'purpose': 'reddit_venue_and_rule_scan',
        'account_id': str(original_account['id']), 'profile_id': str(original_account['profile_id']), 'operation': None}
    assert blocked['failure_scope'] is None
    assert blocked['recovery']['probes_stopped'] and blocked['recovery']['attempts'] == 3
    with tx(community.aid) as c:
        assert one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result'] == before
        assert one(c, 'SELECT count(*) n FROM lead.outbox_events')['n'] == events


async def test_projected_candidate_uses_canonical_source_and_disappears_after_real_inspection(community, monkeypatch):  # noqa: F811
    env = community
    old_stop, _ = stopped(env)
    original = search(env, [group(1), group(2), group(3, subtitle='@different_handle, 20 members')])
    search(env, [group(1)], 'another observation of the same peer')
    pad_recent_work(env)
    with tx(env.aid) as c:
        stopped_before = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (old_stop['task_id'],))['result']
        grants_before = digest(c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall())
    context = agent.build_context(env.aid)
    projected = context['community_candidates']
    assert not projected['limited'] and len(projected['items']) == 2
    candidate = next(item for item in projected['items'] if item['group_id'] == group(2)['group_id'])
    assert candidate['source_ref'] == original
    assert candidate['account_id'] == str(env.account['id']) and candidate['profile_id'] == str(env.account['profile_id'])
    assert candidate['binding_version'] == env.account['binding_version']
    assert candidate['inspection_status'] == 'not_yet_inspected'
    assert not {'can_reply', 'membership', 'permissions', 'business_fit', 'ready', 'rules'} & candidate.keys()
    spec = inspect(env, candidate['source_ref'], candidate['group_id'])
    with tx(env.aid) as c:
        canonical = validate_community_source(c, spec)
    assert canonical['canonical_url'] == candidate['canonical_url']
    accepted = accept_commands(env.aid, DecisionProposal(context_key=context['context_key'],
        rationale='Read this exact current original candidate without assuming publication permission.',
        commands=[Command(command_key='inspect-projected', group_key='inspect-projected', kind='task.create',
            goal_ref={'revision': context['goal']['revision']}, account_id=env.account['id'],
            expected_binding=candidate['binding_version'], payload=spec.model_dump(mode='json'))]),
        config_revision=context['config_revision'])
    assert accepted['rejected'] == []
    ref = accepted['accepted'][0]['task_refs'][0]
    claim = runtime.advance(ref)['claim']
    calls = []

    async def rpc(path, payload):
        assert path.endswith('/community/step') and payload['operation'] == 'inspect'
        calls.append(payload['task_id'])
        return {'group': {**group(2), 'kind': 'discussion', 'rules': '', 'rules_hash': digest(''),
            'rules_status': 'unresolved', 'automation_forbidden': False, 'membership': 'not_joined',
            'can_reply': False, 'messages': [], 'observed_at': utcnow().isoformat()},
            'source_hash': canonical['source_hash'], 'execution_ref': env.actor.execution_ref.model_dump(mode='json')}

    monkeypatch.setattr(activities, 'browser_rpc', rpc)
    observed = await runtime.execute(claim)
    assert observed['outcome'] == 'completed', observed
    assert runtime.finish(claim, observed)['accepted_control']
    assert calls == [ref['task_id']]
    after = agent.build_context(env.aid)['community_candidates']['items']
    assert [item['group_id'] for item in after] == [group(1)['group_id']]
    with tx(env.aid) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'succeeded'
        assert one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (old_stop['task_id'],))['result'] == stopped_before
        assert digest(c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()) == grants_before


@pytest.mark.parametrize('state', ['planned', 'running', 'succeeded', 'failed', 'canceled', 'waiting_user', 'reconciling'])
def test_inspection_history_outside_recent_work_prevents_reoffering_the_peer(community, state):  # noqa: F811
    env = community
    original = search(env, [group(1)])
    with tx(env.aid) as c:
        ref = submit_work(c, env.aid, inspect(env, original, group(1)['group_id']))
        c.execute('UPDATE lead.tasks SET state=%s WHERE id=%s', (state, ref['task_id']))
    pad_recent_work(env)
    search(env, [group(1)], 'later duplicate cannot revive inspection')
    context = agent.build_context(env.aid)
    assert not any(str(row['id']) == ref['task_id'] for row in context['work'])
    assert context['community_candidates']['items'] == []


@pytest.mark.parametrize('change', ['binding', 'profile', 'failed_source', 'invalid_url', 'duplicate_source_peer',
    'excluded_peer', 'excluded_url', 'account_paused', 'project_paused', 'automation_disabled', 'community_disabled'])
def test_invalid_or_currently_excluded_sources_are_not_projected(community, change):  # noqa: F811
    env = community
    original = search(env, [group(1)])
    assert len(agent.build_context(env.aid)['community_candidates']['items']) == 1
    with tx(env.aid) as c:
        if change in ('binding', 'profile'):
            key, value = ('binding_version', 999) if change == 'binding' else ('profile_id', str(uuid4()))
            c.execute('UPDATE lead.tasks SET input=jsonb_set(input,%s,%s) WHERE id=%s',
                ([key], j(value), original['task_id']))
        elif change == 'failed_source':
            c.execute("UPDATE lead.tasks SET state='failed' WHERE id=%s", (original['task_id'],))
        elif change in ('invalid_url', 'duplicate_source_peer'):
            values = [group(1, subtitle='@different_handle')] if change == 'invalid_url' else [group(1), group(1)]
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{output,output,groups}',%s) WHERE id=%s",
                (j(values), original['task_id']))
        elif change == 'automation_disabled':
            c.execute('UPDATE lead.social_accounts SET automation_enabled=false WHERE id=%s', (env.account['id'],))
        elif change == 'community_disabled':
            key = 'channel-rules/telegram/'+env.aid
            admin_config.write(c, key, ChannelRule(allow_community_growth=False).model_dump(),
                admin_config.read(c, key)['version'], action='publish')
    if change.startswith('excluded_') or change == 'account_paused':
        set_intent(env.aid, env.account['id'], 'paused' if change == 'account_paused' else 'running',
            source='owner_command', source_ref='fixture:current-owner-control',
            scope={'excluded_targets': [group(1)['group_id'] if change == 'excluded_peer' else group(1)['canonical_url']]})
    elif change == 'project_paused':
        domain.set_control(env.aid, 'pause')
    assert agent.build_context(env.aid)['community_candidates']['items'] == []


@pytest.mark.parametrize('foreign_tenant', [False, True], ids=['other-agent', 'other-tenant'])
def test_foreign_source_cannot_supply_a_candidate_for_the_current_account(community, foreign_tenant):  # noqa: F811
    env = community
    original = search(env, [group(1)])
    with tx(env.aid) as c:
        source = one(c, 'SELECT input,result FROM lead.tasks WHERE id=%s', (original['task_id'],))
    if foreign_tenant:
        tenant = str(uuid4())
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute("INSERT INTO lead.tenants(id,name) VALUES(%s,'Context-source isolation fixture')", (tenant,))
    else:
        from lead_app.scope import current_tenant
        tenant = current_tenant()
    with tenant_scope(tenant):
        other = domain.create_agent(runtime_engine='growth')
        try:
            with tx(other) as c:
                foreign = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
                    VALUES(lead.ctx_tenant(),%s,'growth.account','succeeded',%s,%s,%s,now()) RETURNING id""",
                    (other, 'foreign-context-source:'+str(uuid4()), j(source['input']),
                     j({**source['result'], 'output': {'outcome': 'completed', 'output': {'groups': [group(2)]}}})))
        finally:
            domain.set_control(other, 'archive')
    context = agent.build_context(env.aid)
    assert [item['group_id'] for item in context['community_candidates']['items']] == [group(1)['group_id']]
    assert not any(item['source_ref']['task_id'] == str(foreign['id']) for item in context['community_candidates']['items'])


def test_candidate_metadata_is_bounded_without_claiming_complete_coverage(community):  # noqa: F811
    for batch in range(3):
        search(community, [group(batch*12+number+1, title='观测标题'*100, subtitle=f'@observed_{batch*12+number+1}, '+ '说明'*300)
            for number in range(12)], query=f'bounded observation batch {batch}')
    projected = agent.build_context(community.aid)['community_candidates']
    assert len(projected['items']) == 24 and projected['limited']
    assert all(len(item['title'].encode()) <= 240 and len(item['subtitle'].encode()) <= 240 for item in projected['items'])


def test_observed_candidate_does_not_bypass_a_real_same_profile_stop(community):  # noqa: F811
    env = community
    source = search(env, [group(1)])
    blocked, _ = stopped(env, shared_profile=True)
    projected = agent.build_context(env.aid)['community_candidates']['items'][0]
    assert projected['source_ref'] == source
    with tx(env.aid) as c:
        before = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (blocked['task_id'],))['result']
        ref = submit_work(c, env.aid, inspect(env, projected['source_ref'], projected['group_id']))
    admitted = runtime.advance(ref)
    assert admitted['state'] == 'wait' and admitted['wait']['reason'] == 'resource_blocked'
    with tx(env.aid) as c:
        recovery = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (blocked['task_id'],))['result']['runtime']['recovery']
        assert all(recovery[key] == before['runtime']['recovery'][key]
            for key in ('status', 'probes', 'attempts', 'probes_stopped', 'condition_revision'))


def test_a_new_peer_or_source_task_cannot_reoffer_an_already_inspected_url(community):  # noqa: F811
    env = community
    original = search(env, [group(1), group(2, canonical_url=group(1)['canonical_url'], subtitle=group(1)['subtitle'])])
    assert len(agent.build_context(env.aid)['community_candidates']['items']) == 1
    with tx(env.aid) as c:
        ref = submit_work(c, env.aid, inspect(env, original, group(1)['group_id']))
    runtime.finish(runtime.advance(ref)['claim'], WorkResult(outcome='failed', failure_code='community_source_binding_invalid'))
    search(env, [group(3, canonical_url=group(1)['canonical_url'], subtitle=group(1)['subtitle'])], 'a new source of an already inspected URL')
    assert agent.build_context(env.aid)['community_candidates']['items'] == []


@pytest.mark.parametrize('native', [False, True], ids=['bounded-model-context', 'native-context'])
def test_candidate_metadata_obeys_the_existing_context_compaction_policy(community, monkeypatch, native):  # noqa: F811
    if native:
        connected(community.aid)
    for batch in range(2):
        search(community, [group(batch*12+number+1, title='观测标题'*100,
            subtitle=f'@observed_{batch*12+number+1}, '+ '说明'*300)
            for number in range(12)], query=f'context-limit batch {batch}')
    monkeypatch.setattr(agent, 'MAX_CONTEXT_BYTES', 24000)
    context = agent.build_context(community.aid)
    size = len(json.dumps(context, default=str, ensure_ascii=False).encode())
    if native:
        assert len(context['community_candidates']['items']) == 24
        assert not context['community_candidates']['limited']
        assert size > agent.MAX_CONTEXT_BYTES
    else:
        assert 0 < len(context['community_candidates']['items']) < 24
        assert context['community_candidates']['limited'] and context['history_truncated']
        assert size <= agent.MAX_CONTEXT_BYTES
