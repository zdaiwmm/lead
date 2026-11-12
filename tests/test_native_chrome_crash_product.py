"""One real Chrome-main-process fault across two original account task pages.

Uses synthetic fixture-A logins only. No model semantics, publication, forged
running claim or account row is involved. Another independent instance is
explicitly outside this minimal case's evidence.
"""
import asyncio
import copy
import json
import os
import signal
import sys
from datetime import timedelta
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required',
)


def descendant_processes(parent):
    found, remaining = set(), [parent]
    while remaining:
        pid = remaining.pop()
        try:
            children = (Path('/proc')/str(pid)/'task'/str(pid)/'children').read_text().split()
        except OSError:
            continue
        for value in children:
            child = int(value)
            if child not in found:
                found.add(child)
                remaining.append(child)
    return found


def command_line(pid):
    return [value for value in (Path('/proc')/str(pid)/'cmdline').read_bytes().decode().split('\0') if value]


def parent_chain(pid):
    chain = []
    while pid > 1:
        chain.append(pid)
        status = (Path('/proc')/str(pid)/'status').read_text().splitlines()
        pid = int(next(line.split(':', 1)[1] for line in status if line.startswith('PPid:')))
    return chain


@pytest.mark.asyncio
async def test_real_chrome_main_failure_revokes_two_shared_original_task_pages(monkeypatch):
    from lead_app import account_profiles
    from lead_app.db import all_rows, one, tx, uid
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance
    from lead_app.growth.store import submit_work
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)

    async def check(context):
        aid, actor = context['aid'], context['actor']
        runtime = actor.native_runtime
        account_a = str(context['pending']['account_id'])
        with tx(aid) as connection:
            accounts = all_rows(connection, "SELECT * FROM lead.social_accounts WHERE profile_id=%s AND state='connected' ORDER BY id", (actor.profile['id'],))
            account_b = next(account for account in accounts if str(account['id']) != account_a and account['connection_origin'] == context['boards']['93.184.215.15'].origin)
            # The existing bootstrap issued B's original claim through advance;
            # do not fabricate or replace it to clear capacity for this test.
            task_b = one(connection, """SELECT * FROM lead.tasks WHERE type='growth.account' AND state='running'
                AND input#>>'{spec,account_id}'=%s AND input#>>'{spec,purpose}'='discover' ORDER BY created_at LIMIT 1""", (str(account_b['id']),))
            assert task_b and task_b['result']['runtime']['attempt_token']
            ref_a = submit_work(connection, aid, TaskSpec(kind='account', purpose='discover', account_id=account_a,
                payload={'target_urls': [context['board'].origin + '/home']}, review_at=utcnow()+timedelta(hours=1),
                dedupe_key='chrome-crash-fixed-a-' + uid()))
        assert advance(ref_a)['state'] == 'ready'
        ref_b = {'agent_id': aid, 'task_id': str(task_b['id']), 'task_generation': task_b['input']['task_generation']}
        controllers = [NativeTask(actor, ref['task_id']) for ref in (ref_a, ref_b)]
        await controllers[0].start(context['board'].origin + '/home')
        await controllers[1].start(context['boards']['93.184.215.15'].origin + '/home')
        scopes = [copy.deepcopy(controller.lease) for controller in controllers]
        pages = [controller.page for controller in controllers]
        assert scopes[0]['target_id'] != scopes[1]['target_id']
        assert {scope['account_id'] for scope in scopes} == {account_a, str(account_b['id'])}
        assert all(page.context is actor.context for page in pages)
        for controller, subject in zip(controllers, (context['board'].state.username, context['boards']['93.184.215.15'].state.username)):
            await runtime.scope_view(controller.lease['target_id'])
            assert (await controller.page.locator('main > p').inner_text()).startswith('Signed in as ' + subject)
        before_model = len(context['model_calls'])
        old_session, old_epoch = str(actor.session), actor.epoch
        storage = str((actor.folder/'storage').resolve())
        candidates = []
        for pid in descendant_processes(os.getpid()):
            try:
                argv = command_line(pid)
            except OSError:
                continue
            if (argv and Path(argv[0]).name in ('chrome', 'chromium', 'google-chrome')
                    and '--user-data-dir=' + storage in argv
                    and any(value.startswith('--remote-debugging-port=') for value in argv)
                    and not any(value.startswith('--type=') for value in argv)):
                candidates.append((pid, argv))
        assert len(candidates) == 1, [(pid, argv[0]) for pid, argv in candidates]
        chrome_pid, chrome_argv = candidates[0]
        ancestry = parent_chain(chrome_pid)
        assert os.getpid() in ancestry and chrome_pid != os.getpid()
        # Recheck the exact process immediately before the only destructive step.
        assert command_line(chrome_pid) == chrome_argv
        with tx(aid) as connection:
            before_session = one(connection, 'SELECT * FROM lead.browser_sessions WHERE id=%s', (old_session,))
            before_lease = one(connection, 'SELECT * FROM lead.profile_leases WHERE profile_id=%s', (actor.profile['id'],))
        assert before_session['state'] == 'ready' and before_session['epoch'] == old_epoch
        assert before_lease['state'] == 'held' and before_lease['epoch'] == old_epoch
        os.kill(chrome_pid, signal.SIGKILL)

        def database_state():
            with tx(aid) as connection:
                session = one(connection, 'SELECT * FROM lead.browser_sessions WHERE id=%s', (old_session,))
                lease = one(connection, 'SELECT * FROM lead.profile_leases WHERE profile_id=%s', (actor.profile['id'],))
            invalid = lease['epoch'] != old_epoch or lease['state'] != 'held' or lease['expires_at'] <= utcnow()
            return session, lease, invalid
        deadline = asyncio.get_running_loop().time()+8
        while True:
            session, lease, epoch_invalid = database_state()
            if runtime.closed and actor.disconnected and session['ended_at'] is not None and epoch_invalid:
                break
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(.05)
        scope_errors, authority_errors = [], []
        for scope in scopes:
            try:
                await runtime.scope_view(scope['target_id'])
                scope_errors.append(None)
            except Exception as error:
                scope_errors.append({'type': type(error).__name__, 'reason': str(error)[:160]})
            try:
                runtime.current_scope(scope)
                authority_errors.append(None)
            except Exception as error:
                authority_errors.append({'type': type(error).__name__, 'reason': str(error)[:160]})
        diagnostics = {'killed_actual_chrome_main': True, 'chrome_pid': chrome_pid,
            'verified_test_storage': storage, 'parent_chain_to_this_test_verified': True,
            'two_distinct_original_tasks': [ref_a, ref_b], 'old_session': old_session, 'old_epoch': old_epoch,
            'runtime_closed': runtime.closed, 'actor_disconnected': actor.disconnected,
            'old_session_state': session['state'], 'old_session_ended': session['ended_at'] is not None,
            'lease_state': lease['state'], 'lease_epoch': lease['epoch'], 'old_epoch_invalid': epoch_invalid,
            'pages_closed': [page.is_closed() for page in pages], 'scope_errors': scope_errors,
            'authority_errors': authority_errors, 'forward_token_revoked': runtime.forward_token() is None,
            'new_model_requests': len(context['model_calls'])-before_model,
            'independent_instance_survives': 'not covered by this minimal shared-instance case'}
        print('CHROME_CRASH_DIAGNOSTICS ' + json.dumps(diagnostics, sort_keys=True))
        assert diagnostics['runtime_closed'] and diagnostics['actor_disconnected'], diagnostics
        assert diagnostics['old_session_state'] != 'ready' and diagnostics['old_session_ended'] and epoch_invalid, diagnostics
        assert all(diagnostics['pages_closed']) and all(scope_errors) and all(authority_errors), diagnostics
        assert diagnostics['forward_token_revoked'] and diagnostics['new_model_requests'] == 0, diagnostics
        return diagnostics

    result = await verify_login(monkeypatch, after_discovery=check)
    assert result['action_check']['killed_actual_chrome_main']
