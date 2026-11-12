"""Narrow DB marker consumers; no browser, model or external recovery proof."""

from datetime import timedelta
from uuid import uuid4

from lead_app.db import all_rows, j, one, require_agent, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import (
    _recovery_lock,
    _resume_proven,
    advance,
    finish,
    wake_browser_method,
)
from lead_app.growth.store import submit_work
from test_browser_method_recovery import corrected_prepared, work_events


def test_partial_method_recovery_synchronizes_marker_when_other_dependency_reopens_task(growth_agent):
    aid = growth_agent
    _, ref, action, _, before, _ = corrected_prepared(aid, failed=True)
    other = {'resource': 'fixture:independent-resource', 'condition_revision': 'fixture:old-condition'}
    with tx(aid) as c:
        row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        runtime = row['result']['runtime']
        runtime['blocked_resources'] = [*(runtime.get('blocked_resources') or []), other]
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{runtime}',%s) WHERE id=%s", (j(runtime), ref['task_id']))
        events = work_events(c, ref['task_id'])
        budget = c.execute('SELECT id,spent_units,reserved_units FROM lead.budget_periods ORDER BY id').fetchall()
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    with tx(aid) as c:
        partial = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        assert partial['state'] == 'failed' and partial['input'] == before['input']
        assert partial['result']['runtime']['blocked_resources'] == [other]
        marker = partial['result']['runtime']['browser_method_repair']
        assert work_events(c, ref['task_id']) == events
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    # Exercise the existing last-proof consumer with a synthetic independent
    # resource proof. This tests marker generation, not proof discovery.
    with tx(aid) as c:
        _recovery_lock(c, aid)
        agent = require_agent(c, aid, True)
        all_rows(c, 'SELECT id FROM lead.social_accounts ORDER BY id FOR SHARE')
        all_rows(c, 'SELECT id FROM lead.authorization_grants ORDER BY id FOR SHARE')
        row = one(c, 'SELECT * FROM lead.tasks WHERE id=%s FOR UPDATE', (ref['task_id'],))
        resumed = _resume_proven(c, aid, agent, row, other, 'fixture:completed-independent-proof')
    expected = {**ref, 'task_generation': ref['task_generation'] + 1}
    assert resumed == expected and advance(ref) == {'state': 'terminal', 'reason': 'stale_task'}
    with tx(aid) as c:
        final = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        runtime = final['result']['runtime']
        assert final['input'] == {**before['input'], 'task_generation': expected['task_generation']}
        assert runtime['browser_method_repair'] == {**marker, 'task_generation': expected['task_generation']}
        assert runtime['attempts'] == runtime['failure_attempts'] == 3
        assert not runtime['blocked_resources']
        assert work_events(c, ref['task_id']) == [*events, {'payload': expected}]
        assert one(c, 'SELECT intent,grant_id FROM lead.actions WHERE id=%s', (action['id'],)) == {
            'intent': action['intent'], 'grant_id': action['grant_id']}
        assert c.execute('SELECT id,spent_units,reserved_units FROM lead.budget_periods ORDER BY id').fetchall() == budget
    assert advance(expected)['state'] == 'ready'


def test_fresh_completed_producer_same_actual_method_session_and_grant_does_not_wake_twice(growth_agent):
    aid = growth_agent
    account, ref, action, _, _, _ = corrected_prepared(aid)
    assert wake_browser_method(aid, action['target_id'], 'publish') == [ref]
    with tx(aid) as c:
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
        events = work_events(c, ref['task_id'])
        observed = one(c, 'SELECT verified_permissions FROM lead.account_targets WHERE id=%s', (action['target_id'],))['verified_permissions']
        work = submit_work(c, aid, TaskSpec(kind='account', purpose='discover', account_id=account['id'],
                dedupe_key='fixture:fresh-same-method:' + uuid4().hex, review_at=utcnow() + timedelta(hours=1)))
    claim = advance(work)['claim']
    producer = observed['browser_method_producers']['publish']
    producer.update(task_id=str(work['task_id']), task_generation=work['task_generation'], attempt_token=claim['attempt_token'])
    # Same trusted target/proof publication boundary as the root component
    # fixture: only producer identity changes; method, session, grant do not.
    with tx(aid) as c:
        c.execute('UPDATE lead.account_targets SET verified_permissions=%s WHERE id=%s', (j(observed), action['target_id']))
    finish(claim, WorkResult(outcome='completed'))
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    with tx(aid) as c:
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (work['task_id'],))['state'] == 'succeeded'
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],)) == before
        assert work_events(c, ref['task_id']) == events
