"""One native repair sees masked source errors without bypassing original admission."""
import json
from datetime import datetime, timedelta

import pytest
from lead_app import local_executor_client
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx
from lead_app.growth import agent
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.runtime import advance, ensure_decision, finish
from lead_app.growth.store import digest, submit_work
from test_growth_telegram_community import community as community
from test_growth_telegram_community import source
from test_local_executor import connected

PEER = '-2074413183'
VALID_URL = 'https://t.me/batchat_1234'
BAD_URL = 'https://t.me/batchat'
STOP = {'condition': 'A genuinely new relevant original source is observed.', 'producer': 'evidence_added',
    'responsibility': 'product', 'next_action': 'Use the new source while preserving invalid sources and original unknown results.'}


def historical_source_and_failed_predecessor(env, *, invalid):
    """Seed retained pre-fix output, then leave every original row immutable."""
    with tx(env.aid) as c:
        initial = one(c, "SELECT id,input FROM lead.tasks WHERE type='growth.research'")
    initial_ref = {'agent_id': env.aid, 'task_id': str(initial['id']), 'task_generation': initial['input']['task_generation']}
    finish(advance(initial_ref)['claim'], WorkResult(outcome='completed', output={'fixture_setup_complete': True}))
    original = source(env, {'group_id': PEER, 'title': '蝙蝠App-加密聊天/通讯工具/加密软件交流群@batchat',
        'canonical_url': VALID_URL, 'subtitle': '@batchat_1234, 3 039 members\n蝙'})
    with tx(env.aid) as c:
        failed = submit_work(c, env.aid, TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
            required_refs=[{'task_id': original['task_id'], 'reason': 'Inspect the original selected group.'}],
            payload={'community_operation': 'inspect', 'group_task_id': original['task_id'], 'group_id': PEER},
            review_at=utcnow()-timedelta(hours=2)))
    # A real terminal predecessor is sufficient for the same dependency guard;
    # no external request or invented successful inspection is needed.
    assert advance(failed)['reason'] == 'work_window_ended'
    if invalid:
        with tx(env.aid) as c:
            saved = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (original['task_id'],))['result']
            saved['output']['output']['groups'][0]['canonical_url'] = BAD_URL
            c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(saved), original['task_id']))
    return original, failed


@pytest.mark.parametrize('mode', ['search', 'repeat_bad_source', 'binding_changed', 'paused', 'valid_source'])
async def test_native_repair_reviews_failed_dependency_and_source_in_the_same_call(community, monkeypatch, mode):
    aid = community.aid
    connected(aid)
    original, failed = historical_source_and_failed_predecessor(community, invalid=mode != 'valid_source')
    retained_ids = [original['task_id'], failed['task_id']]

    def retained_snapshot(c):
        return digest(c.execute('SELECT id,state,input,result FROM lead.tasks WHERE id=ANY(%s::uuid[]) ORDER BY id',
            (retained_ids,)).fetchall())

    with tx(aid) as c:
        before = retained_snapshot(c)
        failed_deadline = one(c, 'SELECT input FROM lead.tasks WHERE id=%s', (failed['task_id'],))['input']['spec']['review_at']
        experiment_snapshot = digest(c.execute('SELECT * FROM lead.growth_experiments ORDER BY id').fetchall())
    decision = ensure_decision(aid, [{'reason': 'configuration_changed', 'control_version': 1, 'config_revision': 1}])
    claim = advance(decision)['claim']
    past_deadline = utcnow()-timedelta(hours=1)
    spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        required_refs=[{'task_id': original['task_id'], 'reason': 'Use the actual search source.'},
            {'task_id': failed['task_id'], 'reason': 'Retain the old inspection as a mandatory predecessor.'}],
        evidence_refs=[{'kind': 'task', 'id': original['task_id']}, {'kind': 'task', 'id': failed['task_id']}],
        payload={'community_operation': 'inspect', 'group_task_id': original['task_id'], 'group_id': PEER},
        review_at=past_deadline)
    command = Command(command_key='inspect-after-read-recovery', group_key='recovered-account-read', kind='task.create',
        account_id=community.account['id'], expected_binding=community.account['binding_version'], goal_ref={'revision': 1},
        payload=spec.model_dump(mode='json'))
    calls = []
    started = utcnow()
    repaired_spec = None

    async def complete(request_aid, request_id):
        nonlocal repaired_spec
        with tx(request_aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
            assert retained_snapshot(c) == before
        context = json.loads(request['payload']['input'])['task']
        calls.append(context)
        if len(calls) == 1:
            assert 'repair' not in context
            chosen = command
        else:
            assert len(calls) == 2
            repair = context['repair']
            assert repair['errors'] == [{'group': command.group_key, 'reason': 'dependency_failed'}]
            assert repair['groups'] == [command.group_key] and repair['accepted'] == []
            assert repair['community_task_reviews'] == [{'group': command.group_key, 'command_key': command.command_key,
                'required_tasks': [{'task_id': original['task_id'], 'state': 'succeeded'},
                    {'task_id': failed['task_id'], 'state': 'failed'}],
                'source_error': None if mode == 'valid_source' else 'community_source_binding_invalid'}]
            server_now = datetime.fromisoformat(repair['server_now'])
            assert server_now.tzinfo and started <= server_now <= utcnow() and past_deadline < server_now
            assert 'SAME rejected group' in repair['instruction'] and 'independent community search' in repair['instruction']
            assert 'never extend an old task deadline' in repair['instruction']
            if mode in ('repeat_bad_source', 'valid_source'):
                repaired_spec = spec.model_copy(update={'required_refs': spec.required_refs[:1],
                    'review_at': server_now+timedelta(minutes=30)})
            else:
                repaired_spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
                    evidence_refs=spec.evidence_refs,
                    payload={'community_operation': 'search', 'queries': ['privacy tools discussion']},
                    review_at=server_now+timedelta(minutes=30))
            chosen = command.model_copy(update={'payload': repaired_spec.model_dump(mode='json')})
            if mode == 'binding_changed':
                with tx(aid) as c:
                    c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s', (community.account['id'],))
            elif mode == 'paused':
                set_intent(aid, community.account['id'], 'paused', source='owner_command', source_ref='fixture:pause-before-repair')
        proposal = DecisionProposal(context_key=context['context_key'], rationale='Use current original admission facts and preserve stopped sources.',
            commands=[chosen], continuation=STOP)
        value = proposal.model_dump(mode='json')
        return {'result_json': json.dumps(value)} if request['payload']['result_codec'] == 'json_string_v1' else value

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    token = local_executor_client.execution_context.set({'key': 'decision:'+decision['task_id'],
        'task_id': decision['task_id'], 'session_key': 'decision:'+aid+':1'})
    try:
        result = await agent.decide(aid, decision['task_id'], decision_claim=claim)
    finally:
        local_executor_client.execution_context.reset(token)
    assert len(calls) == 2 and calls[0]['context_key'] == calls[1]['context_key']
    assert calls[0]['goal'] == calls[1]['goal'] and calls[0]['now'] == calls[1]['now']
    with tx(aid) as c:
        assert retained_snapshot(c) == before
        failed_row = one(c, 'SELECT state,input FROM lead.tasks WHERE id=%s', (failed['task_id'],))
        assert failed_row['state'] == 'failed' and failed_row['input']['spec']['review_at'] == failed_deadline
        assert digest(c.execute('SELECT * FROM lead.growth_experiments ORDER BY id').fetchall()) == experiment_snapshot
        assert one(c, 'SELECT count(*) n FROM lead.local_executor_requests WHERE task_id=%s', (decision['task_id'],))['n'] == 2
        new = c.execute("SELECT id,state,experiment_id,input FROM lead.tasks WHERE type='growth.account' AND NOT(id=ANY(%s::uuid[]))",
            (retained_ids,)).fetchall()
        assert one(c, 'SELECT count(*) n FROM lead.actions')['n'] == 0
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0
        frozen = one(c, "SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s", (decision['task_id'],))['context']
        assert frozen['context_key'] == calls[0]['context_key'] and 'repair' not in frozen
    if mode in ('search', 'valid_source'):
        assert result['rejected'] == [] and len(result['accepted']) == len(new) == 1
        assert result['accepted'][0]['group'] == command.group_key
        assert new[0]['input']['spec'] == repaired_spec.model_dump(mode='json')
        assert new[0]['experiment_id'] is None and new[0]['input']['spec']['experiment_id'] is None
        admitted = result['accepted'][0]['task_refs'][0]
        assert str(new[0]['id']) == admitted['task_id']
        assert advance(admitted)['state'] == 'ready'  # Finite future window, not an already expired accepted task.
    else:
        assert result['accepted'] == [] and new == [] and result['repair_exhausted'] is True
        assert result['rejected'] == [{'group': command.group_key, 'reason': {
            'repeat_bad_source': 'community_source_binding_invalid',
            'binding_changed': 'account_changed', 'paused': 'account_paused'}[mode]}]
