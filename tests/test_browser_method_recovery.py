"""A trusted method correction wakes only an untouched, continuously owned intention."""
import copy
from datetime import timedelta
from uuid import uuid4

import pytest
from lead_app.browser_actions import method_hash
from lead_app.db import all_rows, j, one, tx
from lead_app.execution_routes import resolve
from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import submit_work
from test_native_method_recovery import prepare_original


def corrected_prepared(aid, *, failed=False):
    account, observed, ref, spec, data, action, actor, claim = prepare_original(aid)
    if failed:
        for index in range(3):
            if index:
                claim = advance(ref, now=utcnow()+timedelta(minutes=index))['claim']
            finish(claim, WorkResult(outcome='failed', failure_code='HTTPStatusError'))
    else:
        finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='method_correction', producer='channel_adapter',
            recovery_action='recheck', check_at=utcnow()+timedelta(minutes=10), episode_root=ref['task_id'])))
    with tx(aid) as c:
        work = submit_work(c, aid, TaskSpec(kind='account', purpose='discover', account_id=account['id'],
            review_at=utcnow()+timedelta(hours=1), dedupe_key=uuid4().hex))
    producer_claim = advance(work)['claim']
    method = copy.deepcopy(observed['browser_method'])
    method['selectors']['submit'] = '#corrected-current-submit'
    with tx(aid) as c:
        intent = one(c, 'SELECT revision FROM lead.account_operating_intents WHERE account_id=%s', (account['id'],))
        target = one(c, 'SELECT verified_permissions FROM lead.account_targets WHERE id=%s', (action['target_id'],))
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        producer = {'task_id': str(work['task_id']), 'task_generation': work['task_generation'],
            'attempt_token': producer_claim['attempt_token'], 'control_version': producer_claim['control_version'],
            'intent_revision': intent['revision'], 'browser_session_id': str(actor.session), 'browser_epoch': actor.epoch,
            'execution_ref': resolve(aid, profile_id=account['profile_id']).model_dump(mode='json'),
            'method_hash': method_hash(method)}
        permissions = target['verified_permissions']
        permissions['browser_methods']['publish'] = method
        permissions['browser_method_producers'] = {'publish': producer}
        # This component test starts at the trusted target/proof write boundary;
        # derive_target's stricter producing-claim checks have their own tests.
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s', (j(permissions), action['target_id']))
    finish(producer_claim, WorkResult(outcome='completed'))
    return account, ref, action, actor, before, method


def work_events(c, task_id):
    return all_rows(c, "SELECT payload FROM lead.outbox_events WHERE event_type='growth.work' AND payload->>'task_id'=%s", (str(task_id),))


def test_trusted_correction_wakes_original_wait_once_and_preserves_input(growth_agent):
    aid = growth_agent
    _, ref, action, actor, before, method = corrected_prepared(aid)
    from lead_app.growth.runtime import wake_browser_method
    with tx(aid) as c:
        events = work_events(c, ref['task_id'])
    assert wake_browser_method(aid, action['target_id'], 'publish') == [ref]
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    with tx(aid) as c:
        task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert task['input'] == before['input']
        assert task['result']['runtime']['attempts'] == before['result']['runtime']['attempts']
        assert task['result']['runtime']['wait'] is None
        marker = task['result']['runtime']['browser_method_repair']
        assert marker['target_id'] == str(action['target_id']) and marker['operation'] == 'publish'
        assert marker['grant_id'] == str(action['grant_id']) and marker['task_generation'] == ref['task_generation']
        assert marker['browser_session_id'] == str(actor.session) and marker['browser_epoch'] == actor.epoch
        assert marker['method_hash'] == method_hash(method) and marker['proof_ref'].startswith('native-method:')
        assert work_events(c, ref['task_id']) == [*events, {'payload': ref}]
        assert one(c, 'SELECT intent,grant_id FROM lead.actions WHERE id=%s', (action['id'],)) == {
            'intent': action['intent'], 'grant_id': action['grant_id']}
    assert advance(ref)['state'] == 'ready'


@pytest.mark.parametrize('change', ['grant', 'binding', 'intent', 'control', 'session', 'lease',
    'producer_session', 'producer_hash', 'producer_task', 'already_repaired', 'claimed', 'transport', 'deadline', 'paused'])
def test_changed_authority_or_transport_cannot_wake_original_intention(growth_agent, change):
    aid = growth_agent
    account, ref, action, actor, before, _ = corrected_prepared(aid)
    from lead_app.growth.runtime import wake_browser_method
    if change == 'intent':
        from lead_app.account_intents import set_intent
        set_intent(aid, account['id'], 'running', source='owner_command', source_ref=uuid4().hex,
            scope={'target_scope': 'authenticated_permitted_targets', 'excluded_targets': [], 'publish_per_day': 2})
    with tx(aid) as c:
        if change == 'grant':
            c.execute('UPDATE lead.authorization_grants SET revoked_at=now() WHERE id=%s', (action['grant_id'],))
        elif change == 'binding':
            c.execute('UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s', (account['id'],))
        elif change == 'intent':
            pass
        elif change == 'control':
            c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (aid,))
        elif change == 'session':
            c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE id=%s", (actor.session,))
        elif change == 'lease':
            c.execute("UPDATE lead.profile_leases SET expires_at=now()-interval '1 second' WHERE profile_id=%s", (account['profile_id'],))
        elif change.startswith('producer_'):
            target = one(c, 'SELECT verified_permissions FROM lead.account_targets WHERE id=%s', (action['target_id'],))
            permissions = target['verified_permissions']
            proof = permissions['browser_method_producers']['publish']
            proof[{'producer_session': 'browser_session_id', 'producer_hash': 'method_hash', 'producer_task': 'task_id'}[change]] = str(uuid4())
            c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s', (j(permissions), action['target_id']))
        elif change == 'already_repaired':
            c.execute("UPDATE lead.action_attempts SET observation=observation || %s WHERE action_id=%s", (j({'method_repair': {'revision': 1}}), action['id']))
        elif change == 'claimed':
            c.execute("UPDATE lead.action_attempts SET observation=jsonb_set(observation,'{browser_request,claimed}','true') WHERE action_id=%s", (action['id'],))
        elif change == 'transport':
            c.execute("UPDATE lead.action_attempts SET outcome='unknown',ended_at=now() WHERE action_id=%s", (action['id'],))
        elif change == 'deadline':
            value = copy.deepcopy(before['input'])
            value['spec']['review_at'] = (utcnow()-timedelta(seconds=1)).isoformat()
            c.execute('UPDATE lead.tasks SET input=%s WHERE id=%s', (j(value), ref['task_id']))
        else:
            c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (aid,))
        events = work_events(c, ref['task_id'])
        task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    with tx(aid) as c:
        assert work_events(c, ref['task_id']) == events
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],)) == task


def test_terminal_method_failure_resumes_same_task_with_new_workflow_generation(growth_agent):
    aid = growth_agent
    _, ref, action, _, before, _ = corrected_prepared(aid, failed=True)
    from lead_app.growth.runtime import wake_browser_method
    assert before['state'] == 'failed'
    resumed = {**ref, 'task_generation': ref['task_generation']+1}
    assert wake_browser_method(aid, action['target_id'], 'publish') == [resumed]
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    assert advance(ref) == {'state': 'terminal', 'reason': 'stale_task'}
    with tx(aid) as c:
        task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert task['input'] == {**before['input'], 'task_generation': resumed['task_generation']}
        assert task['result']['runtime']['attempts'] == 3
        assert task['result']['runtime']['failure_attempts'] == 3
        assert task['result']['runtime']['recovery_permit'] == 1
        assert task['result']['runtime']['browser_method_repair']['task_generation'] == resumed['task_generation']
    assert advance(resumed)['state'] == 'ready'
    with tx(aid) as c:
        runtime = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result']['runtime']
        assert runtime['attempts'] == 4 and runtime['failure_attempts'] == 3 and runtime['recovery_permit'] == 0


def test_new_original_action_uses_current_control_without_requiring_a_browser_restart(growth_agent):
    # A retained browser records its launch control, while a new Action and
    # producer carry the current control after the owner resumes the project.
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s', (growth_agent,))
    _, ref, action, actor, _, _ = corrected_prepared(growth_agent)
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.browser_sessions SET control_version=control_version-1 WHERE id=%s', (actor.session,))
        assert one(c, 'SELECT control_version FROM lead.agents WHERE id=%s', (growth_agent,))['control_version'] == action['expected_control_version']
    from lead_app.growth.runtime import wake_browser_method
    assert wake_browser_method(growth_agent, action['target_id'], 'publish') == [ref]
