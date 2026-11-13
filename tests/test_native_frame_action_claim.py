"""An Action keeps the canonical Task claim of its derived public-frame role.

Original Action/attempt/Connection rows, prepare_binding, Action.start and frame
canonical_roles are real. Browser creation/private-self DOM/baseline DOM are the
explicit I/O seams. This does not claim a browser or upstream publication run.
"""
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app.db import j, one, tx
from lead_app.growth.contracts import utcnow
from lead_app.native_action import NativeAction
from lead_app.native_frame_roles import canonical_roles, digest
from lead_app.native_task import NativeTask
from test_browser_action_transactions import native_work
from test_growth_account_connection import login_work


@pytest.mark.asyncio
@pytest.mark.parametrize('stale', [None, 'token', 'task_generation'])
async def test_action_start_preserves_current_task_claim_for_approved_frame(growth_agent, monkeypatch, stale):
    actor, action, claim, *_ = native_work(growth_agent, provider='chromium')
    original_claim = copy.deepcopy(claim)
    method = action['intent']['browser_method']
    _, connection, _ = login_work(growth_agent, method['account_id'])
    runtime = SimpleNamespace(actor=actor, execution_ref=copy.deepcopy(action['intent']['execution_ref']),
        pages={}, bridge=SimpleNamespace(url='http://127.0.0.1:1', headers={}))
    actor.native_runtime = runtime
    scope = {'target_id': 'original-editor', 'task_id': str(action['task_id']),
        'task_generation': claim['ref']['task_generation'], 'claim': {'attempt_token': claim['attempt_token']},
        'generation': actor.epoch, 'account_id': method['account_id'],
        'control_version': action['expected_control_version'], 'binding_version': action['intent']['binding_version'],
        'intent_revision': action['intent']['intent_revision'], 'origins': [method['target_url']]}
    source = {'state': 'approved', 'candidate_id': 'a'*64, 'resource_url': 'https://public.fixture/reference',
        'parent_url': method['target_url'], 'expires_at': (utcnow()+timedelta(minutes=10)).isoformat(),
        'profile_id': str(actor.profile['id']), 'execution_ref': runtime.execution_ref,
        'bound': {'account_id': method['account_id'], 'binding_version': scope['binding_version'],
            'intent_revision': scope['intent_revision'], 'control_version': scope['control_version'],
            'browser_session_id': str(actor.session), 'browser_epoch': actor.epoch}}
    source['source_proof'] = digest(source)
    role = {key: source[key] for key in ('source_proof','candidate_id','resource_url','parent_url','expires_at')}
    role.update({key:copy.deepcopy(scope[key]) for key in ('task_id','task_generation','claim','generation')})
    scope['frame_roles'] = [role]
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now(),completed_account_id=%s,connection_data=connection_data||%s WHERE id=%s",
            (method['account_id'], j({'read_dependencies':[source]}), connection['id']))
    observations = []
    async def view(target):
        assert target == scope['target_id']
        with tx(growth_agent) as c:
            checked = canonical_roles(c, runtime, scope)
        observations.append(copy.deepcopy(checked))
        return {'target_id': target, 'origins': list(scope['origins'])}
    runtime.scope_view = view
    async def editor_start(controller, url=None):
        controller.lease = scope
        controller.page = SimpleNamespace(url=method['target_url'])
        runtime.pages[scope['target_id']] = {'scope': scope, 'page': controller.page}
        return await controller.descriptor()
    async def private_self(controller):
        return {'external_account_id':'verified-by-browser-boundary'}
    async def baseline(controller):
        with tx(growth_agent) as c:
            attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
            assert attempt['attempt'] == 1 and attempt['observation']['browser_request']['claimed'] is False
        if stale == 'token':
            role['claim'] = {'attempt_token':'stale-task-claim'}
        elif stale == 'task_generation':
            role['task_generation'] += 1
    monkeypatch.setattr(NativeTask, 'start', editor_start)
    monkeypatch.setattr(NativeAction, 'verify_private_self', private_self)
    monkeypatch.setattr(NativeAction, 'baseline', baseline)
    controller = NativeAction(actor, action['id'], claim)
    if stale:
        with pytest.raises(ValueError, match='frame_source_expired'):
            await controller.start()
        assert len(observations) == 1
    else:
        await controller.start()
        assert len(observations) == 2
        assert controller.lease['claim'] == {'attempt_token': claim['attempt_token']}
        assert role['claim'] == controller.lease['claim']
        assert controller.claim == original_claim and claim == original_claim
        assert controller.lease['action']['id'] == action['id']
    with tx(growth_agent) as c:
        final = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action['id'],))
        attempts = c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],)).fetchall()
        assert final['state'] == 'prepared' and final['intent'] == action['intent']
        assert len(attempts) == 1 and attempts[0]['attempt'] == 1
        assert attempts[0]['observation']['browser_request']['claimed'] is False
        assert 'browser_transport' not in attempts[0]['observation']
