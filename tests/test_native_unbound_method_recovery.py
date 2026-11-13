"""A never-opened prepared action needs its immutable original session proof."""
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app.browser_actions import claim_request, prepare_binding
from lead_app.db import one, tx
from lead_app.growth.contracts import Wait, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from test_native_method_recovery import correct_current_method, prepare_original
from test_native_session_wake import rotate_session


def unbound_wait(aid):
    account, observed, ref, _, _, action, actor, claim = prepare_original(aid, bind=False)
    due = utcnow()+timedelta(minutes=10)
    finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='method_correction', producer='channel_adapter',
        check_at=due, recovery_action='recheck', episode_root=ref['task_id'])))
    with tx(aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s', (action['id'],))['n'] == 0
        frozen = action['intent']['browser_method_producer']
        assert frozen['browser_session_id'] == str(actor.session) and frozen['browser_epoch'] == actor.epoch
    return account, observed, ref, action, actor, due


def test_unbound_original_can_use_corrected_method_only_in_its_frozen_session(growth_agent):
    aid = growth_agent
    account, observed, ref, action, actor, _ = unbound_wait(aid)
    target, corrected = correct_current_method(aid, account, observed, 'method_only')
    admitted = advance(ref)
    assert admitted['state'] == 'ready', admitted
    assert target['grant_id'] == str(action['grant_id'])
    attempt_id = prepare_binding(actor, action['id'], admitted['claim'], actor.epoch)
    method = corrected['browser_method']
    assert claim_request(actor, action['id'], admitted['claim'], actor.epoch,
        {'method': 'POST', 'url': method['request']['url'], 'hasPostData': True,
         'postData': json.dumps({'body': action['intent']['body']})},
        {'subject': account['external_account_id'], 'target_url': method['target_url'], 'body': action['intent']['body']})
    with tx(aid) as c:
        attempts = c.execute('SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],)).fetchall()
        assert len(attempts) == 1 and str(attempts[0]['id']) == attempt_id and attempts[0]['attempt'] == 1
        assert attempts[0]['observation']['method_repair']['original_method'] == action['intent']['browser_method']
        assert attempts[0]['observation']['effective_method'] == method
        actual = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action['id'],))
        assert actual['intent'] == action['intent'] and actual['grant_id'] == action['grant_id']
        assert one(c, 'SELECT sum(spent_units) spent FROM lead.budget_periods')['spent'] == 17


def test_unbound_original_cannot_borrow_a_new_session_to_repair_its_method(growth_agent):
    aid = growth_agent
    account, observed, ref, action, actor, due = unbound_wait(aid)
    session, epoch = rotate_session(aid, actor)
    target, _ = correct_current_method(aid, account, observed, 'method_only')
    assert target['grant_id'] == str(action['grant_id'])
    assert advance(ref)['state'] == 'wait'
    admitted = advance(ref, now=due+timedelta(seconds=1))
    assert admitted['state'] == 'ready'
    replacement = SimpleNamespace(aid=aid, profile=actor.profile, session=session, epoch=epoch)
    with pytest.raises(ValueError, match='browser_method_changed'):
        prepare_binding(replacement, action['id'], admitted['claim'], epoch)
    with tx(aid) as c:
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s', (action['id'],))['n'] == 0
        assert one(c, 'SELECT intent FROM lead.actions WHERE id=%s', (action['id'],))['intent'] == action['intent']
