"""Current owner preferences cross the real frozen/native decision boundary."""
import json
from datetime import timedelta

import pytest
from lead_app import commands, domain, local_executor_client
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.growth import agent
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    utcnow,
)
from lead_app.growth.runtime import advance, ensure_decision
from lead_app.growth.store import accept_commands, create_experiment, digest
from lead_app.model import Decision
from test_growth_runtime import new_work
from test_growth_telegram_community import community  # noqa: F401
from test_local_executor import connected


@pytest.fixture
def native_payloads(monkeypatch):
    captured = []

    async def complete(aid, request_id):
        with tx(aid) as c:
            request = one(c, 'SELECT id,payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        captured.append(request)
        context = json.loads(request['payload']['input'])['task']
        result = DecisionProposal(context_key=context['context_key'], rationale='No new original evidence yet.',
            commands=[], continuation={'condition': 'New original evidence arrives.', 'producer': 'evidence_added',
                'next_action': 'Assess the new evidence.'}).model_dump(mode='json')
        return {'result_json': json.dumps(result)} if request['payload']['result_codec'] == 'json_string_v1' else result

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    return captured


async def test_saved_owner_preferences_reach_native_decision_and_keep_frozen_input(growth_agent, native_payloads):
    aid = growth_agent
    new_work(aid)
    connected(aid)
    revision = domain.update_preferences(aid, 1, ['DELETED_OWNER_PREFERENCE'])['revision']
    sourced = 'Use the original account controls and verify every actual receipt.'
    message = domain.submit_message(aid, '记住：' + sourced, uid())
    with tx(aid) as c:
        commands.apply_preference(c, aid, message, Decision(reply='Saved.', preference=sourced), revision)
        revision = one(c, 'SELECT config_revision FROM lead.agents WHERE id=%s', (aid,))['config_revision']
    direction = 'CURRENT_OWNER_DIRECTION：自主寻找产品受众与公开场所，遵守平台规则和现有权限。' + '完整指令。' * 100
    revision = domain.update_preferences(aid, revision, [direction, sourced])['revision']
    other = domain.create_agent(runtime_engine='growth')
    domain.update_preferences(other, 1, ['OTHER_AGENT_OWNER_PREFERENCE'])
    with tx(aid) as c:
        expected = all_rows(c, """SELECT id,body->>'text' text,source_refs,confirmation
            FROM lead.memory_entries WHERE kind='preference' AND deleted_at IS NULL
            ORDER BY created_at DESC,id DESC LIMIT 100""")
        trigger = one(c, """SELECT payload FROM lead.outbox_events WHERE event_type='growth.wake'
            AND payload->>'reason'='preferences_changed' ORDER BY occurred_at DESC,id DESC LIMIT 1""")['payload']
    assert any(entry['source_refs'] for entry in expected)
    ref = ensure_decision(aid, [trigger])
    claim = advance(ref)['claim']
    token = local_executor_client.execution_context.set({'key': 'decision:' + ref['task_id'],
        'task_id': ref['task_id'], 'session_key': 'decision:' + aid + ':1'})
    try:
        await agent.decide(aid, ref['task_id'], decision_claim=claim)
        payload = native_payloads[0]['payload']
        context = json.loads(payload['input'])['task']
        assert context['config_revision'] == revision
        assert context['owner_preferences'] == json.loads(json.dumps(expected, default=str))
        assert direction in payload['input']
        assert 'DELETED_OWNER_PREFERENCE' not in payload['input']
        assert 'OTHER_AGENT_OWNER_PREFERENCE' not in payload['input']
        assert all(entry['confirmation'] == 'user_explicit' for entry in context['owner_preferences'])
        with tx(aid) as c:
            frozen = one(c, "SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s", (ref['task_id'],))['context']
        assert frozen == context
        domain.update_preferences(aid, revision, ['A_NEWER_OWNER_DIRECTION'])
        await agent.decide(aid, ref['task_id'], decision_claim=claim)
        assert native_payloads[1] == native_payloads[0]
        with tx(aid) as c:
            assert one(c, "SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s", (ref['task_id'],))['context'] == frozen
        fresh = agent.build_context(aid)
        assert [entry['text'] for entry in fresh['owner_preferences']] == ['A_NEWER_OWNER_DIRECTION']
        assert fresh['config_revision'] == revision + 1 and fresh['context_key'] != frozen['context_key']
    finally:
        local_executor_client.execution_context.reset(token)


async def test_native_preferences_keep_complete_ui_sized_text_and_limit_memory_rows(growth_agent, native_payloads):
    connected(growth_agent)
    entries = ['%03d:' % i + '界' * 3996 for i in range(100)]
    revision = domain.update_preferences(growth_agent, 1, entries)['revision']
    # A later canonical chat preference can make historical active memory exceed
    # the settings form's 100 entries; the context keeps the latest 100 whole rows.
    with tx(growth_agent) as c:
        domain.remember(c, growth_agent, 'LATEST_CHAT_PREFERENCE')
    await agent.decide(growth_agent)
    payload = native_payloads[0]['payload']
    context = json.loads(payload['input'])['task']
    assert context['config_revision'] == revision + 1
    assert len(context['owner_preferences']) == 100
    texts = [entry['text'] for entry in context['owner_preferences']]
    assert 'LATEST_CHAT_PREFERENCE' in texts
    assert len(set(texts) & set(entries)) == 99
    assert all(len(text) == 4000 for text in texts if text != 'LATEST_CHAT_PREFERENCE')
    assert len(payload['input']) < 1048576


async def test_preexisting_frozen_decision_is_not_backfilled_with_new_owner_preferences(growth_agent, native_payloads):
    new_work(growth_agent)
    connected(growth_agent)
    ref = ensure_decision(growth_agent, [{'reason': 'preferences_changed', 'revision': 1}])
    claim = advance(ref)['claim']
    historical = agent.decision_context(growth_agent, ref['task_id'], claim, [])
    # Seed the unchanged shape of an already frozen pre-upgrade decision.
    historical.pop('config_revision', None)
    historical.pop('owner_preferences', None)
    historical['prompt_version'] = 'growth-v2.9'
    historical['context_key'] = digest({k: v for k, v in historical.items() if k not in ('now', 'budget', 'context_key')})
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,decision_context}',%s) WHERE id=%s",
            (j(historical), ref['task_id']))
    domain.update_preferences(growth_agent, 1, ['OWNER_DIRECTION_AFTER_FREEZE'])
    await agent.decide(growth_agent, ref['task_id'], decision_claim=claim)
    sent = json.loads(native_payloads[0]['payload']['input'])['task']
    assert sent == historical and 'OWNER_DIRECTION_AFTER_FREEZE' not in native_payloads[0]['payload']['input']
    with tx(growth_agent) as c:
        assert one(c, "SELECT result#>'{runtime,decision_context}' context FROM lead.tasks WHERE id=%s", (ref['task_id'],))['context'] == historical


def test_exhausted_audience_experiment_does_not_block_independent_account_search(community):  # noqa: F811
    aid = community.aid
    spec = ExperimentSpec(question_type='audience', question='Who needs this product?',
        hypothesis='A relevant audience may need it.', learning_signal='An original demand statement.',
        token_limit=1000, review_at=utcnow() + timedelta(hours=1))
    with tx(aid) as c:
        for variation in ('first', 'second'):
            experiment = create_experiment(c, aid, spec.model_copy(update={'variation': variation}))
            c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s WHERE id=%s",
                (j({'verified_progress': False}), experiment['id']))
    search = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
        payload={'community_operation': 'search', 'queries': ['relevant product audience']},
        review_at=utcnow() + timedelta(hours=1))
    result = accept_commands(aid, DecisionProposal(context_key='current', rationale='Find a new original venue first.', commands=[
        Command(command_key='search', group_key='independent-search', kind='task.create', goal_ref={'revision': 1},
            account_id=community.account['id'], expected_binding=community.account['binding_version'],
            payload=search.model_dump(mode='json')),
        Command(command_key='repeat-audience', group_key='exhausted-audience', kind='experiment.create',
            goal_ref={'revision': 1}, payload=spec.model_copy(update={'variation': 'third'}).model_dump(mode='json'))]))
    assert len(result['accepted']) == 1 and result['accepted'][0]['group'] == 'independent-search'
    assert result['rejected'] == [{'group': 'exhausted-audience', 'reason': 'no_progress_path_exhausted'}]
    with tx(aid) as c:
        task = one(c, 'SELECT experiment_id,input FROM lead.tasks WHERE id=%s', (result['accepted'][0]['task_refs'][0]['task_id'],))
        assert task['experiment_id'] is None and task['input']['spec'] == search.model_dump(mode='json')
        assert one(c, 'SELECT count(*) n FROM lead.growth_experiments')['n'] == 2
