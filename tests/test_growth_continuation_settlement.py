"""A frozen instruction and its real Task settlement cross one decision boundary."""
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app import domain
from lead_app.db import emit, j, one, tx
from lead_app.growth import agent, tools
from lead_app.growth.contracts import DecisionProposal, TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import (
    advance,
    consume_triggers,
    ensure_decision,
    finish,
    trigger_key,
)
from lead_app.growth.store import accept_commands, submit_work
from test_growth_runtime import new_work
from test_growth_telegram_community import community  # noqa: F401


def test_configuration_goal_and_direction_are_one_read_snapshot(growth_agent, monkeypatch):
    aid = growth_agent
    original = agent.one
    injected = False
    direction = {'text': 'Find another permitted audience and complete actual work.',
        'source_ref': {'kind': 'message', 'id': str(uuid4())}, 'command_id': str(uuid4()), 'config_revision': 2}

    def interleaved(c, sql, params=()):
        nonlocal injected
        if not injected and 'SELECT config_revision,' in sql:
            injected = True
            with tx(aid) as writer:
                domain.snapshot_preferences(writer, aid)
                writer.execute('UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s',
                    (j({'operating_direction': direction, 'operating_contract': {'revision': 2, 'objective': 'The current owner goal.'}}), aid))
        return original(c, sql, params)

    monkeypatch.setattr(agent, 'one', interleaved)
    context = agent.build_context(aid)
    assert injected and context['config_revision'] == 2
    assert context['goal']['revision'] == 2
    assert context['goal']['objective'] == 'The current owner goal.'
    assert context['owner_direction'] == direction
    value = DecisionProposal(context_key=context['context_key'], rationale='Use the one real snapshot.', commands=[],
        continuation={'condition': 'New original evidence arrives.', 'producer': 'evidence_added',
                      'next_action': 'Assess the actual evidence.'})
    assert not accept_commands(aid, value, config_revision=context['config_revision']).get('discarded')


def observe(aid, dedupe=''):
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        return submit_work(c, aid, TaskSpec(kind='observe', purpose='outcomes', payload={},
            dedupe_key=dedupe, review_at=utcnow()+timedelta(hours=1)))


async def judgment(aid, ref, monkeypatch):
    trigger = {'reason': 'original_review', 'revision': 1}
    decision = ensure_decision(aid, [trigger])
    claim = advance(decision)['claim']

    async def model(schema, context, **kwargs):
        source = next(source for source in context['continuation_sources'] if source['ref']['id'] == ref['task_id'])
        return DecisionProposal(context_key=context['context_key'], rationale='Await the original readback.', commands=[],
            continuation={'condition': 'The original outcome read finishes.', 'source_ref': source['ref'],
                'producer': source['producer'], 'responsibility': source['responsibility'],
                'next_action': 'Use the real result to select the next actual task.'})

    monkeypatch.setattr(agent, 'run_model', model)
    value = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    assert value['status'] == 'waiting_for_change' and value['continuation_check']['valid']
    return claim, value, trigger


async def complete_observe(aid, ref):
    claim = advance(ref)['claim']
    with tx(aid) as c:
        row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    value = await tools.execute_tool(aid, row, claim)
    assert value.outcome == 'completed'
    assert not value.output['business_progress'] and not value.output['knowledge_progress']
    assert finish(claim, value)['accepted_control']
    return claim, value


def settlement_facts(aid):
    with tx(aid) as c:
        return [row['payload'] for row in c.execute("""SELECT payload FROM lead.outbox_events
            WHERE event_type='growth.wake' AND payload->>'reason'='work_settled' ORDER BY occurred_at,id""").fetchall()]


@pytest.mark.parametrize('source_first', [False, True])
async def test_bound_zero_change_readback_settles_once_in_both_finish_orders(growth_agent, monkeypatch, source_first):
    aid = growth_agent
    ref = observe(aid)
    decision_claim, value, trigger = await judgment(aid, ref, monkeypatch)
    if source_first:
        source_claim, result = await complete_observe(aid, ref)
    assert finish(decision_claim, WorkResult(outcome='completed', output=value))['accepted_control']
    if not source_first:
        source_claim, result = await complete_observe(aid, ref)
    facts = settlement_facts(aid)
    assert len(facts) == 1
    assert facts[0] == {'reason': 'work_settled', 'task_id': ref['task_id'], 'task_generation': 1,
                        'config_revision': value['config_revision'], 'goal_revision': value['goal_revision']}
    assert not finish(source_claim, result)['accepted_control']
    assert not finish(decision_claim, WorkResult(outcome='completed', output=value))['accepted_control']
    assert settlement_facts(aid) == facts
    consume_triggers(aid, [trigger])
    next_work = ensure_decision(aid, facts)
    assert next_work is not None and next_work['task_id'] != decision_claim['ref']['task_id']
    assert ensure_decision(aid, facts) == next_work


@pytest.mark.parametrize('changed', ['configuration', 'goal', 'generation'])
async def test_settlement_rechecks_the_exact_frozen_source_context(growth_agent, monkeypatch, changed):
    aid = growth_agent
    ref = observe(aid)
    decision_claim, value, _ = await judgment(aid, ref, monkeypatch)
    finish(decision_claim, WorkResult(outcome='completed', output=value))
    with tx(aid) as c:
        if changed == 'configuration':
            domain.snapshot_preferences(c, aid)
        elif changed == 'goal':
            c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract}','{\"revision\":2}') WHERE id=%s", (aid,))
        else:
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{task_generation}','2') WHERE id=%s", (ref['task_id'],))
            ref = {**ref, 'task_generation': 2}
    await complete_observe(aid, ref)
    assert settlement_facts(aid) == []


async def test_unbound_completion_is_not_a_new_settlement_trigger(growth_agent):
    ref = observe(growth_agent)
    await complete_observe(growth_agent, ref)
    assert settlement_facts(growth_agent) == []


def test_search_with_an_existing_scope_observation_is_not_a_promised_producer(community):  # noqa: F811
    aid = community.aid
    refs = []
    for query in ('privacy tools', 'private conversations'):
        with tx(aid) as c:
            refs.append(submit_work(c, aid, TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
                payload={'community_operation': 'search', 'queries': [query]}, review_at=utcnow()+timedelta(hours=1))))
        if len(refs) == 1:
            finish(advance(refs[0])['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    assert refs[0]['task_id'] != refs[1]['task_id']
    assert not any(source['ref']['id'] == refs[1]['task_id'] for source in agent.build_context(aid)['continuation_sources'])


def test_renaming_an_observation_does_not_create_a_second_settlement(growth_agent):
    first = observe(growth_agent, 'original')
    finish(advance(first)['claim'], WorkResult(outcome='completed', output={'business_progress': False}))
    with tx(growth_agent) as c:
        renamed = submit_work(c, growth_agent, TaskSpec(kind='observe', purpose='renamed-outcomes', payload={},
            dedupe_key='renamed', review_at=utcnow()+timedelta(hours=2)))
    assert renamed == first


@pytest.mark.parametrize('fact', ['analytics.cleaned', 'experiment_review_due'])
def test_a_real_observation_or_original_review_epoch_allows_revisit(growth_agent, fact):
    first = observe(growth_agent)
    with tx(growth_agent) as c:
        if fact == 'analytics.cleaned':
            emit(c, growth_agent, fact, {'scope_key': 'live', 'ingest_id': str(uuid4())})
        else:
            emit(c, growth_agent, 'growth.wake', {'reason': fact, 'experiment_id': str(uuid4()), 'revision': 1})
    assert observe(growth_agent)['task_id'] != first['task_id']


def test_settlement_trigger_identity_keeps_task_generation():
    fact = {'reason': 'work_settled', 'task_id': str(uuid4()), 'task_generation': 1}
    assert trigger_key(fact) != trigger_key({**fact, 'task_generation': 2})


async def test_another_task_completion_does_not_settle_the_selected_source(growth_agent, monkeypatch):
    ref = observe(growth_agent)
    claim, value, _ = await judgment(growth_agent, ref, monkeypatch)
    finish(claim, WorkResult(outcome='completed', output=value))
    other = new_work(growth_agent)
    finish(advance(other)['claim'], WorkResult(outcome='completed'))
    assert settlement_facts(growth_agent) == []


async def test_bound_duplicate_empty_search_does_not_add_a_settlement_loop(community, monkeypatch):  # noqa: F811
    aid = community.aid
    with tx(aid) as c:
        refs = [submit_work(c, aid, TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
            payload={'community_operation': 'search', 'queries': [query]}, review_at=utcnow()+timedelta(hours=1)))
            for query in ('privacy tools', 'private conversations')]
    claim, value, _ = await judgment(aid, refs[1], monkeypatch)
    finish(claim, WorkResult(outcome='completed', output=value))
    for ref in refs:
        finish(advance(ref)['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    assert settlement_facts(aid) == []
    with tx(aid) as c:
        assert one(c, """SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='evidence_added' AND payload->>'task_id'=ANY(%s::text[])""",
            ([ref['task_id'] for ref in refs],))['n'] == 1
