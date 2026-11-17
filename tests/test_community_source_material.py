"""Live native group text supplies existing document/experiment admission, not permission."""
import json
from datetime import datetime, timedelta

import pytest
from lead_app import activities, community_runtime, local_executor_client
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx, uid
from lead_app.execution_routes import register
from lead_app.growth import runtime
from lead_app.growth.contracts import (
    Command,
    DecisionProposal,
    ExperimentSpec,
    TaskSpec,
    WorkResult,
    utcnow,
)
from lead_app.growth.store import accept_commands, create_experiment, digest
from lead_app.local_executor_tools import _business_read
from lead_app.scope import service_scope
from test_growth_telegram_community import PEER, URL, source
from test_growth_telegram_community import community as community
from test_local_executor import connected

DESCRIPTION = 'Privacy tools discussion. Be helpful and respectful.'
MESSAGE = 'How can I keep a chat private? ' + 'original message text ' * 80


def native_observation(*, description=DESCRIPTION, messages=True):
    return {'target_id': PEER, 'title': 'Privacy fixture', 'kind': 'discussion',
        'description': description, 'joined': False, 'can_reply': False,
        'history': [{'mid': str((1 << 32)+10), 'body': MESSAGE, 'own': False}] if messages else [],
        'dialogs': [], 'join_button_count': 1, 'apply_button_count': 0, 'controls': [], 'members': 20}


def admitted_inspect(env, predecessor):
    spec = TaskSpec(kind='account', purpose='community', account_id=env.account['id'],
        payload={'community_operation': 'inspect', 'group_task_id': predecessor['task_id'], 'group_id': PEER},
        required_refs=[{'task_id': predecessor['task_id'], 'reason': 'Read the original selected group.'}],
        dedupe_key=uid(), review_at=utcnow()+timedelta(hours=1))
    result = accept_commands(env.aid, DecisionProposal(context_key=uid(), rationale='Inspect actual group text.', commands=[
        Command(command_key=uid(), group_key=uid(), kind='task.create', goal_ref={'revision': 1},
            account_id=env.account['id'], expected_binding=env.account['binding_version'], payload=spec.model_dump(mode='json'))]))
    assert not result['rejected']
    return result['accepted'][0]['task_refs'][0]


def native_rpc(env, monkeypatch, *, observation=None, after=None):
    async def observe(*args, **kwargs):
        return observation or native_observation()

    async def rpc(path, body):
        assert path == f'/agents/{env.aid}/community/step' and body['operation'] == 'inspect'
        result = await community_runtime.growth_community_step(env.actor, body['task_id'], 'inspect', body['growth_claim'],
            execution_ref=env.actor.execution_ref)
        if after:
            after(result)
        return result

    monkeypatch.setattr(community_runtime, 'observe_group', observe)
    monkeypatch.setattr(activities, 'browser_rpc', rpc)


async def inspect(env, predecessor):
    ref = admitted_inspect(env, predecessor)
    claim = runtime.advance(ref)['claim']
    with service_scope():
        result = await runtime.execute(claim)
    return ref, claim, result


def exhausted(env):
    spec = ExperimentSpec(question_type='distribution', question='Where can a relevant audience be reached?',
        hypothesis='Original group discussion may show a concrete need.', learning_signal='An original relevant message.',
        token_limit=1000, review_at=utcnow()+timedelta(hours=1))
    with tx(env.aid) as c:
        for variation in ('first', 'second'):
            row = create_experiment(c, env.aid, spec.model_copy(update={'variation': variation}))
            c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s WHERE id=%s",
                (j({'verified_progress': False}), row['id']))
    return spec


def experiment_and_content(env, spec, evidence, *, variation):
    group = uid()
    proposal = DecisionProposal(context_key=uid(), rationale='Assess genuinely new original group evidence.', commands=[
        Command(command_key=uid(), group_key=group, kind='experiment.create', goal_ref={'revision': 1},
            payload=spec.model_copy(update={'variation': variation, 'evidence_refs': evidence}).model_dump(mode='json')),
        Command(command_key=uid(), group_key=group, kind='task.create', goal_ref={'revision': 1}, payload=TaskSpec(
            kind='content', purpose='community-reply', evidence_refs=evidence, payload={'brief': 'Answer the observed privacy question factually.'},
            review_at=utcnow()+timedelta(hours=1)).model_dump(mode='json'))])
    return accept_commands(env.aid, proposal)


async def test_live_inspect_unlocks_real_admission_and_original_native_content_input(community, monkeypatch):
    connected(community.aid)
    experiment = exhausted(community)
    assert experiment_and_content(community, experiment, [], variation='blocked')['rejected'][0]['reason'] == 'no_progress_path_exhausted'
    original = source(community)
    native_rpc(community, monkeypatch)
    ref, claim, result = await inspect(community, original)
    assert result['outcome'] == 'completed' and len(result['evidence_refs']) == 2
    assert result['output']['knowledge_progress'] is True and result['output']['inspection_task_id'] == ref['task_id']
    runtime.finish(claim, result)
    with tx(community.aid) as c:
        docs = c.execute('SELECT id,source_ref,source_type,sha256,trust_state,created_at FROM lead.knowledge_documents ORDER BY source_ref').fetchall()
        assert [d['source_ref'] for d in docs] == [URL, URL+'/10']
        assert all(d['source_type'] == 'account_read' and d['trust_state'] == 'untrusted_source' for d in docs)
        assert all(d['created_at'] == datetime.fromisoformat(result['output']['group']['observed_at']) for d in docs)
        before = digest(docs)
    document = _business_read({'agent_id': community.aid}, 'lead_document_read', docs[1]['id'])
    assert document['chunks'][0]['body'] == MESSAGE[:1200].strip()
    assert docs[1]['sha256'] == digest(document['chunks'][0]['body'])
    assert result['output']['documents'][1]['source_hash'] == community_runtime.digest(MESSAGE)
    read = _business_read({'agent_id': community.aid}, 'lead_task_read', ref['task_id'])
    assert read['output']['evidence_refs'] == result['evidence_refs']

    evidence = [WorkResult.model_validate(result).evidence_refs[1]]
    admitted = experiment_and_content(community, experiment, evidence, variation='genuine-message')
    assert not admitted['rejected']
    content_ref = admitted['accepted'][0]['task_refs'][0]
    sent = []

    async def complete(aid, request_id):
        with tx(aid) as c:
            request = one(c, 'SELECT payload FROM lead.local_executor_requests WHERE id=%s', (request_id,))
        prompt = json.loads(request['payload']['input'])['task']
        sent.append(prompt)
        assert prompt['sources'][0]['body'] == MESSAGE[:1200].strip()
        assert prompt['sources'][0]['trust_state'] == 'untrusted_source'
        draft = {'body': 'Consider a private invitation.', 'claim_checks': []}
        return {'result_json': json.dumps(draft)} if request['payload']['result_codec'] == 'json_string_v1' else draft

    monkeypatch.setattr(local_executor_client, 'await_request', complete)
    with service_scope():
        drafted = await runtime.execute(runtime.advance(content_ref)['claim'])
    assert drafted['outcome'] == 'completed' and len(sent) == 1
    with tx(community.aid) as c:
        assert one(c, 'SELECT state FROM lead.growth_experiments WHERE id=%s', (admitted['accepted'][0]['experiment_id'],))['state'] == 'active'
        assert digest(c.execute('SELECT id,source_ref,source_type,sha256,trust_state,created_at FROM lead.knowledge_documents ORDER BY source_ref').fetchall()) == before


async def test_identical_text_metadata_and_case_changes_never_refresh_documents(community, monkeypatch):
    connected(community.aid)
    original = source(community)
    native_rpc(community, monkeypatch)
    ref, claim, first = await inspect(community, original)
    runtime.finish(claim, first)
    with tx(community.aid) as c:
        before = digest(c.execute('SELECT * FROM lead.knowledge_documents ORDER BY id').fetchall())
        saved = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (original['task_id'],))['result']
        saved['output']['output']['groups'][0].update(canonical_url=URL.upper().replace('HTTPS://T.ME/', 'https://t.me/'), subtitle='@PRIVACY_FIXTURE, 999 members')
        c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(saved), original['task_id']))
    experiment = exhausted(community)
    updated = native_observation()
    updated.update(title='Updated displayed title', members=999)
    native_rpc(community, monkeypatch, observation=updated)
    _, _, repeated = await inspect(community, original)
    assert repeated['outcome'] == 'completed' and repeated['evidence_refs'] == first['evidence_refs']
    assert repeated['output']['knowledge_progress'] is False
    with tx(community.aid) as c:
        assert digest(c.execute('SELECT * FROM lead.knowledge_documents ORDER BY id').fetchall()) == before
    assert experiment_and_content(community, experiment, WorkResult.model_validate(repeated).evidence_refs,
        variation='same-text')['rejected'][0]['reason'] == 'no_progress_path_exhausted'


@pytest.mark.parametrize('description,messages,documents', [('', False, 0), ('Privacy tools', False, 0), ('', True, 1)])
async def test_metadata_only_is_not_fresh_but_actual_message_is_retained(community, monkeypatch, description, messages, documents):
    connected(community.aid)
    experiment = exhausted(community)
    native_rpc(community, monkeypatch, observation=native_observation(description=description, messages=messages))
    _, _, result = await inspect(community, source(community))
    assert result['outcome'] == 'completed' and len(result['evidence_refs']) == documents
    assert result['output']['knowledge_progress'] == bool(documents)
    assert result['output']['group']['rules_status'] == 'unresolved'
    with tx(community.aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == documents
    admission = experiment_and_content(community, experiment, WorkResult.model_validate(result).evidence_refs, variation='current-read')
    assert bool(admission['accepted']) == bool(documents)


async def test_observation_before_closed_review_cannot_become_fresh_by_late_ingestion(community, monkeypatch):
    connected(community.aid)
    closed = []
    native_rpc(community, monkeypatch, after=lambda response: closed.append(exhausted(community)))
    _, _, result = await inspect(community, source(community))
    assert result['outcome'] == 'completed' and len(result['evidence_refs']) == 2
    admission = experiment_and_content(community, closed[0], WorkResult.model_validate(result).evidence_refs, variation='late-ingestion')
    assert admission['rejected'][0]['reason'] == 'no_progress_path_exhausted'


@pytest.mark.parametrize('change', ['pause', 'revoke', 'route', 'source', 'config', 'lease', 'old', 'future', 'missing_actual_ref',
    'raw_message_url', 'tampered_message_url', 'temporary_message_mid', 'ephemeral_message_mid'])
async def test_await_drift_or_noncurrent_observation_cannot_save_any_document(community, monkeypatch, change):
    original = source(community)

    def alter(response):
        if change == 'pause':
            set_intent(community.aid, community.account['id'], 'paused', source='owner_command', source_ref='fixture:pause-live-read')
        elif change == 'route':
            register(community.aid, community.account['profile_id'], executor_ref='changed-browser')
        elif change in ('old', 'future'):
            response['group']['observed_at'] = (utcnow()+timedelta(days=1 if change == 'future' else -1)).isoformat()
        elif change == 'missing_actual_ref':
            response.pop('execution_ref')
        elif change in ('raw_message_url', 'tampered_message_url', 'temporary_message_mid', 'ephemeral_message_mid'):
            message = response['group']['messages'][0]
            if change == 'raw_message_url':
                message['parent_url'] = URL+'/'+message['mid']
            elif change == 'tampered_message_url':
                message['parent_url'] = URL+'/11'
            else:
                message['mid'] = message['mid']+'.0001' if change == 'temporary_message_mid' else str((2 << 32)+10)
        else:
            with tx(community.aid) as c:
                if change == 'revoke':
                    c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s", (community.card['id'],))
                elif change == 'config':
                    c.execute('UPDATE lead.agents SET config_revision=config_revision+1 WHERE id=%s', (community.aid,))
                elif change == 'lease':
                    c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',%s) WHERE id=%s",
                        (j((utcnow()-timedelta(seconds=1)).isoformat()), response['inspection_task_id']))
                elif change == 'source':
                    saved = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (original['task_id'],))['result']
                    saved['output']['output']['groups'][0]['title'] = 'Source changed during await'
                    c.execute('UPDATE lead.tasks SET result=%s WHERE id=%s', (j(saved), original['task_id']))

    async def read(*args, **kwargs):
        return native_observation()

    async def rpc(path, body):
        response = await community_runtime.growth_community_step(community.actor, body['task_id'], 'inspect', body['growth_claim'],
            execution_ref=community.actor.execution_ref)
        response['inspection_task_id'] = body['task_id']
        alter(response)
        return response

    monkeypatch.setattr(community_runtime, 'observe_group', read)
    monkeypatch.setattr(activities, 'browser_rpc', rpc)
    ref, claim, result = await inspect(community, original)
    assert result['outcome'] == 'failed'
    permanent = change in ('route', 'source', 'missing_actual_ref', 'raw_message_url', 'tampered_message_url',
        'temporary_message_mid', 'ephemeral_message_mid')
    if permanent:
        runtime.finish(claim, result)
    with tx(community.aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0
        if permanent:
            saved = one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (ref['task_id'],))
            assert saved['state'] == 'failed' and not saved['result']['runtime'].get('recovery')
            assert saved['result']['runtime']['attempts'] == 1


async def test_inspect_without_execution_ref_keeps_original_direct_caller_compatibility(community, monkeypatch):
    ref = admitted_inspect(community, source(community))
    claim = runtime.advance(ref)['claim']

    async def read(*args, **kwargs):
        return native_observation()

    monkeypatch.setattr(community_runtime, 'observe_group', read)
    result = await community_runtime.growth_community_step(community.actor, ref['task_id'], 'inspect', claim)
    assert result['group']['group_id'] == PEER and 'execution_ref' not in result
    with tx(community.aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.knowledge_documents')['n'] == 0
