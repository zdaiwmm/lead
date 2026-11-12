"""C16 current-producer correction versus immutable prepared/unknown intentions.

Real isolated PostgreSQL, derive_target, TaskRef leases, prepare/claim and ledger.
The RPC replacement runs the real SQL prepare/claim gates, mapping rejection to
the real manager's HTTP 409 contract. It never performs a network send.
"""

import copy
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from lead_app import activities, budget
from lead_app.account_connection import derive_target
from lead_app.browser_actions import (
    claim_request,
    mark_untrusted,
    method_hash,
    prepare_binding,
)
from lead_app.db import j, one, tx, uid
from lead_app.execution_routes import resolve
from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from lead_app.growth.runtime import _resource_condition, advance, execute, finish
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import prepare_channel
from test_growth_channel_operation import ChannelInput
from test_native_method_transactions import observed_method


def complete_discovery(aid, observed):
    producer = observed['producer_scope']
    finish({'ref': {'agent_id': aid, 'task_id': producer['task_id'],
                    'task_generation': producer['task_generation']},
            'attempt_token': producer['attempt_token'], 'control_version': producer['control_version']},
           WorkResult(outcome='completed'))


def prepare_original(aid, *, bind=True):
    account, observed = observed_method(aid)
    target = derive_target(aid, account, observed)
    complete_discovery(aid, observed)
    with tx(aid) as c:
        body = {'body': 'Synthetic C16 original immutable content', 'title': '', 'format': 'text', 'media': []}
        content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""", (aid, j(body), digest(body)))
        data = ChannelInput(content_id=content['id'], content_hash=content['content_hash'])
        spec = TaskSpec(kind='channel', purpose='publish', account_id=account['id'], target_id=target['target_id'],
                        payload=data.model_dump(mode='json'), review_at=utcnow() + timedelta(hours=1))
        ref = submit_work(c, aid, spec)
    claim = advance(ref)['claim']
    action = prepare_channel(aid, ref['task_id'], data)
    with tx(aid) as c:
        session = one(c, 'SELECT * FROM lead.browser_sessions WHERE profile_id=%s AND ended_at IS NULL', (account['profile_id'],))
    actor = SimpleNamespace(aid=aid, profile={'id': account['profile_id']}, epoch=session['epoch'], session=session['id'])
    if bind:
        prepare_binding(actor, action['id'], claim, actor.epoch)
    token = budget.growth_cost_context.set({'task_id': str(ref['task_id']), 'experiment_id': None})
    try:
        reservation = budget.reserve(aid, uid(), 100, 'test-model', 'test')
        budget.settle(aid, reservation, actual=17, details={'purpose': 'synthetic_C16_prior_investment'})
    finally:
        budget.growth_cost_context.reset(token)
    return account, observed, ref, spec, data, action, actor, claim


def correct_current_method(aid, account, observed, mode):
    with tx(aid) as c:
        work = submit_work(c, aid, TaskSpec(kind='account', purpose='discover', account_id=account['id'],
                dedupe_key='actual-method-correction-' + uuid4().hex, review_at=utcnow() + timedelta(hours=1)))
    claim = advance(work)['claim']
    corrected = copy.deepcopy(observed)
    with tx(aid) as c:
        intent = one(c, 'SELECT revision FROM lead.account_operating_intents WHERE account_id=%s', (account['id'],))
        session = one(c, "SELECT id,epoch FROM lead.browser_sessions WHERE profile_id=%s AND state='ready' AND ended_at IS NULL", (account['profile_id'],))
    corrected['producer_scope'] = {'task_id': str(work['task_id']), 'task_generation': work['task_generation'],
        'attempt_token': claim['attempt_token'], 'control_version': claim['control_version'],
        'intent_revision': intent['revision'], 'execution_ref': resolve(aid, profile_id=account['profile_id']).model_dump(mode='json'),
        'browser_session_id': str(session['id']), 'browser_epoch': session['epoch']}
    corrected['browser_method']['selectors']['submit'] = '#current-submit-v2'
    corrected['evidence_refs'] = ['fixture:current-corrected-dom']
    current = derive_target(aid, account, corrected)
    if mode == 'grant_rotation':
        reply = copy.deepcopy(corrected)
        reply['browser_method']['operations'] = ['reply']
        reply['browser_method']['selectors']['editor_parent'] = '#parent'
        reply['browser_method']['request'].update(url=observed['canonical_url'] + '/replies', parent_field='parent')
        current = derive_target(aid, account, reply)
    complete_discovery(aid, corrected)
    return current, corrected


def resource_snapshot(aid, ref):
    with tx(aid) as c:
        agent = one(c, 'SELECT * FROM lead.agents WHERE id=%s', (aid,))
        task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        resource, condition = _resource_condition(c, agent, task, TaskSpec.model_validate(task['input']['spec']))
        periods = c.execute('SELECT id,reserved_units,spent_units FROM lead.budget_periods ORDER BY id').fetchall()
        events = c.execute("SELECT id FROM lead.outbox_events WHERE event_type='growth.work' AND payload->>'task_id'=%s", (str(ref['task_id']),)).fetchall()
    return {'resource': resource, 'condition': condition, 'task': task, 'budget': periods, 'work_events': len(events)}


@pytest.mark.parametrize('mode', ['method_only', 'grant_rotation'])
async def test_method_recovery_preserves_original_intention_and_authority(growth_agent, monkeypatch, mode):
    aid = growth_agent
    account, observed, ref, spec, data, action, actor, claim = prepare_original(aid)
    due = utcnow() + timedelta(minutes=10)
    finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='method_correction', producer='channel_adapter',
        check_at=due, recovery_action='recheck', episode_root=ref['task_id'], scope_ref=str(action['id']))))
    before = resource_snapshot(aid, ref)
    with tx(aid) as c:
        original_attempt = one(c, 'SELECT id FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
    current, corrected = correct_current_method(aid, account, observed, mode)
    after = resource_snapshot(aid, ref)
    immediate = advance(ref)
    errors = []
    claimed = []

    def has_method(value, method):
        if value == method:
            return True
        if isinstance(value, dict):
            return any(has_method(item, method) for item in value.values())
        if isinstance(value, list):
            return any(has_method(item, method) for item in value)
        return False

    async def sql_manager_rpc(path, payload=None):
        assert path == f'/agents/{aid}/publish'
        assert str(payload['action_id']) == str(action['id'])
        try:
            prepare_binding(actor, action['id'], payload['growth_claim'], actor.epoch)
        except ValueError as error:
            errors.append(str(error))
            # browser.auth masks an English ValueError as a 409. The real
            # activities.browser_rpc then raises HTTPStatusError; preserve it.
            request = httpx.Request('POST', 'http://127.0.0.1:1' + path)
            response = httpx.Response(409, request=request, json={'detail': '浏览器状态不允许此操作', 'code': None})
            response.raise_for_status()
        method = corrected['browser_method']
        request = {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
                   'postData': json.dumps({'body': action['intent']['body']})}
        observed = {'subject': account['external_account_id'], 'target_url': method['target_url'],
                    'body': action['intent']['body']}
        assert claim_request(actor, action['id'], payload['growth_claim'], actor.epoch, request, observed)
        claimed.append(str(action['id']))
        # Claiming the original SQL request is enough for this contract. The
        # synthetic boundary supplies no receipt and never calls an upstream.
        return {'state': 'unknown'}

    monkeypatch.setattr(activities, 'browser_rpc', sql_manager_rpc)
    if mode == 'method_only':
        assert after['condition'] != before['condition']
        assert after['work_events'] == before['work_events'] + 1
        assert immediate['state'] == 'ready', immediate
        assert current['grant_id'] == str(action['grant_id'])
        admitted_claims = [immediate]
    else:
        admitted_claims = None
    attempts = []
    for index in range(1 if mode == 'method_only' else 3):
        admitted = admitted_claims[index] if admitted_claims else advance(ref, now=due + timedelta(seconds=40 * index))
        assert admitted['state'] == 'ready', admitted
        same = prepare_channel(aid, ref['task_id'], data)
        assert same['id'] == action['id'] and same['intent'] == action['intent']
        result = WorkResult.model_validate(await execute(admitted['claim']))
        finish(admitted['claim'], result)
        attempts.append(result.model_dump(mode='json'))
    final = resource_snapshot(aid, ref)
    with tx(aid) as c:
        again = submit_work(c, aid, spec.model_copy(update={'dedupe_key': 'new-proposal-after-correction'}))
        actual_action = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action['id'],))
        original_grant = one(c, 'SELECT id,revoked_at FROM lead.authorization_grants WHERE id=%s', (action['grant_id'],))
        bindings = c.execute('SELECT id,attempt,observation FROM lead.action_attempts WHERE action_id=%s', (action['id'],)).fetchall()
        binding = bindings[0]
    facts = {'mode': mode, 'original_ref': ref, 'new_proposal_ref': again, 'before': before, 'after': after,
             'immediate_admission': immediate, 'current_derived_target': current,
             'old_selector': action['intent']['browser_method']['selectors']['submit'],
             'new_selector': corrected['browser_method']['selectors']['submit'],
             'original_grant': original_grant, 'gate_errors': errors, 'worker_results': attempts,
             'final': final, 'action_state': actual_action['state'], 'binding': binding}
    assert final['budget'] == before['budget'] and again == ref
    assert len(bindings) == 1 and binding['attempt'] == 1 and binding['id'] == original_attempt['id']
    assert actual_action['intent'] == action['intent'] and actual_action['grant_id'] == action['grant_id']
    assert final['task']['input']['task_generation'] == before['task']['input']['task_generation']
    assert final['task']['input']['spec']['review_at'] == before['task']['input']['spec']['review_at']
    if mode == 'grant_rotation':
        # Section 6.1.2 forbids revocation/regrant from reviving an old Action.
        # This authority change is a safety control, not permission to rebind.
        assert original_grant['revoked_at'] and errors == ['target_not_permitted'] * 3
        assert final['task']['state'] == 'failed' and not claimed
        assert not binding['observation']['browser_request']['claimed']
    else:
        assert not errors, facts
        assert claimed == [str(action['id'])] and binding['observation']['browser_request']['claimed']
        assert not original_grant['revoked_at'] and attempts[0]['outcome'] == 'unknown'
        assert has_method(binding['observation'], corrected['browser_method'])
        assert has_method(binding['observation'], action['intent']['browser_method'])
        assert binding['observation']['browser_request']['method_hash'] == method_hash(corrected['browser_method'])
        assert has_method(binding['observation'], method_hash(action['intent']['browser_method']))
        assert final['task']['result']['runtime']['attempts'] == before['task']['result']['runtime']['attempts'] + 1
        assert final['task']['state'] == 'reconciling'
        with pytest.raises(ValueError):
            prepare_binding(actor, action['id'], immediate['claim'], actor.epoch)
        with tx(aid) as c:
            assert one(c, 'SELECT observation FROM lead.action_attempts WHERE id=%s', (binding['id'],))['observation'] == binding['observation']


@pytest.mark.parametrize('old_state', ['dispatching', 'unknown'])
def test_unknown_original_cannot_borrow_corrected_method_to_resend(growth_agent, old_state):
    aid = growth_agent
    account, observed, ref, spec, data, action, actor, claim = prepare_original(aid)
    method = action['intent']['browser_method']
    request = {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
               'postData': json.dumps({'body': action['intent']['body']})}
    facts = {'subject': account['external_account_id'], 'target_url': method['target_url'], 'body': action['intent']['body']}
    assert claim_request(actor, action['id'], claim, actor.epoch, request, facts)
    finish(claim, WorkResult(outcome='unknown'))
    current, _ = correct_current_method(aid, account, observed, 'grant_rotation')
    if old_state == 'unknown':
        assert mark_untrusted(actor, actor.epoch) >= 1
    admitted = advance(ref, now=utcnow() + timedelta(minutes=6))
    assert admitted['state'] == 'ready'
    same = prepare_channel(aid, ref['task_id'], data)
    assert same['id'] == action['id'] and same['state'] == old_state
    assert not claim_request(actor, action['id'], admitted['claim'], actor.epoch, request, facts)
    with tx(aid) as c:
        again = submit_work(c, aid, spec.model_copy(update={'dedupe_key': 'must-not-resend-unknown'}))
        count = one(c, 'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s', (action['id'],))['n']
    assert again == ref and count == 1
    assert current['grant_id'] != str(action['grant_id'])


def test_unchanged_prepared_method_can_resume_after_original_wait(growth_agent):
    aid = growth_agent
    _, _, ref, _, data, action, actor, claim = prepare_original(aid)
    due = utcnow() + timedelta(minutes=10)
    finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='method_correction', producer='channel_adapter',
        check_at=due, recovery_action='recheck', episode_root=ref['task_id'], scope_ref=str(action['id']))))
    admitted = advance(ref, now=due)
    assert admitted['state'] == 'ready'
    same = prepare_channel(aid, ref['task_id'], data)
    assert same['id'] == action['id']
    prepare_binding(actor, action['id'], admitted['claim'], actor.epoch)
    method = action['intent']['browser_method']
    request = {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
               'postData': json.dumps({'body': action['intent']['body']})}
    with tx(aid) as c:
        account = one(c, 'SELECT external_account_id FROM lead.social_accounts WHERE profile_id=%s', (actor.profile['id'],))
    facts = {'subject': account['external_account_id'], 'target_url': method['target_url'], 'body': action['intent']['body']}
    assert claim_request(actor, same['id'], admitted['claim'], actor.epoch, request, facts)
