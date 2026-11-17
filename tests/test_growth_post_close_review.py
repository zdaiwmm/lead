"""Accepted receipts do not replace actual runnable work in a bounded decision."""
from datetime import timedelta

import pytest
from lead_app import activities, domain
from lead_app.db import one, tx
from lead_app.growth import agent, runtime
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.store import create_experiment, digest, submit_work
from test_growth_telegram_community import community  # noqa: F401
from test_local_executor import connected


def search_command(env, query, *, key='next-search', group='independent-next-search'):
    return Command(command_key=key, group_key=group, kind='task.create', goal_ref={'revision': 1},
        account_id=env.account['id'], expected_binding=env.account['binding_version'],
        payload=TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
            payload={'community_operation': 'search', 'queries': [query]},
            review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))


async def setup_case(env, monkeypatch, initial_kind):
    aid = env.aid
    connected(aid)
    with tx(aid) as c:
        initial = one(c, "SELECT id,input FROM lead.tasks WHERE type='growth.research'")
    runtime.finish(runtime.advance({'agent_id': aid, 'task_id': str(initial['id']),
        'task_generation': initial['input']['task_generation']})['claim'], WorkResult(outcome='completed'))
    with tx(aid) as c:
        blocked = submit_work(c, aid, TaskSpec(kind='research', purpose='original-stopped-source',
            payload={'urls': ['https://stopped-source.example/original']},
            review_at=utcnow()+timedelta(hours=1)))
    for number in range(3):
        claim = runtime.advance(blocked, now=utcnow()+timedelta(minutes=number))['claim']
        runtime.finish(claim, WorkResult(outcome='failed', failure_code='source_unavailable',
            output={'failure_scope': {'provider': 'direct_http', 'operation': 'read',
                'origin': 'https://stopped-source.example', 'access_mode': 'public'}}))
    with tx(aid) as c:
        c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,recovery}',
            result#>'{runtime,recovery}' || '{"probes":8,"probes_stopped":true}'::jsonb) WHERE id=%s""",
            (blocked['task_id'],))
    browser_calls = []

    async def browser_rpc(path, payload):
        assert path.endswith('/community/search')
        browser_calls.append(payload['query'])
        return {'groups': [] if payload['query'] == 'already observed audience' else [{
            'group_id': '-778899', 'canonical_url': 'https://t.me/privacy_fixture',
            'title': 'Observed independent audience', 'subtitle': 'A bounded synthetic page observation'}]}

    monkeypatch.setattr(activities, 'browser_rpc', browser_rpc)
    experiment = None
    if initial_kind == 'close':
        with tx(aid) as c:
            experiment = create_experiment(c, aid, ExperimentSpec(question_type='distribution',
                question='Can this original directory support acquisition?', hypothesis='The source may expose a relevant audience.',
                learning_signal='A relevant original source and actual permitted task.', token_limit=1000,
                review_at=utcnow()+timedelta(hours=1)))
            failed = submit_work(c, aid, TaskSpec(kind='research', purpose='invalid-original-finding',
                experiment_id=experiment['id'], payload={'urls': ['https://directory.example/original']},
                review_at=utcnow()+timedelta(hours=1)))
        runtime.finish(runtime.advance(failed)['claim'],
            WorkResult(outcome='failed', failure_code='unsupported_research_finding'))
        first = Command(command_key='close-original-experiment', group_key='close-original', kind='experiment.close',
            experiment_id=experiment['id'], expected_revision=experiment['revision'], goal_ref={'revision': 1},
            payload={'state': 'inconclusive', 'reason': 'The original finding is not supported; stop this experiment.'})
    else:
        first = search_command(env, 'already observed audience', key='already-done', group='already-done')
        with tx(aid) as c:
            completed = submit_work(c, aid, first.payload)
        done_claim = runtime.advance(completed)['claim']
        done = await runtime.execute(done_claim)
        assert done['outcome'] == 'completed'
        assert runtime.finish(done_claim, done)['accepted_control']
    decision = runtime.ensure_decision(aid, [{'reason': 'work_failed', 'task_id': blocked['task_id']}])
    claim = runtime.advance(decision)['claim']
    with tx(aid) as c:
        stopped_before = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (blocked['task_id'],))['result']
        permissions = digest(c.execute('SELECT id,revoked_at,allowed_actions,limits FROM lead.authorization_grants ORDER BY id').fetchall())
    return first, decision, claim, blocked, stopped_before, permissions, experiment, browser_calls


def stopped_continuation(context, blocked, valid):
    source = next(source for source in context['continuation_sources'] if source['ref']['id'] == blocked['task_id'])
    assert source['status'] == 'blocked' and source['check_at'] is None
    return {'condition': 'The original stopped source has a verified recovery.', 'producer': source['producer'],
        'responsibility': source['responsibility'], 'source_ref': source['ref'] if valid else None,
        'next_action': 'Use only the actual recovery without resetting this stopped source.'}


@pytest.mark.parametrize('initial_kind', ['close', 'completed_task'])
@pytest.mark.parametrize('valid_blocker', [False, True], ids=['missing-source', 'stopped-source'])
async def test_accepted_receipt_without_pending_work_reviews_and_completes_an_independent_task(
        community, monkeypatch, initial_kind, valid_blocker):  # noqa: F811
    aid = community.aid
    first, decision, claim, blocked, stopped_before, permissions, experiment, browser_calls = await setup_case(
        community, monkeypatch, initial_kind)
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(context)
        assert context['discovery']['native_public_read'] is True
        return DecisionProposal(context_key=context['context_key'], rationale='Close or acknowledge old work, then investigate a different permitted audience.',
            commands=[first] if len(calls) == 1 else [search_command(community, 'another permitted audience')],
            continuation=stopped_continuation(context, blocked, valid_blocker))

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    assert runtime.finish(claim, WorkResult(outcome='completed', output=result))['accepted_control']
    assert len(calls) == 2
    review = calls[1]['repair']
    assert review.get('no_work_review') is True, review
    assert 'continuation_only' not in review
    assert len(review['accepted']) == 1
    assert bool(review['accepted'][0]['task_refs']) == (initial_kind == 'completed_task')
    assert calls[0]['context_key'] == calls[1]['context_key']
    assert calls[0]['config_revision'] == calls[1]['config_revision']
    assert calls[0]['goal'] == calls[1]['goal']
    assert result['rejected'] == [] and len(result['accepted']) == 2
    next_ref = result['accepted'][1]['task_refs'][0]
    next_claim = runtime.advance(next_ref)['claim']
    observed = await runtime.execute(next_claim)
    assert observed['outcome'] == 'completed' and observed['output']['groups']
    assert runtime.finish(next_claim, observed)['accepted_control']
    assert browser_calls[-1] == 'another permitted audience'
    with tx(aid) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (next_ref['task_id'],))['state'] == 'succeeded'
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.wake' AND payload->>'task_id'=%s AND payload->>'reason'='evidence_added'",
            (next_ref['task_id'],))['n'] == 1
        assert one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (blocked['task_id'],))['result'] == stopped_before
        assert digest(c.execute('SELECT id,revoked_at,allowed_actions,limits FROM lead.authorization_grants ORDER BY id').fetchall()) == permissions
        if experiment:
            assert one(c, 'SELECT state,revision FROM lead.growth_experiments WHERE id=%s', (experiment['id'],)) == {'state': 'closed', 'revision': 2}


@pytest.mark.parametrize('change', ['configuration', 'goal', 'pause'])
async def test_bounded_review_keeps_first_receipt_when_current_scope_changes(community, monkeypatch, change):  # noqa: F811
    aid = community.aid
    first, decision, claim, blocked, stopped_before, _, experiment, _ = await setup_case(community, monkeypatch, 'close')
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(context)
        if len(calls) == 2:
            if change == 'pause':
                domain.set_control(aid, 'pause')
            else:
                with tx(aid) as c:
                    if change == 'configuration':
                        domain.snapshot_preferences(c, aid)
                    else:
                        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{operating_contract}','{\"revision\":2}') WHERE id=%s", (aid,))
        return DecisionProposal(context_key=context['context_key'], rationale='Use a current independent route within the same decision.',
            commands=[first] if len(calls) == 1 else [search_command(community, 'another permitted audience')],
            continuation=stopped_continuation(context, blocked, False))

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    assert len(calls) == 2 and calls[1]['repair'].get('no_work_review') is True
    assert len(result['accepted']) == 1 and result['accepted'][0]['task_refs'] == []
    with tx(aid) as c:
        assert one(c, 'SELECT state,revision FROM lead.growth_experiments WHERE id=%s', (experiment['id'],)) == {'state': 'closed', 'revision': 2}
        assert not one(c, "SELECT id FROM lead.tasks WHERE type='growth.account'")
        assert one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (blocked['task_id'],))['result'] == stopped_before
    if change == 'configuration':
        assert result['discarded'] == 'configuration_changed'
    elif change == 'goal':
        assert result['rejected'][0]['reason'] == 'objective_changed'
    else:
        assert result.get('discarded') == 'stale_decision' or result['rejected'][0]['reason'] == 'project_paused'


@pytest.mark.parametrize('replay', ['group', 'command'])
async def test_bounded_review_cannot_replay_an_accepted_group_or_command(community, monkeypatch, replay):  # noqa: F811
    aid = community.aid
    first, decision, claim, blocked, _, _, _, _ = await setup_case(community, monkeypatch, 'completed_task')
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(context)
        repeated = search_command(community, 'another permitted audience',
            key=first.command_key if replay == 'command' else 'new-key',
            group=first.group_key if replay == 'group' else 'new-group')
        return DecisionProposal(context_key=context['context_key'], rationale='Never replay an accepted original group or command.',
            commands=[first] if len(calls) == 1 else [repeated],
            continuation=stopped_continuation(context, blocked, False))

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    assert len(calls) == 2 and calls[1]['repair'].get('no_work_review') is True
    assert len(result['accepted']) == 1 and result['repair_exhausted']
    with tx(aid) as c:
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.account'")['n'] == 1


async def test_accepted_close_with_a_real_future_review_does_not_add_a_proposal(community, monkeypatch):  # noqa: F811
    aid = community.aid
    first, decision, claim, _, _, _, _, _ = await setup_case(community, monkeypatch, 'close')
    with tx(aid) as c:
        future = create_experiment(c, aid, ExperimentSpec(question_type='audience',
            question='Does the original placement expose a different audience?',
            hypothesis='The existing observation window may produce a relevant response.',
            learning_signal='The original observed response or an inconclusive review.', token_limit=1000,
            review_at=utcnow()+timedelta(hours=1)))
    calls = []

    async def model(schema, context, **kwargs):
        calls.append(context)
        source = next(source for source in context['continuation_sources'] if source['ref']['id'] == str(future['id']))
        return DecisionProposal(context_key=context['context_key'], rationale='The independent original review is a real producer.',
            commands=[first], continuation={'condition': 'The current original experiment reaches its review.',
                'producer': source['producer'], 'responsibility': source['responsibility'], 'source_ref': source['ref'],
                'next_action': 'Evaluate the actual original observation window.'})

    monkeypatch.setattr(agent, 'run_model', model)
    result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    assert len(calls) == 1 and len(result['accepted']) == 1
    assert result['status'] == 'waiting_for_change' and result['continuation_check']['valid']
