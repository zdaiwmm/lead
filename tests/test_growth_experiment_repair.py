"""Rejected experiment groups can be deliberately repaired through native input."""
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
    utcnow,
)
from lead_app.growth.store import create_experiment, digest
from test_growth_telegram_community import PEER, community, source  # noqa: F401
from test_local_executor import connected


@pytest.mark.parametrize('mode', ['search', 'inspect', 'repeat_experiment', 'change_accepted',
    'paused', 'revoked', 'stale_binding', 'primary_stale_binding'])
async def test_native_repair_keeps_original_groups_and_admission_guards(community, monkeypatch, mode):  # noqa: F811
    aid = community.aid
    connected(aid)
    deadline = utcnow() + timedelta(hours=1)
    experiment = ExperimentSpec(question_type='distribution', question='Where can new relevant discussion be observed?',
        hypothesis='A new public venue may expose a specific need.', learning_signal='An original relevant discussion.',
        token_limit=1000, review_at=deadline)
    with tx(aid) as c:
        for variation in ('first', 'second'):
            old = create_experiment(c, aid, experiment.model_copy(update={'variation': variation}))
            c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s WHERE id=%s",
                (j({'verified_progress': False}), old['id']))
    observed = source(community) if mode == 'inspect' else None

    def account_command(group, query='new relevant discussion'):
        payload = {'community_operation': 'search', 'queries': [query]}
        refs = []
        if observed and group == 'needs-new-venue':
            payload = {'community_operation': 'inspect', 'group_task_id': observed['task_id'], 'group_id': PEER}
            refs = [{'task_id': observed['task_id'], 'reason': 'Inspect this actual observed group.'}]
        spec = TaskSpec(kind='account', purpose='community', account_id=community.account['id'],
            payload=payload, required_refs=refs, review_at=deadline)
        return Command(command_key=group + '-task', group_key=group, kind='task.create', goal_ref={'revision': 1},
            account_id=community.account['id'], expected_binding=community.account['binding_version'],
            payload=spec.model_dump(mode='json'))

    accepted_command = account_command('already-accepted', 'a different independent venue')
    search_command = account_command('needs-new-venue')
    create = Command(command_key='venue-experiment', group_key='needs-new-venue', kind='experiment.create',
        goal_ref={'revision': 1}, payload=experiment.model_copy(update={'variation': 'third'}).model_dump(mode='json'))
    calls = []
    accepted_before = []

    async def complete(request_aid, request_id):
        with tx(request_aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        context = json.loads(request['payload']['input'])['task']
        calls.append(context)
        if len(calls) == 1:
            initial_search = search_command.model_copy(update={'expected_binding': 999}) if mode == 'primary_stale_binding' else search_command
            commands = [accepted_command, create, initial_search]
        else:
            with tx(aid) as c:
                accepted_before.extend(c.execute("SELECT id,input FROM lead.tasks WHERE type='growth.account' ORDER BY id").fetchall())
            if mode in ('paused', 'revoked'):
                set_intent(aid, community.account['id'], mode, source='owner_command', source_ref='fixture:stop-before-repair')
            commands = [create, search_command] if mode == 'repeat_experiment' else [search_command]
            if mode == 'change_accepted':
                commands = [account_command('already-accepted', 'mutated accepted input')]
            if mode == 'stale_binding':
                commands = [search_command.model_copy(update={'expected_binding': 999})]
        proposal = DecisionProposal(context_key=context['context_key'], rationale='Use a bounded valid account primitive.',
            commands=commands, continuation={'condition': 'New original evidence arrives.', 'producer': 'evidence_added',
                'next_action': 'Assess the new evidence without reopening the old stopped source.'})
        result = proposal.model_dump(mode='json')
        return {'result_json': json.dumps(result)} if request['payload']['result_codec'] == 'json_string_v1' else result

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    result = await agent.decide(aid)
    assert len(calls) == 2
    assert calls[0]['context_key'] == calls[1]['context_key'] and calls[0]['goal'] == calls[1]['goal']
    assert len(accepted_before) == (2 if observed else 1)
    repair = calls[1]['repair']
    assert repair['groups'] == ['needs-new-venue']
    assert repair['accepted'][0]['group'] == 'already-accepted'
    if mode == 'primary_stale_binding':
        assert repair['errors'] == [{'group': 'needs-new-venue', 'reason': 'account_changed'}]
        assert 'experiment_create_failures' not in repair
    else:
        assert repair['errors'] == [{'group': 'needs-new-venue', 'reason': 'no_progress_path_exhausted'}]
        assert repair['experiment_create_failures'] == [{'group': 'needs-new-venue', 'command_key': 'venue-experiment',
            'kind': 'experiment.create'}]
        assert 'Command.experiment_id' in repair['instruction'] and 'TaskSpec.experiment_id' in repair['instruction']
        assert 'search/inspect' in repair['instruction'] and 'source-specific stop' in repair['instruction']
    with tx(aid) as c:
        accepted_after = c.execute('SELECT id,input FROM lead.tasks WHERE id=ANY(%s::uuid[]) ORDER BY id',
            ([row['id'] for row in accepted_before],)).fetchall()
        assert digest(accepted_after) == digest(accepted_before)
        assert one(c, 'SELECT count(*) n FROM lead.growth_experiments')['n'] == 2
        assert one(c, 'SELECT onboarding_state FROM lead.agents WHERE id=%s', (aid,))['onboarding_state']['growth_runtime']['decision']['generation'] == 1
        if mode in ('search', 'inspect', 'primary_stale_binding'):
            assert len(result['accepted']) == 2 and result['rejected'] == []
            new = one(c, 'SELECT experiment_id,input FROM lead.tasks WHERE id=%s', (result['accepted'][1]['task_refs'][0]['task_id'],))
            assert new['experiment_id'] is None and new['input']['spec']['experiment_id'] is None
            assert new['input']['spec']['payload'] == search_command.payload['payload']
            assert new['input']['spec']['required_refs'] == search_command.payload['required_refs']
        else:
            assert len(result['accepted']) == 1 and result['repair_exhausted'] is True
            if mode == 'repeat_experiment':
                assert result['rejected'] == [{'group': 'needs-new-venue', 'reason': 'no_progress_path_exhausted'}]
            if mode in ('paused', 'revoked', 'stale_binding'):
                assert result['rejected'] == [{'group': 'needs-new-venue',
                    'reason': 'account_changed' if mode == 'stale_binding' else 'account_paused'}]
