"""Independent effective-method consumers; no browser, model, or upstream send."""

import json
from datetime import timedelta

import pytest
from lead_app.browser_actions import claim_request, execution_action, prepare_binding
from lead_app.db import all_rows, one, require_agent, tx
from lead_app.growth.contracts import Wait, WorkResult, utcnow
from lead_app.growth.runtime import (
    _recovery_lock,
    _resume_proven,
    advance,
    finish,
    wake_browser_method,
)
from lead_app.native_action import NativeAction
from test_browser_method_recovery import corrected_prepared
from test_native_method_recovery import correct_current_method, prepare_original


def test_terminal_generation_first_repair_uses_one_original_snapshot(growth_agent):
    aid = growth_agent
    account, ref, action, actor, _, method = corrected_prepared(aid, failed=True)
    resumed = wake_browser_method(aid, action['target_id'], 'publish')[0]
    assert resumed['task_generation'] == ref['task_generation'] + 1
    claim = advance(resumed)['claim']
    prepare_binding(actor, action['id'], claim, actor.epoch)
    with tx(aid) as c:
        frozen = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action['id'],))
        attempt = one(c, 'SELECT observation FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
        assert frozen['intent'] == action['intent'] and frozen['grant_id'] == action['grant_id']
        assert execution_action(c, frozen)['intent']['browser_method'] == method
        assert attempt['observation']['method_repair']['task_generation'] == resumed['task_generation']
    request = {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
               'postData': json.dumps({'body': action['intent']['body']})}
    observed = {'subject': account['external_account_id'], 'target_url': method['target_url'], 'body': action['intent']['body']}
    assert claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    assert not claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    controller = NativeAction(actor, action['id'], claim)
    assert controller.reconcile and controller.action['intent']['browser_method'] == method


def test_already_repaired_unsent_action_accepts_its_canonical_recovered_generation(growth_agent):
    aid = growth_agent
    account, observed, ref, _, _, action, actor, claim = prepare_original(aid)
    finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='method_correction', producer='channel_adapter',
        check_at=utcnow() + timedelta(minutes=10), recovery_action='recheck', episode_root=ref['task_id'])))
    _, corrected = correct_current_method(aid, account, observed, 'method_only')
    claim = advance(ref)['claim']
    prepare_binding(actor, action['id'], claim, actor.epoch)
    first_claim = claim
    # No request has been claimed. A later temporary failure reaches the
    # existing terminal recovery boundary without another method revision.
    for index in range(3):
        if index:
            claim = advance(ref, now=utcnow() + timedelta(minutes=index))['claim']
        finish(claim, WorkResult(outcome='failed', failure_code='HTTPStatusError'))
    with tx(aid) as c:
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        original_attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
        assert before['state'] == 'failed'
        assert original_attempt['observation']['browser_request']['claimed'] is False
        assert 'browser_transport' not in original_attempt['observation']
        assert one(c, 'SELECT revoked_at FROM lead.authorization_grants WHERE id=%s', (action['grant_id'],))['revoked_at'] is None
    # A synthetic genuine-proof consumer boundary isolates generation handling;
    # this test does not claim that it discovered an external recovery proof.
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        all_rows(c, 'SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE')
        all_rows(c, 'SELECT id FROM lead.authorization_grants ORDER BY id FOR SHARE')
        row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE', (ref['task_id'],))
        episode = {**row['result']['runtime']['recovery'], 'status': 'recovered',
                   'proof_ref': 'fixture:temporary-failure-cleared',
                   'observed_condition': row['result']['runtime']['condition_revision']}
        resumed = _resume_proven(c, aid, agent, row, episode, 'fixture:temporary-failure-cleared')
    assert resumed['task_generation'] == ref['task_generation'] + 1
    admitted = advance(resumed)
    assert admitted['state'] == 'ready', admitted
    claim = admitted['claim']
    with tx(aid) as c:
        current = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert current['result']['runtime']['browser_method_repair']['task_generation'] == resumed['task_generation']
        assert execution_action(c, action)['intent']['browser_method'] == corrected['browser_method']
    method = corrected['browser_method']
    request = {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
               'postData': json.dumps({'body': action['intent']['body']})}
    observed = {'subject': account['external_account_id'], 'target_url': method['target_url'], 'body': action['intent']['body']}
    with pytest.raises(ValueError, match='stale_submission_claim'):
        claim_request(actor, action['id'], first_claim, actor.epoch, request, observed)
    prepare_binding(actor, action['id'], claim, actor.epoch)
    assert claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    assert not claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(aid) as c:
        final = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
        assert final['id'] == original_attempt['id'] and final['attempt'] == 1
        assert final['observation']['method_repair']['revision'] == 1
        assert final['observation']['method_repair']['task_generation'] == ref['task_generation']
        assert all(final['observation']['method_repair'].get(key) == value
                   for key, value in original_attempt['observation']['method_repair'].items())
        assert final['observation']['effective_method'] == original_attempt['observation']['effective_method']
        assert one(c, 'SELECT intent FROM lead.actions WHERE id=%s', (action['id'],))['intent'] == action['intent']
    controller = NativeAction(actor, action['id'], claim)
    assert controller.reconcile and controller.action['intent']['browser_method'] == method
    with pytest.raises(ValueError, match='original_action_reconcile_only'):
        prepare_binding(actor, action['id'], claim, actor.epoch)
