"""Real Linux owner OTP continuation on the original native ConnectionRef.

Only the local fixture owner supplies synthetic code 135790. The mechanical
fake model stops at OTP and never receives or answers it. No real provider or
social account is involved. QR/passkeys and challenge-refresh rotation require
separate evidence; these cases cover OTP completion, cancellation and expiry.
"""
import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timedelta

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required',
)


@pytest.mark.parametrize('exit_path', ['complete', 'cancel', 'expired'])
@pytest.mark.asyncio
async def test_owner_otp_original_connection_lifecycle(monkeypatch, exit_path):
    from lead_app import account_profiles, api, browser
    from lead_app.account_connection import begin, execute_account
    from lead_app.config import STATE
    from lead_app.connection_control import LoginInput
    from lead_app.db import all_rows, one, tx, uid
    from lead_app.growth.contracts import TaskRef, utcnow
    from lead_app.growth.runtime import advance, finish, recover_due
    from lead_app.growth.tools import AccountInput
    from lead_app.native_connection import NativeConnection
    from lead_app.security import internal_token, token
    from native_browser_fixture import BoardState, NativeBoard
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    issued = {}
    original_start = NativeConnection.start

    async def track_issued_scope(controller):
        value = await original_start(controller)
        # Capture real manager-issued scopes; never manufacture a task claim.
        issued.setdefault(controller.connection_id, copy.deepcopy(controller.lease))
        return value
    monkeypatch.setattr(NativeConnection, 'start', track_issued_scope)

    async def check(context):
        aid, pending, board = context['aid'], context['pending'], context['board']
        connection_id, task_id = str(pending['takeover_id']), str(context['task']['id'])
        profile_id = context['task']['input']['spec']['payload']['profile_id']
        actor = await browser.gateway.get(aid, profile_id, allow_challenge=True)
        controller = actor.connection_pages[connection_id]
        page, target = controller.page, controller.lease['target_id']

        def state():
            with tx(aid) as connection:
                return one(connection, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection_id,))

        def task():
            with tx(aid) as connection:
                return one(connection, 'SELECT * FROM lead.tasks WHERE id=%s', (task_id,))

        waiting = task()
        owner = state()
        version = owner['challenge_version']
        assert context['result'].outcome == 'waiting' and waiting['state'] == 'waiting_user'
        assert waiting['result']['runtime']['wait']['responsibility'] == 'owner'
        assert owner['control_mode'] == 'owner' and version > pending['challenge_version']
        assert owner['encrypted_login'] is not None and owner['completed_account_id'] is None
        assert page.url == board.origin + '/challenge' and not page.is_closed()
        assert not actor.native_runtime.pages[target]['slot_held']
        with pytest.raises(ValueError):
            actor.native_runtime.current_scope(issued[connection_id])
        assert not finish(context['claim'], context['result'])['accepted_control']

        headers = {'Authorization': 'Bearer ' + internal_token()}
        async with httpx.AsyncClient(base_url=context['gateway_endpoint'], headers=headers, timeout=30) as gateway, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8088',
                    cookies={'lead_session': token('owner')},
                    headers={'Origin': 'http://127.0.0.1:3100', 'X-Lead-Request': '1'}, timeout=30) as owner_api:
            async def owner_input(kind, *, challenge=version, **values):
                return await gateway.post(f'/agents/{aid}/input', json={
                    'takeover': connection_id, 'kind': kind, 'challenge_version': challenge, **values})

            async def control(action, challenge=version):
                return await owner_api.post(f'/api/agents/{aid}/connections/{connection_id}/control',
                    json={'challenge_version': challenge, 'action': action})

            stale = await owner_input('text', challenge=pending['challenge_version'], text='late synthetic answer')
            assert stale.status_code == 409
            old_worker = await gateway.post(f'/agents/{aid}/native/command', json={
                'run_id': connection_id, 'command_id': uid(), 'operation': 'scope', 'args': {}})
            assert old_worker.status_code == 409
            assert not any(r['method'] == 'POST' and r['path'] == '/challenge' for r in board.oracle()['requests'])

            if exit_path != 'complete':
                if exit_path == 'cancel':
                    response = await control('cancel')
                    assert response.status_code == 200, response.text
                    assert response.json()['state'] == 'ended'
                else:
                    # Shorten only the synthetic connection lifetime; let real
                    # time and the public due scheduler expire it naturally.
                    with tx(aid) as connection:
                        connection.execute('UPDATE lead.takeover_sessions SET expires_at=%s WHERE id=%s',
                                           (utcnow()+timedelta(milliseconds=100), connection_id))
                    await asyncio.sleep(.2)
                    recover_due(aid)
                    recover_due(aid)
                ended = state()
                assert ended['state'] == ('ended' if exit_path == 'cancel' else 'expired')
                assert ended['encrypted_login'] is None and ended['challenge_version'] > version
                assert (await owner_input('text', text='135790')).status_code == 409
                assert (await control('resume')).status_code == 409
                assert not await controller.reconcile()
                assert page.is_closed() and target not in actor.native_runtime.pages
                assert task()['state'] != 'succeeded' and task()['input']['task_generation'] == waiting['input']['task_generation']
                assert not any(r['method'] == 'POST' and r['path'] == '/challenge' for r in board.oracle()['requests'])
                if exit_path == 'expired':
                    with tx(aid) as connection:
                        assert len(all_rows(connection, "SELECT id FROM lead.outbox_events WHERE payload->>'reason'='connection_expired'")) == 1
                return {'exit': exit_path, 'late_input_rejected': True, 'credentials_cleared': True, 'original_page_released': True}

            # A's owner page stays open while B performs a real credential/login
            # and private-self roundtrip in the same instance on another origin.
            other = NativeBoard(BoardState(host='93.184.215.15', username='other-' + uid(), password='other-' + uid()),
                                public_origin='http://93.184.215.15').start()
            context['boards']['93.184.215.15'] = other
            context['workflow'].update(kind='login', step=0, allow_identity=True)
            second = await begin(aid, 'web', site_url=other.origin + '/login',
                                 login=LoginInput(username=other.state.username, password=other.state.password))
            with tx(aid) as connection:
                second_task = one(connection, "SELECT * FROM lead.tasks WHERE input#>>'{spec,payload,takeover_id}'=%s", (str(second['takeover_id']),))
            assert str(second_task['input']['spec']['payload']['profile_id']) == str(actor.profile['id'])
            second_ref = TaskRef(agent_id=aid, task_id=second_task['id'], task_generation=second_task['input']['task_generation'])
            second_claim = advance(second_ref)['claim']
            second_result = await asyncio.wait_for(execute_account(aid, second_task,
                AccountInput.model_validate(second_task['input']['spec']['payload'])), 100)
            assert second_result.outcome == 'completed'
            assert finish(second_claim, second_result)['accepted_control']
            with tx(aid) as connection:
                assert one(connection, 'SELECT state,external_account_id FROM lead.social_accounts WHERE id=%s',
                           (second['account_id'],)) == {'state': 'connected', 'external_account_id': other.state.username}
                assert one(connection, 'SELECT state FROM lead.tasks WHERE id=%s', (second_task['id'],))['state'] == 'succeeded'
            other_worker = json.loads((STATE/'browser-use-runs'/('connection-' + str(second['takeover_id']))/'result.json').read_text())
            assert not any(other_worker['credential_scan'].values())
            await actor.connection_pages[str(second['takeover_id'])].reconcile()
            assert controller.page is page and not page.is_closed() and state()['control_mode'] == 'owner'

            # Coordinates come from the actual visible OTP field. Only the test
            # owner sends the synthetic code through the existing InputRequest API.
            box = await page.get_by_label('One-time code').bounding_box()
            assert box
            assert (await owner_input('click', x=box['x']+box['width']/2, y=box['y']+box['height']/2)).status_code == 200
            assert (await owner_input('text', text='135790')).status_code == 200
            assert state()['challenge_version'] == version  # ordinary text is not a new challenge
            assert (await owner_input('key', text='Enter')).status_code == 200
            await page.wait_for_url(board.origin + '/home')
            assert len([r for r in board.oracle()['requests'] if r['method'] == 'POST' and r['path'] == '/challenge']) == 1
            async with asyncio.timeout(4):
                while state()['last_input_at'] > utcnow()-timedelta(seconds=2):
                    await asyncio.sleep(.05)
            resumed = await control('resume')
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()['challenge_version'] > version
            assert (await owner_input('text', text='late old challenge')).status_code == 409

            # Preserve the original Wait and its actual due time. The production
            # scheduler must emit the original TaskRef before a new issued claim.
            check_at = datetime.fromisoformat(waiting['result']['runtime']['wait']['check_at'])
            await asyncio.sleep(max(0, (check_at-utcnow()).total_seconds()) + .02)
            with tx(aid) as connection:
                previous_events = {str(row['id']) for row in all_rows(connection, "SELECT id FROM lead.outbox_events WHERE event_type='growth.work'")}
            recover_due(aid)
            with tx(aid) as connection:
                dispatched = [row for row in all_rows(connection, "SELECT * FROM lead.outbox_events WHERE event_type='growth.work'")
                              if str(row['id']) not in previous_events and row['payload']['task_id'] == task_id]
            assert dispatched
            ref = dispatched[0]['payload']
            assert ref == {'agent_id': aid, 'task_id': task_id, 'task_generation': waiting['input']['task_generation']}
            resumed_claim = advance(ref)['claim']
            assert resumed_claim['attempt_token'] != context['claim']['attempt_token']
            with pytest.raises(ValueError):
                actor.native_runtime.current_scope(issued[connection_id])
            context['workflow'].update(kind='resume', step=0)
            continued = await execute_account(aid, task(), AccountInput.model_validate(task()['input']['spec']['payload']))
            assert continued.outcome == 'completed'
            assert finish(resumed_claim, continued)['accepted_control']
            final = state()
            assert task()['state'] == 'succeeded'
            assert final['encrypted_login'] is None and str(final['completed_account_id']) == str(pending['account_id'])
            assert any(r['path'] == '/account' for r in board.oracle()['requests'])
            assert continued.output['connection_proof'] == ['native-self:' + connection_id + ':' + str(final['challenge_version'])]
            with tx(aid) as connection:
                assert one(connection, 'SELECT state,external_account_id FROM lead.social_accounts WHERE id=%s',
                           (pending['account_id'],)) == {'state': 'connected', 'external_account_id': board.state.username}
            assert not await controller.reconcile()
            assert page.is_closed() and target not in actor.native_runtime.pages
            return {'exit': exit_path, 'other_origin_completed': True, 'fresh_claim': True, 'fresh_private_self': True,
                    'same_connection': True, 'late_input_rejected': True, 'credentials_cleared': True, 'original_page_released': True}

    result = await verify_login(monkeypatch, mode='owner_otp', owner_hook=check)
    assert result['owner_check']['exit'] == exit_path
    assert result['privacy'] == {'literal_in_model_request': False, 'literal_in_workdir': False}
    assert not result['oracle']['posts']


@pytest.mark.asyncio
async def test_owner_unsubmitted_otp_keeps_person_step_without_another_model_request(monkeypatch):
    from lead_app import account_profiles, api, browser
    from lead_app.account_connection import execute_account
    from lead_app.db import all_rows, one, tx
    from lead_app.growth.contracts import utcnow
    from lead_app.growth.runtime import advance, finish, recover_due
    from lead_app.growth.tools import AccountInput
    from lead_app.security import internal_token, token
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)

    async def check(context):
        aid, pending = context['aid'], context['pending']
        connection_id, task_id = str(pending['takeover_id']), str(context['task']['id'])
        actor = await browser.gateway.get(aid, context['task']['input']['spec']['payload']['profile_id'], allow_challenge=True)
        controller = actor.connection_pages[connection_id]
        page = controller.page
        with tx(aid) as connection:
            owner = one(connection, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection_id,))
            waiting = one(connection, 'SELECT * FROM lead.tasks WHERE id=%s', (task_id,))
        assert owner['control_mode'] == 'owner' and waiting['state'] == 'waiting_user'
        assert owner['encrypted_login'] is not None
        version = owner['challenge_version']
        async with httpx.AsyncClient(base_url=context['gateway_endpoint'],
                headers={'Authorization': 'Bearer ' + internal_token()}, timeout=30) as gateway, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8088',
                    cookies={'lead_session': token('owner')},
                    headers={'Origin': 'http://127.0.0.1:3100', 'X-Lead-Request': '1'}, timeout=30) as owner_api:
            field = page.get_by_label('One-time code')
            box = await field.bounding_box()
            for values in ({'kind': 'click', 'x': box['x']+box['width']/2, 'y': box['y']+box['height']/2},
                           {'kind': 'text', 'text': '135790'}):
                response = await gateway.post(f'/agents/{aid}/input', json={
                    'takeover': connection_id, 'challenge_version': version, **values})
                assert response.status_code == 200
            assert await field.input_value() == '135790'
            assert not any(row['method'] == 'POST' and row['path'] == '/challenge' for row in context['board'].oracle()['requests'])
            # The owner may mistakenly say the person-only step is complete.
            # Keep the real input settling interval and original task timer.
            async with asyncio.timeout(4):
                while True:
                    with tx(aid) as connection:
                        latest = one(connection, 'SELECT last_input_at FROM lead.takeover_sessions WHERE id=%s', (connection_id,))
                    if latest['last_input_at'] <= utcnow()-timedelta(seconds=2):
                        break
                    await asyncio.sleep(.05)
            response = await owner_api.post(f'/api/agents/{aid}/connections/{connection_id}/control',
                json={'challenge_version': version, 'action': 'resume'})
            assert response.status_code == 200, response.text
        check_at = datetime.fromisoformat(waiting['result']['runtime']['wait']['check_at'])
        await asyncio.sleep(max(0, (check_at-utcnow()).total_seconds()) + .02)
        with tx(aid) as connection:
            before_events = {str(row['id']) for row in all_rows(connection, "SELECT id FROM lead.outbox_events WHERE event_type='growth.work'")}
        recover_due(aid)
        with tx(aid) as connection:
            dispatched = [row for row in all_rows(connection, "SELECT * FROM lead.outbox_events WHERE event_type='growth.work'")
                          if str(row['id']) not in before_events and row['payload']['task_id'] == task_id]
        assert dispatched
        claim = advance(dispatched[0]['payload'])['claim']
        assert claim['attempt_token'] != context['claim']['attempt_token']
        with tx(aid) as connection:
            task = one(connection, 'SELECT * FROM lead.tasks WHERE id=%s', (task_id,))
        before = len(context['model_calls'])
        # This fake transport can only inspect/report identity. It never submits
        # or solves an OTP, so an extra request cannot be blamed on test actions.
        context['workflow'].update(kind='resume', step=0)
        result = await execute_account(aid, task, AccountInput.model_validate(task['input']['spec']['payload']))
        assert finish(claim, result)['accepted_control']
        calls = context['model_calls'][before:]
        known = all('synthetic_otp_literal_seen' in row for row in calls)
        diagnostics = {'new_model_requests': len(calls),
                       'synthetic_otp_literal_seen': any(row.get('synthetic_otp_literal_seen') for row in calls) if known else None,
                       'challenge_post_count': sum(row['method'] == 'POST' and row['path'] == '/challenge' for row in context['board'].oracle()['requests']),
                       'result_outcome': result.outcome}
        # Only fixed synthetic-literal presence is recorded; never dump request bodies.
        print('UNSUBMITTED_OTP_DIAGNOSTICS ' + json.dumps(diagnostics, sort_keys=True))
        assert diagnostics['new_model_requests'] == 0, diagnostics
        with tx(aid) as connection:
            final = one(connection, 'SELECT control_mode FROM lead.takeover_sessions WHERE id=%s', (connection_id,))
            assert one(connection, 'SELECT state FROM lead.tasks WHERE id=%s', (task_id,))['state'] == 'waiting_user'
        assert final['control_mode'] == 'owner' and result.outcome == 'waiting'
        assert diagnostics['challenge_post_count'] == 0
        return diagnostics

    await verify_login(monkeypatch, mode='owner_otp', owner_hook=check)
