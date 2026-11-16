"""A no-work judgment gets one review within the original two-call decision."""
import json
from datetime import timedelta

import pytest
from lead_app import local_executor_client
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx
from lead_app.growth import agent
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.runtime import advance, ensure_decision, finish
from lead_app.growth.store import create_experiment, digest
from test_growth_telegram_community import community  # noqa: F401
from test_local_executor import connected

STOP = {'condition': 'A relevant new original source or actual resource recovery is observed.',
    'producer': 'resource_recovered', 'responsibility': 'product',
    'next_action': 'Use the actual change while preserving stopped sources and unknown actions.'}


@pytest.mark.parametrize('mode', ['search', 'retain_stop', 'stale_binding', 'paused', 'repeat_experiment', 'invalid_fields', 'stale_claim'])
async def test_empty_native_decision_reviews_current_scope_without_bypassing_admission(community, monkeypatch, mode):  # noqa: F811
    aid = community.aid
    connected(aid)
    deadline = utcnow() + timedelta(hours=1)
    with tx(aid) as c:
        initial = one(c, "SELECT id,input FROM lead.tasks WHERE type='growth.research'")
    ref = {'agent_id': aid, 'task_id': str(initial['id']), 'task_generation': initial['input']['task_generation']}
    finish(advance(ref)['claim'], WorkResult(outcome='completed', output={'fixture_setup_complete': True}))
    experiment = ExperimentSpec(question_type='distribution', question='Where is a relevant real discussion?',
        hypothesis='A relevant venue may expose an original need.', learning_signal='An original discussion.',
        token_limit=1000, review_at=deadline)
    with tx(aid) as c:
        for variation in ('first', 'second'):
            old = create_experiment(c, aid, experiment.model_copy(update={'variation': variation}))
            c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s WHERE id=%s",
                (j({'verified_progress': False}), old['id']))
        before = digest(c.execute('SELECT id,revision,state,conclusion FROM lead.growth_experiments ORDER BY id').fetchall())
        wakes_before = digest(c.execute("SELECT id,payload FROM lead.outbox_events WHERE event_type='growth.wake' ORDER BY id").fetchall())
    decision = ensure_decision(aid, [{'reason': 'configuration_changed', 'control_version': 1, 'config_revision': 1}])
    claim = advance(decision)['claim']
    account = Command(command_key='independent-search', group_key='independent-search', kind='task.create',
        goal_ref={'revision': 1}, account_id=community.account['id'], expected_binding=community.account['binding_version'],
        payload=TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
            payload={'community_operation': 'search', 'queries': ['relevant privacy discussion']},
            review_at=deadline).model_dump(mode='json'))
    calls = []
    first = None

    async def complete(request_aid, request_id):
        nonlocal first, wakes_before
        with tx(request_aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        context = json.loads(request['payload']['input'])['task']
        calls.append(context)
        if len(calls) == 1:
            proposal = DecisionProposal(context_key=context['context_key'], commands=[], continuation=STOP,
                rationale='The previous experiment group rolled back, so account search must wait for a runtime change.')
            first = proposal.model_dump(mode='json')
        else:
            assert len(calls) == 2
            review = context['repair']
            assert review['no_work_review'] is True and review['previous_proposal'] == first
            assert review['accepted'] == []
            assert not {'groups', 'errors', 'continuation_only'} & review.keys()
            assert 'experiment.create' in review['instruction'] and 'atomic rollback' in review['instruction']
            assert 'connection_account_id' in review['instruction']
            commands = [] if mode == 'retain_stop' else [account]
            if mode == 'paused':
                set_intent(aid, community.account['id'], 'paused', source='owner_command', source_ref='fixture:pause')
                with tx(aid) as c:
                    wakes_before = digest(c.execute("SELECT id,payload FROM lead.outbox_events WHERE event_type='growth.wake' ORDER BY id").fetchall())
            if mode == 'stale_binding':
                commands = [account.model_copy(update={'expected_binding': 999})]
            if mode == 'repeat_experiment':
                commands = [Command(command_key='still-exhausted', group_key='still-exhausted', kind='experiment.create',
                    goal_ref={'revision': 1}, payload=experiment.model_copy(update={'variation': 'renamed'}).model_dump(mode='json'))]
            if mode == 'invalid_fields':
                commands = [account.model_copy(update={'payload': {**account.payload,
                    'payload': {**account.payload['payload'], 'connection_account_id': str(community.account['id'])}}})]
            if mode == 'stale_claim':
                with tx(aid) as c:
                    c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',%s) WHERE id=%s",
                        (j((utcnow()-timedelta(seconds=1)).isoformat()), decision['task_id']))
            proposal = DecisionProposal(context_key=context['context_key'], commands=commands, continuation=STOP,
                rationale='Recheck the current operation scope; preserve real stops and original unknown results.')
        result = proposal.model_dump(mode='json')
        return {'result_json': json.dumps(result)} if request['payload']['result_codec'] == 'json_string_v1' else result

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    token = local_executor_client.execution_context.set({'key': 'decision:' + decision['task_id'],
        'task_id': decision['task_id'], 'session_key': 'decision:' + aid + ':1'})
    try:
        result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    finally:
        local_executor_client.execution_context.reset(token)
    assert len(calls) == 2
    assert calls[0]['context_key'] == calls[1]['context_key'] and calls[0]['goal'] == calls[1]['goal']
    with tx(aid) as c:
        assert digest(c.execute('SELECT id,revision,state,conclusion FROM lead.growth_experiments ORDER BY id').fetchall()) == before
        assert digest(c.execute("SELECT id,payload FROM lead.outbox_events WHERE event_type='growth.wake' ORDER BY id").fetchall()) == wakes_before
        tasks = c.execute("SELECT experiment_id,input FROM lead.tasks WHERE type='growth.account'").fetchall()
        assert one(c, "SELECT count(*) n FROM lead.local_executor_requests")['n'] == 2
        assert one(c, "SELECT count(*) n FROM lead.local_executor_requests WHERE task_id=%s", (decision['task_id'],))['n'] == 2
    if mode == 'search':
        assert len(result['accepted']) == len(tasks) == 1 and result['rejected'] == []
        assert tasks[0]['experiment_id'] is None and tasks[0]['input']['spec']['experiment_id'] is None
        assert tasks[0]['input']['spec']['payload'] == account.payload['payload']
    else:
        assert result['accepted'] == [] and tasks == []
        if mode == 'retain_stop':
            assert result['status'] == 'waiting_for_change' and result['continuation'] == STOP
        elif mode == 'stale_claim':
            assert result['discarded'] == 'stale_decision'
        else:
            assert result['repair_exhausted'] is True and len(result['rejected']) == 1
            reason = result['rejected'][0]['reason']
            if mode == 'invalid_fields':
                assert 'community_operation_cannot_replace_account_connection' in reason
            else:
                assert reason == {'paused': 'account_paused', 'stale_binding': 'account_changed',
                    'repeat_experiment': 'no_progress_path_exhausted'}[mode]


@pytest.mark.parametrize('new_command', [False, True])
async def test_pending_or_accepted_work_does_not_get_a_no_work_review(community, monkeypatch, new_command):  # noqa: F811
    aid = community.aid
    connected(aid)
    calls = []

    async def complete(request_aid, request_id):
        with tx(request_aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        context = json.loads(request['payload']['input'])['task']
        calls.append(context)
        commands = []
        if new_command:
            commands = [Command(command_key='accepted', group_key='accepted', kind='task.create', goal_ref={'revision': 1},
                account_id=community.account['id'], expected_binding=community.account['binding_version'],
                payload=TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
                    payload={'community_operation': 'search', 'queries': ['a relevant venue']},
                    review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))]
        proposal = DecisionProposal(context_key=context['context_key'], rationale='Original work remains pending.',
            commands=commands, continuation=STOP)
        value = proposal.model_dump(mode='json')
        return {'result_json': json.dumps(value)} if request['payload']['result_codec'] == 'json_string_v1' else value

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    result = await agent.decide(aid)
    assert len(calls) == 1 and 'repair' not in calls[0]
    assert len(result['accepted']) == int(new_command)


async def test_no_declared_account_investigation_keeps_original_stop(growth_agent, monkeypatch):
    connected(growth_agent)
    calls = []

    async def complete(aid, request_id):
        with tx(aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        context = json.loads(request['payload']['input'])['task']
        calls.append(context)
        assert not any(account.get('community_operations') for account in context['accounts'])
        proposal = DecisionProposal(context_key=context['context_key'], rationale='No new permitted account investigation is declared.',
            commands=[], continuation=STOP)
        result = proposal.model_dump(mode='json')
        return {'result_json': json.dumps(result)} if request['payload']['result_codec'] == 'json_string_v1' else result

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    result = await agent.decide(growth_agent)
    assert len(calls) == 1 and result['status'] == 'waiting_for_change'
