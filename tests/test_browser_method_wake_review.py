"""Independent narrow C16 lock-order and admission fences, with real SQL."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from lead_app.config import database_kwargs
from lead_app.db import j, one, tx
from lead_app.growth.runtime import wake_browser_method
from test_browser_method_recovery import corrected_prepared


def test_existing_transport_observation_cannot_be_repaired_or_woken(growth_agent):
    aid = growth_agent
    _, ref, action, _, _, _ = corrected_prepared(aid)
    with tx(aid) as c:
        c.execute('UPDATE lead.action_attempts SET observation=observation || %s WHERE action_id=%s',
                  (j({'browser_transport': {'status': 502, 'error': 'transport_unknown'}}), action['id']))
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    assert wake_browser_method(aid, action['target_id'], 'publish') == []
    with tx(aid) as c:
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],)) == before


@pytest.mark.parametrize(('producer', 'recovery_action'), [
    ('budget', 'configuration'), ('home_release_maintenance', 'recheck'), ('channel_adapter', 'owner')])
def test_method_change_preserves_unrelated_budget_maintenance_or_owner_wait(growth_agent, monkeypatch, producer, recovery_action):
    import test_browser_method_recovery as fixtures
    original = fixtures.finish

    def finish_with_wait(claim, result):
        if result.outcome == 'waiting':
            result = result.model_copy(update={'wait': result.wait.model_copy(update={
                'producer': producer, 'recovery_action': recovery_action})})
        return original(claim, result)

    monkeypatch.setattr(fixtures, 'finish', finish_with_wait)
    _, ref, action, _, _, _ = corrected_prepared(growth_agent)
    with tx(growth_agent) as c:
        before = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
    assert wake_browser_method(growth_agent, action['target_id'], 'publish') == []
    with tx(growth_agent) as c:
        assert one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],)) == before


def test_method_wake_does_not_deadlock_the_actual_lease_then_session_heartbeat(growth_agent):
    aid = growth_agent
    account, ref, action, actor, _, _ = corrected_prepared(aid)
    lease_held, continue_heartbeat = threading.Event(), threading.Event()

    def heartbeat():
        # Exactly Gateway.maintain's two UPDATEs, with the pause only exposing
        # its legitimate lease-before-session interleaving.
        with tx(aid, pooled=False) as c:
            c.execute("SET LOCAL statement_timeout='5s'")
            c.execute("UPDATE lead.profile_leases SET expires_at=now()+interval '90 seconds' WHERE epoch=%s AND profile_id=%s AND state='held'",
                      (actor.epoch, account['profile_id']))
            lease_held.set()
            assert continue_heartbeat.wait(5)
            c.execute('UPDATE lead.browser_sessions SET heartbeat_at=now() WHERE id=%s', (actor.session,))

    failures, results = [], []
    session_locked_before_lease = False
    with ThreadPoolExecutor(max_workers=2) as workers:
        pulse = workers.submit(heartbeat)
        assert lease_held.wait(3)
        waking = workers.submit(wake_browser_method, aid, action['target_id'], 'publish')
        try:
            blocked = False
            with psycopg.connect(**database_kwargs(True)) as observer:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    observer.execute('SELECT pg_stat_clear_snapshot()')
                    if observer.execute("""SELECT 1 FROM pg_stat_activity WHERE datname=current_database()
                        AND query LIKE %s AND wait_event_type='Lock'""",
                        ('%lead.profile_leases%',)).fetchone():
                        blocked = True
                        break
                    time.sleep(0.02)
            assert blocked, 'The method wake did not reach the held heartbeat lease'
            # While wake is blocked on the earlier lease, it must not already
            # own the later session row. NOWAIT is a real independent lock
            # probe; release it before allowing the heartbeat to continue.
            try:
                with psycopg.connect(**database_kwargs(True)) as probe:
                    probe.execute('SELECT id FROM lead.browser_sessions WHERE id=%s FOR NO KEY UPDATE NOWAIT', (actor.session,))
            except psycopg.errors.LockNotAvailable:
                session_locked_before_lease = True
        finally:
            continue_heartbeat.set()
        for future in (pulse, waking):
            try:
                results.append(future.result(timeout=8))
            except Exception as error:
                failures.append(error)
    assert not session_locked_before_lease and not failures, {
        'session_locked_before_lease': session_locked_before_lease,
        'failures': [repr(error) for error in failures],
    }
    assert results == [None, [ref]]
