"""A source-specific failure cannot silently become a same-name family ban."""
from copy import deepcopy
from datetime import timedelta

import pytest
from lead_app.db import j, one, tx
from lead_app.growth import agent, runtime
from lead_app.growth.contracts import ExperimentSpec, WorkResult, utcnow
from lead_app.growth.store import create_experiment, submit_work
from lead_app.scope import service_scope
from test_community_read_proof import observed as observed
from test_community_read_proof import produce
from test_community_read_recovery import failed_read
from test_growth_context_scope import group, inspect, search
from test_growth_telegram_community import community as community
from test_local_executor import connected


@pytest.mark.parametrize('native', [False, True], ids=['bounded-context', 'native-context'])
async def test_recovered_bad_source_is_distinct_from_an_uninspected_same_name_peer(observed, native):
    env = observed
    original_ref, root, predecessor = failed_read(env)
    old_peer = root['input']['spec']['payload']['group_id']
    with tx(env.aid) as c:
        source = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))['result']
        original = source['output']['output']['groups'][0]
        original.update(title='Shared privacy family', subtitle='@different_fixture, 20 members')
        old_url = original['canonical_url']
        c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(source), predecessor['task_id']))
    proof = await produce(env)
    with service_scope():
        assert runtime.consume_community_read_proof(env.aid, proof['proof_ref'])['recovered_episodes'] == 1
    other = group(2, title='Shared privacy family')
    latest = search(env, [other,
        group(3, group_id=old_peer, title='Shared privacy family'),
        group(4, canonical_url=old_url, subtitle='@privacy_fixture, 20 members', title='Shared privacy family'),
        group(5, group_id='-99999', title='Shared privacy family')], query='independent current source observation')
    if native:
        connected(env.aid)
    with tx(env.aid) as c:
        before = one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (original_ref['task_id'],))
        event_count = one(c, 'SELECT count(*) n FROM lead.outbox_events')['n']
        grants = deepcopy(c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall())
    context = agent.build_context(env.aid)
    historical = next(row for row in context['work'] if str(row['id']) == original_ref['task_id'])
    scope = historical['community_inspection_scope']
    assert scope['task_ref'] == original_ref
    assert scope['account_id'] == str(env.account['id'])
    assert scope['profile_id'] == str(env.account['profile_id'])
    assert scope['binding_version'] == env.account['binding_version']
    assert scope['group_id'] == old_peer and scope['source_task_id'] == predecessor['task_id']
    assert scope['source_binding'] == {'state':'invalid', 'reason':'community_source_binding_invalid'}
    assert scope['recorded_recovery']['status'] == 'recovered'
    assert scope['recorded_recovery']['proof_scope'] == 'community_read_v1'
    assert scope['recorded_recovery']['attempts'] == 3
    assert scope['recorded_recovery']['probes'] == before['result']['runtime']['recovery']['probes']
    assert not any(str(row['id']) == original_ref['task_id'] for row in context['blocked_resources'])
    assert [row['group_id'] for row in context['community_candidates']['items']] == [other['group_id']]
    candidate = context['community_candidates']['items'][0]
    assert candidate['source_ref'] == latest
    assert candidate['canonical_url'] != old_url and candidate['group_id'] != old_peer
    with tx(env.aid) as c:
        from lead_app.account_connection import validate_community_source
        assert validate_community_source(c, inspect(env, latest, other['group_id']))['canonical_url'] == other['canonical_url']
        assert one(c, 'SELECT state,input,result FROM lead.tasks WHERE id=%s', (original_ref['task_id'],)) == before
        assert one(c, 'SELECT count(*) n FROM lead.outbox_events')['n'] == event_count
        assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall() == grants


def test_bounded_experiment_keeps_its_audience_and_stop_rule(growth_agent):
    from test_growth_runtime import new_work
    new_work(growth_agent)
    with tx(growth_agent) as c:
        original = create_experiment(c, growth_agent, ExperimentSpec(question_type='distribution',
            question='Does this directory accept a fitting product?', hypothesis='The directory may have a suitable route.',
            learning_signal='The directory and its own rules', token_limit=1000, review_at=utcnow()+timedelta(hours=1)))
        spec = {**original['specification'], 'audience':'Readers of this one directory',
            'stop_rule':'Read this directory and at most two linked rule pages; do not contact or submit.'}
        c.execute('UPDATE lead.growth_experiments SET specification=%s WHERE id=%s', (j(spec), original['id']))
    context = agent.build_context(growth_agent)
    projected = next(row for row in context['experiments'] if row['id'] == original['id'])['specification']
    assert projected['audience'] == spec['audience']
    assert projected['stop_rule'] == spec['stop_rule']
    with tx(growth_agent) as c:
        assert one(c, 'SELECT specification FROM lead.growth_experiments WHERE id=%s', (original['id'],))['specification'] == spec


@pytest.mark.parametrize('change', ['missing_binding', 'missing_refs', 'unsupported_type'])
def test_historical_scope_preserves_unknowns_and_distinguishes_unsupported_type(community, change):
    env = community
    predecessor = search(env, [group(1)])
    with tx(env.aid) as c:
        ref = submit_work(c, env.aid, inspect(env, predecessor, group(1)['group_id']))
    runtime.finish(runtime.advance(ref)['claim'], WorkResult(outcome='failed', failure_code='community_source_binding_invalid'))
    with tx(env.aid) as c:
        old = one(c, 'SELECT input FROM lead.tasks WHERE id=%s', (ref['task_id'],))['input']
        if change == 'missing_binding':
            old.pop('binding_version')
        elif change == 'missing_refs':
            old['spec']['required_refs'] = None
        else:
            source = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (predecessor['task_id'],))['result']
            source['output']['output']['groups'][0]['kind'] = 'broadcast'
            c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(source), predecessor['task_id']))
        c.execute('UPDATE lead.tasks SET input=%s WHERE id=%s', (j(old), ref['task_id']))
    context = agent.build_context(env.aid)
    scope = next(row for row in context['work'] if str(row['id']) == ref['task_id'])['community_inspection_scope']
    assert scope['source_binding'] == ({'state':'unsupported', 'reason':'community_discussion_binding_required'}
        if change == 'unsupported_type' else {'state':'unknown'})
    if change == 'missing_binding':
        assert scope['binding_version'] is None


@pytest.mark.parametrize('foreign_tenant', [False, True], ids=['other-agent', 'other-tenant'])
def test_foreign_failed_inspection_is_not_added_to_current_history(community, foreign_tenant):
    from uuid import uuid4

    import psycopg
    from lead_app import domain
    from lead_app.config import database_kwargs
    from lead_app.scope import current_tenant, tenant_scope

    env = community
    predecessor = search(env, [group(1)])
    tenant = str(uuid4()) if foreign_tenant else current_tenant()
    if foreign_tenant:
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute("INSERT INTO lead.tenants(id,name) VALUES(%s,'Failed-inspection scope fixture')", (tenant,))
    with tenant_scope(tenant):
        other = domain.create_agent(runtime_engine='growth')
        try:
            with tx(other) as c:
                foreign = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
                    VALUES(lead.ctx_tenant(),%s,'growth.account','failed',%s,%s,'{}',now()) RETURNING id""",
                    (other, 'foreign-inspection:'+str(uuid4()), j({'spec':inspect(env, predecessor, group(1)['group_id']).model_dump(mode='json'),
                        'task_generation':1, 'profile_id':str(env.account['profile_id']), 'binding_version':env.account['binding_version']})))
        finally:
            domain.set_control(other, 'archive')
    context = agent.build_context(env.aid)
    assert not any(row['id'] == foreign['id'] for row in context['work'])
    assert [row['group_id'] for row in context['community_candidates']['items']] == [group(1)['group_id']]
