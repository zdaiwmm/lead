"""A no-work judgment must identify its real future producer or honest blocker."""
from datetime import datetime, timedelta
from uuid import uuid4

from lead_app import domain, local_executor
from lead_app.db import j, one, tx
from lead_app.growth import agent
from lead_app.growth.contracts import (
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.runtime import advance, ensure_decision, finish, recover_due
from lead_app.growth.store import accept_commands, create_experiment, submit_work
from lead_app.growth.tools import prepare_channel, submission_gate
from lead_app.growth.views import render_growth
from test_growth_channel_operation import channel_work
from test_growth_runtime import new_work
from test_growth_telegram_community import community  # noqa: F401

STOP = {'condition': 'A new relevant original source becomes available.', 'producer': 'evidence_added',
        'responsibility': 'product', 'next_action': 'Inspect the original source and select actual work.'}


def proposal(context, continuation=None):
    return DecisionProposal(context_key=context['context_key'], rationale='Use the original scoped condition.',
                            commands=[], continuation=continuation or STOP)


async def test_no_source_is_not_a_healthy_wait_even_after_bounded_review(growth_agent, monkeypatch):
    calls = []
    monkeypatch.setattr(local_executor, 'enabled', lambda *a, **k: True)

    async def model(schema, context, **kwargs):
        calls.append(kwargs['call_key'])
        return proposal(context)

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent)
    assert calls == ['primary', 'repair']
    assert result['status'] == 'needs_follow_up'
    assert result['continuation_check']['valid'] is False
    assert result['continuation_check']['reason'] == 'continuation_source_required'
    with tx(growth_agent) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.account'")['n'] == 0


async def test_existing_experiment_is_a_valid_future_review_without_new_traffic(growth_agent, monkeypatch):
    deadline = utcnow() + timedelta(minutes=20)
    with tx(growth_agent) as c:
        exp = create_experiment(c, growth_agent, ExperimentSpec(question_type='distribution',
            question='Does the original placement expose demand?', hypothesis='The original audience may respond.',
            learning_signal='A real attributed response or an inconclusive original window.', token_limit=1000,
            review_at=deadline))
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(kwargs['call_key'])
        source = next(s for s in context['continuation_sources'] if s['ref']['id'] == str(exp['id']))
        return proposal(context, {**STOP, 'producer': source['producer'], 'source_ref': source['ref']})

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent)
    assert calls == ['primary']
    assert result['status'] == 'waiting_for_change'
    assert result['continuation_check']['valid'] is True
    assert datetime.fromisoformat(result['continuation_check']['source']['check_at']) == deadline
    recover_due(growth_agent, now=deadline+timedelta(seconds=1))
    with tx(growth_agent) as c:
        assert one(c, "SELECT state FROM lead.growth_experiments WHERE id=%s", (exp['id'],))['state'] == 'closed'
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE payload->>'reason'='experiment_review_due'")['n'] == 1


def test_foreign_or_expired_source_cannot_be_used_as_current_wait(growth_agent):
    context = agent.build_context(growth_agent)
    continuation = {**STOP, 'source_ref': {'kind': 'task', 'id': str(uuid4()), 'version': '1'}}
    result = accept_commands(growth_agent, proposal(context, continuation))
    assert result['continuation_check']['valid'] is False
    assert result['continuation_check']['reason'] == 'continuation_source_changed'


def test_task_generation_is_rechecked_at_admission(growth_agent):
    ref = new_work(growth_agent)
    context = agent.build_context(growth_agent)
    source = next(s for s in context['continuation_sources'] if s['ref']['id'] == ref['task_id'])
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{task_generation}','2') WHERE id=%s", (ref['task_id'],))
    value = proposal(context, {**STOP, 'source_ref': source['ref']})
    assert accept_commands(growth_agent, value)['continuation_check']['valid'] is False


async def test_stopped_resource_is_an_honest_blocker_not_automatic_wait(growth_agent, monkeypatch):
    ref = new_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='failed',completed_at=now(),result=%s WHERE id=%s",
            (j({'runtime': {'recovery': {'status': 'blocked', 'condition_revision': 'original-source',
                'resource': 'source:original', 'probes_stopped': True,
                'next_check_at': (utcnow()-timedelta(hours=1)).isoformat()}}}), ref['task_id']))

    async def model(schema, context, **kwargs):
        source = next(s for s in context['continuation_sources'] if s['ref']['kind'] == 'resource')
        return proposal(context, {**STOP, 'producer': source['producer'], 'responsibility': 'resource',
                                  'source_ref': source['ref']})

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent)
    assert result['status'] == 'blocked'
    assert result['continuation_check']['status'] == 'blocked'
    assert result['continuation_check']['source']['check_at'] is None


async def test_scoped_blocker_does_not_skip_review_of_declared_independent_discovery(growth_agent, monkeypatch):
    ref = new_work(growth_agent)
    monkeypatch.setattr(local_executor, 'enabled', lambda *a, **k: True)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='failed',completed_at=now(),result=%s WHERE id=%s",
            (j({'runtime': {'recovery': {'status': 'blocked', 'condition_revision': 'original-source',
                'probes_stopped': True}}}), ref['task_id']))
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(kwargs['call_key'])
        source = next(s for s in context['continuation_sources'] if s['ref']['kind'] == 'resource')
        return proposal(context, {**STOP, 'producer': source['producer'], 'responsibility': 'resource',
                                  'source_ref': source['ref']})

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent)
    assert calls == ['primary', 'repair']
    assert result['status'] == 'blocked'


async def test_real_configuration_change_discards_old_frozen_proposal(growth_agent, monkeypatch):
    async def model(schema, context, **kwargs):
        with tx(growth_agent) as c:
            domain.snapshot_preferences(c, growth_agent)
        return proposal(context)

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent)
    assert result['discarded'] == 'configuration_changed'
    assert result['accepted'] == []


def test_owner_direction_is_current_scoped_context(growth_agent):
    direction = {'text': '先寻找允许分享入口的陌生受众，再真实执行。',
                 'source_ref': {'kind': 'message', 'id': str(uuid4())},
                 'command_id': str(uuid4()), 'config_revision': 1}
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                  (j({'operating_direction': direction}), growth_agent))
    assert agent.build_context(growth_agent)['owner_direction'] == direction


def test_same_search_does_not_become_new_work_by_changing_dedupe(community):  # noqa: F811
    aid = community.aid
    spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        payload={'community_operation': 'search', 'queries': ['privacy tools']},
        review_at=utcnow()+timedelta(hours=1), dedupe_key='first')
    with tx(aid) as c:
        first = submit_work(c, aid, spec)
        second = submit_work(c, aid, spec.model_copy(update={'dedupe_key': 'renamed',
            'review_at': utcnow()+timedelta(hours=2)}))
    assert first == second


def test_real_new_configuration_allows_bounded_search_revisit(community):  # noqa: F811
    aid = community.aid
    spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        payload={'community_operation': 'search', 'queries': ['privacy tools']},
        review_at=utcnow()+timedelta(hours=1))
    with tx(aid) as c:
        first = submit_work(c, aid, spec)
        domain.snapshot_preferences(c, aid)
        second = submit_work(c, aid, spec)
    assert first['task_id'] != second['task_id']


def test_duplicate_empty_search_observation_wakes_only_once_per_real_scope(community):  # noqa: F811
    aid = community.aid
    spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        payload={'community_operation': 'search', 'queries': ['privacy tools']},
        review_at=utcnow()+timedelta(hours=1))
    with tx(aid) as c:
        first = submit_work(c, aid, spec)
    finish(advance(first)['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    with tx(aid) as c:
        original = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (first['task_id'],))
        duplicate = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
            VALUES(lead.ctx_tenant(),%s,'growth.account','planned',%s,%s,%s,now()) RETURNING id,input""",
            (aid, 'fixture-search:'+str(uuid4()), j(original['input']), j({'runtime': {}, 'output': {}})))
    second = {'agent_id': aid, 'task_id': str(duplicate['id']), 'task_generation': 1}
    finish(advance(second)['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    with tx(aid) as c:
        assert one(c, """SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='evidence_added' AND payload->>'task_id'=ANY(%s::text[])""",
                   ([first['task_id'], second['task_id']],))['n'] == 1


def test_changed_query_can_run_but_same_empty_result_is_not_new_evidence(community):  # noqa: F811
    aid = community.aid
    refs = []
    for query in ('privacy tools', 'private conversations'):
        with tx(aid) as c:
            refs.append(submit_work(c, aid, TaskSpec(kind='account', purpose='community',
                account_id=community.account['id'], payload={'community_operation': 'search', 'queries': [query]},
                review_at=utcnow()+timedelta(hours=1))))
        finish(advance(refs[-1])['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    assert refs[0]['task_id'] != refs[1]['task_id']
    with tx(aid) as c:
        assert one(c, """SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='evidence_added' AND payload->>'task_id'=ANY(%s::text[])""",
                   ([r['task_id'] for r in refs],))['n'] == 1


def test_original_review_fact_allows_same_query_in_a_new_observation_window(community):  # noqa: F811
    aid = community.aid
    deadline = utcnow()+timedelta(minutes=1)
    spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        payload={'community_operation': 'search', 'queries': ['privacy tools']}, review_at=deadline+timedelta(hours=1))
    with tx(aid) as c:
        first = submit_work(c, aid, spec)
        create_experiment(c, aid, ExperimentSpec(question_type='distribution', question='Where is a relevant venue?',
            hypothesis='A relevant audience may be reachable.', learning_signal='A source or an inconclusive window.',
            token_limit=1000, review_at=deadline))
    finish(advance(first)['claim'], WorkResult(outcome='completed', output={'groups': [], 'knowledge_progress': False}))
    recover_due(aid, now=deadline+timedelta(seconds=1))
    with tx(aid) as c:
        second = submit_work(c, aid, spec)
    assert first['task_id'] != second['task_id']


async def test_read_projection_rechecks_terminal_source_without_dispatch(growth_agent, monkeypatch):
    original = new_work(growth_agent)
    decision = ensure_decision(growth_agent, [{'reason': 'product_changed', 'revision': 1}])
    claim = advance(decision)['claim']

    async def model(schema, context, **kwargs):
        source = next(s for s in context['continuation_sources'] if s['ref']['id'] == original['task_id'])
        return proposal(context, {**STOP, 'source_ref': source['ref']})

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(growth_agent, decision['task_id'], decision_claim=claim)
    finish(claim, WorkResult(outcome='completed', output=result))
    assert render_growth(growth_agent)['continuation']['check']['valid']
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s", (original['task_id'],))
        before = one(c, 'SELECT count(*) n FROM lead.outbox_events')['n']
    view = render_growth(growth_agent)
    assert not view['continuation']['check']['valid']
    assert view['continuation']['status'] == 'needs_follow_up'
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.outbox_events')['n'] == before


def test_queued_work_cannot_start_under_an_old_operating_revision(growth_agent):
    ref, _, _ = channel_work(growth_agent)
    with tx(growth_agent) as c:
        domain.snapshot_preferences(c, growth_agent)
    assert not any(source['ref']['id'] == ref['task_id']
                   for source in agent.build_context(growth_agent)['continuation_sources'])
    result = advance(ref)
    assert result['state'] == 'terminal' and result['reason'] == 'configuration_changed'
    with tx(growth_agent) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'canceled'
        spec = TaskSpec.model_validate(one(c, 'SELECT input FROM lead.tasks WHERE id=%s', (ref['task_id'],))['input']['spec'])
        current = submit_work(c, growth_agent, spec)
    assert current['task_id'] != ref['task_id']
    assert advance(current)['state'] == 'ready'


def test_old_goal_work_is_not_a_new_continuation_source(growth_agent):
    ref, _, _ = channel_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,
            '{operating_contract}','{\"revision\":2}') WHERE id=%s""", (growth_agent,))
    context = agent.build_context(growth_agent)
    assert context['goal']['revision'] == 2
    assert not any(source['ref']['id'] == ref['task_id'] for source in context['continuation_sources'])


def test_prepared_action_is_fenced_by_operating_revision(growth_agent):
    from types import SimpleNamespace

    import pytest

    ref, data, account = channel_work(growth_agent)
    claim = advance(ref)['claim']
    action = prepare_channel(growth_agent, ref['task_id'], data)
    actor = SimpleNamespace(aid=growth_agent, profile={'id': account['profile_id']}, epoch=1)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.profile_leases SET epoch=1,state='held',expires_at=now()+interval '1 hour' WHERE profile_id=%s", (account['profile_id'],))
        domain.snapshot_preferences(c, growth_agent)
    with pytest.raises(ValueError, match='configuration_changed'):
        with tx(growth_agent) as c:
            submission_gate(c, actor, action, claim)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET state='planned' WHERE id=%s", (ref['task_id'],))
    assert advance(ref)['reason'] == 'configuration_changed'
    with tx(growth_agent) as c:
        assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action['id'],))['state'] == 'canceled'


def test_original_unknown_receipt_survives_operating_revision_change(growth_agent):
    ref, data, _ = channel_work(growth_agent)
    claim = advance(ref)['claim']
    action = prepare_channel(growth_agent, ref['task_id'], data)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s", (action['id'],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s", (action['id'],))
    finish(claim, WorkResult(outcome='unknown'))
    with tx(growth_agent) as c:
        domain.snapshot_preferences(c, growth_agent)
    assert any(source['ref']['id'] == ref['task_id']
               for source in agent.build_context(growth_agent)['continuation_sources'])
    next_claim = advance(ref, now=utcnow()+timedelta(minutes=6))
    assert next_claim['state'] == 'ready'
    assert prepare_channel(growth_agent, ref['task_id'], data)['id'] == action['id']
    with tx(growth_agent) as c:
        assert one(c, 'SELECT count(*) n FROM lead.actions WHERE task_id=%s', (ref['task_id'],))['n'] == 1
