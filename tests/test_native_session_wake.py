"""A real session generation allows a bounded original recheck, never new authority."""
from datetime import timedelta
from uuid import uuid4

from lead_app.browser_actions import claim_request, mark_untrusted, prepare_binding
from lead_app.db import all_rows, j, one, tx
from lead_app.growth.contracts import Wait, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from test_browser_action_transactions import native_work
from test_native_condition_wake import condition


def native_wait(aid, *, unknown=False, running=False):
    actor, action, claim, request, observed, account = native_work(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.browser_profiles SET provider='chromium' WHERE id=%s", (account['profile_id'],))
    finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='ready_to_recheck', producer='native_browser',
        recovery_action='recheck', episode_root=action['task_id'], check_at=utcnow())))
    claim = advance(claim['ref'])['claim']
    if unknown:
        prepare_binding(actor, action['id'], claim, actor.epoch)
        assert claim_request(actor, action['id'], claim, actor.epoch, request, observed)
        finish(claim, WorkResult(outcome='unknown'))
    elif not running:
        finish(claim, WorkResult(outcome='waiting', wait=Wait(reason='browser_unavailable', producer='native_browser',
            recovery_action='recheck', episode_root=action['task_id'], check_at=utcnow()+timedelta(minutes=10))))
    return actor, action, claim, account


def rotate_session(aid, actor):
    mark_untrusted(actor, actor.epoch)
    identifier = str(uuid4())
    with tx(aid) as c:
        lease = one(c, """UPDATE lead.profile_leases SET epoch=epoch+1,state='held',expires_at=now()+interval '10 minutes'
            WHERE profile_id=%s RETURNING *""", (actor.profile['id'],))
        c.execute("""INSERT INTO lead.browser_sessions(id,tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state)
            SELECT %s,tenant_id,id,%s,'fixture','fixture',%s,control_version,'ready' FROM lead.agents WHERE id=%s""",
            (identifier, actor.profile['id'], lease['epoch'], aid))
    return identifier, lease['epoch']


def events(c, ref):
    return all_rows(c, "SELECT payload FROM lead.outbox_events WHERE event_type='growth.work' AND payload->>'task_id'=%s", (ref['task_id'],))


def test_new_session_changes_condition_but_heartbeat_does_not(growth_agent):
    actor, _, claim, _ = native_wait(growth_agent)
    before = condition(growth_agent, claim['ref'])
    session, _ = rotate_session(growth_agent, actor)
    after = condition(growth_agent, claim['ref'])
    assert before != after
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.browser_sessions SET heartbeat_at=now() WHERE id=%s', (session,))
        c.execute("UPDATE lead.profile_leases SET expires_at=now()+interval '20 minutes' WHERE profile_id=%s", (actor.profile['id'],))
    assert condition(growth_agent, claim['ref']) == after


def test_current_ready_session_wakes_only_original_nonterminal_wait_once(growth_agent):
    actor, action, claim, _ = native_wait(growth_agent)
    session, epoch = rotate_session(growth_agent, actor)
    from lead_app.growth.runtime import wake_browser_session
    with tx(growth_agent) as c:
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],))
        original_events = events(c, claim['ref'])
    assert wake_browser_session(growth_agent, actor.profile['id'], actor.session, actor.epoch) == []
    assert wake_browser_session(growth_agent, actor.profile['id'], session, epoch) == [claim['ref']]
    assert wake_browser_session(growth_agent, actor.profile['id'], session, epoch) == []
    with tx(growth_agent) as c:
        current = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],))
        assert current['input'] == before['input']
        assert current['result']['runtime']['attempts'] == before['result']['runtime']['attempts']
        assert current['result']['runtime']['wait'] is None
        assert events(c, claim['ref']) == [*original_events, {'payload': claim['ref']}]
        assert not current['result']['runtime'].get('recovery_proofs')
        assert one(c, 'SELECT intent,grant_id FROM lead.actions WHERE id=%s', (action['id'],)) == {
            'intent': action['intent'], 'grant_id': action['grant_id']}
    assert advance(claim['ref'])['state'] == 'ready'


def test_ready_event_cannot_cancel_a_running_claim_or_reopen_failed_task(growth_agent):
    actor, action, claim, _ = native_wait(growth_agent, running=True)
    session, epoch = rotate_session(growth_agent, actor)
    from lead_app.growth.runtime import wake_browser_session
    with tx(growth_agent) as c:
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],))
    assert wake_browser_session(growth_agent, actor.profile['id'], session, epoch) == []
    with tx(growth_agent) as c:
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],)) == before
        runtime = before['result']['runtime']
        runtime.update(attempt_token=None, lease_until=None, reason='HTTPStatusError')
        c.execute("UPDATE lead.tasks SET state='failed',result=%s WHERE id=%s", (j({'runtime': runtime}), action['task_id']))
        failed = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],))
    assert wake_browser_session(growth_agent, actor.profile['id'], session, epoch) == []
    with tx(growth_agent) as c:
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (action['task_id'],)) == failed


def test_new_session_wakes_unknown_only_for_readonly_reconciliation_while_paused(growth_agent):
    from lead_app import domain
    from lead_app.account_intents import set_intent
    actor, action, claim, account = native_wait(growth_agent, unknown=True)
    session, epoch = rotate_session(growth_agent, actor)
    domain.set_control(growth_agent, 'pause')
    set_intent(growth_agent, account['id'], 'paused', source='owner_command', source_ref='pause-after-unknown')
    from lead_app.growth.runtime import wake_browser_session
    assert wake_browser_session(growth_agent, actor.profile['id'], session, epoch) == [claim['ref']]
    admitted = advance(claim['ref'])
    assert admitted['state'] == 'ready'
    assert admitted['claim']['operation_ref'] == str(action['id'])
    with tx(growth_agent) as c:
        assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action['id'],))['state'] == 'unknown'
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts WHERE action_id=%s', (action['id'],))['n'] == 1
        runtime = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (action['task_id'],))['result']['runtime']
        assert runtime['reconcile_only_action_id'] == str(action['id'])
        assert not runtime.get('recovery_proofs')
