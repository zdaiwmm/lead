"""Real owner API rechecks the current native challenge before control resumes."""
import httpx
from fastapi.testclient import TestClient
from lead_app import account_connection, api
from lead_app.db import all_rows, one, tx
from lead_app.security import token
from test_native_condition_wake import control, waiting_connection


def native_owner(aid):
    ref, connection, owner, claim = waiting_connection(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.takeover_sessions SET connection_data=connection_data || '{\"site_url\":\"https://person.example.test/login\"}' WHERE id=%s", (connection['id'],))
    return ref, connection, owner, claim


def test_unpolled_changed_challenge_rejects_old_resume_then_reads_current_db_version(growth_agent, monkeypatch):
    aid = growth_agent
    ref, connection, owner, _ = native_owner(aid)
    calls = []
    async def observe(identifier, profile, path, body=None, **_kwargs):
        assert identifier == aid and str(profile) == str(connection['profile_id'])
        assert path == 'native/command'
        assert body['operation'] == 'owner_status' and body['run_id'] == str(connection['id'])
        assert body['args'] == {} and body['command_id']
        calls.append(path)
        if len(calls) == 1:
            with tx(aid) as c:
                c.execute('UPDATE lead.takeover_sessions SET challenge_version=challenge_version+1 WHERE id=%s', (connection['id'],))
        return {'challenge_version': 999999}  # Worker DTO cannot replace canonical DB state.
    monkeypatch.setattr(account_connection, 'rpc', observe)
    with tx(aid) as c:
        before = {str(row['id']) for row in all_rows(c, "SELECT id FROM lead.outbox_events WHERE event_type='growth.work'")}
    stale = control(aid, connection, owner, 'resume')
    assert stale.status_code == 409
    with tx(aid) as c:
        current = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection['id'],))
        assert current['control_mode'] == 'owner' and current['challenge_version'] == owner['challenge_version']+1
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'waiting_user'
        assert {str(row['id']) for row in all_rows(c, "SELECT id FROM lead.outbox_events WHERE event_type='growth.work'")} == before
    with TestClient(api.app) as client:
        client.cookies.set('lead_session', token('owner'))
        status = client.get(f"/api/agents/{aid}/connections/{connection['id']}")
    assert status.status_code == 200 and status.json()['challenge_version'] == current['challenge_version']
    assert len(calls) == 2
    assert control(aid, connection, current, 'resume').status_code == 200
    assert len(calls) == 3


def test_cancel_ends_original_attempt_even_if_native_browser_cannot_be_observed(growth_agent, monkeypatch):
    aid = growth_agent
    ref, connection, owner, _ = native_owner(aid)
    async def forbidden(*_args, **_kwargs):
        raise AssertionError('Cancel must not depend on a reachable browser')
    monkeypatch.setattr(account_connection, 'rpc', forbidden)
    assert control(aid, connection, owner, 'cancel').status_code == 200
    with tx(aid) as c:
        current = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection['id'],))
        assert current['state'] == 'ended' and current['encrypted_login'] is None
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'canceled'


def test_unavailable_native_observer_does_not_resume_or_wake_the_task(growth_agent, monkeypatch):
    aid = growth_agent
    ref, connection, owner, _ = native_owner(aid)
    async def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError('synthetic unavailable native manager')
    monkeypatch.setattr(account_connection, 'rpc', unavailable)
    response = control(aid, connection, owner, 'resume')
    assert response.status_code == 503
    with tx(aid) as c:
        current = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection['id'],))
        assert current['control_mode'] == 'owner' and current['challenge_version'] == owner['challenge_version']
        assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state'] == 'waiting_user'
