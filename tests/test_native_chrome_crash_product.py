"""One real Chrome-main fault, independent public read and original recovery.

Uses synthetic fixture-A logins only. No model semantics, publication, forged
running claim or account row is involved. Public research uses its real factory;
recovered members need fresh private self, never DB connected state alone.
"""
import asyncio
import copy
import json
import os
import signal
import sqlite3
import sys
import time
from contextlib import closing
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


def committed_session_hosts(storage, cookies):
    """Read only this fresh fixture profile; never print cookie values."""
    expected = {item['domain']: item for item in cookies}
    for database in (Path(storage)/'Default/Cookies', Path(storage)/'Default/Network/Cookies'):
        if not database.is_file():
            continue
        try:
            with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=0)) as connection:
                rows = connection.execute("""SELECT host_key,path,has_expires,is_persistent,expires_utc,value,encrypted_value
                    FROM cookies WHERE name='board_session'""").fetchall()
        except sqlite3.Error:
            continue
        proved = set()
        for host, path, has_expiry, persistent, expiry, value, encrypted in rows:
            cookie = expected.get(host)
            if (cookie and path == cookie['path'] and has_expiry and persistent and expiry > 0
                    and abs(expiry/1_000_000-11_644_473_600-cookie['expires']) < 1
                    and (value == cookie['value'] if value else bool(encrypted))):
                proved.add(host)
        if proved == set(expected):
            return proved
    return set()


@pytest.mark.asyncio
async def test_real_chrome_fault_isolates_public_and_revalidates_shared_recovery(monkeypatch):
    import native_connection_product as helper
    from lead_app import account_profiles, browser
    from lead_app.browser_tools import BrowserTools
    from lead_app.db import all_rows, one, tx, uid
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance
    from lead_app.growth.store import submit_work
    from lead_app.native_task import NativeTask
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    original_board = helper.NativeBoard
    def persistent_board(state, **kwargs):
        state.persistent_session = True
        return original_board(state, **kwargs)
    monkeypatch.setattr(helper, 'NativeBoard', persistent_board)

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
        old_exit = (copy.deepcopy(runtime.execution_ref), runtime.transport_endpoint,
                    runtime.network_stamp, runtime.profile_exit, copy.deepcopy(actor.forward_proxy.upstream))
        with tx(aid) as connection:
            public_ref = submit_work(connection, aid, TaskSpec(kind='research', purpose='public-crash-survivor',
                payload={'urls': [context['board'].origin + '/home'], 'question': 'Read the public board'},
                review_at=utcnow()+timedelta(hours=1), dedupe_key='public-crash-survivor-' + uid()))
        assert advance(public_ref)['state'] == 'ready'
        public = BrowserTools(aid, 'Read the public board independently', task_id=public_ref['task_id'], mode='autonomous')
        public_before = await public.command('open', {'url': context['board'].origin + '/home'})
        if public_before.get('error'):
            print('CHROME_PUBLIC_FACTORY_DIAGNOSTICS ' + json.dumps(public_before, sort_keys=True))
        assert not public_before.get('error'), public_before
        with tx(aid) as connection:
            public_run = one(connection, 'SELECT * FROM lead.browser_runs WHERE id=%s', (public.run_id,))
        public_actor = await browser.gateway.get(aid, public_run['profile_id'])
        assert public_actor is not actor and public_actor.profile['slot'] == 'native-public-research'
        public_page = public_actor.research_pages[public.run_id].page
        public_epoch, public_session = public_actor.epoch, str(public_actor.session)
        public_storage = str((public_actor.folder/'storage').resolve())
        public_processes = []
        for pid in descendant_processes(os.getpid()):
            try:
                argv = command_line(pid)
            except OSError:
                continue
            if (argv and Path(argv[0]).name in ('chrome', 'chromium', 'google-chrome')
                    and '--user-data-dir=' + public_storage in argv
                    and any(value.startswith('--remote-debugging-port=') for value in argv)
                    and not any(value.startswith('--type=') for value in argv)):
                public_processes.append((pid, argv))
        assert len(public_processes) == 1
        public_pid, public_argv = public_processes[0]
        assert os.getpid() in parent_chain(public_pid)
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
        assert chrome_pid != public_pid
        ancestry = parent_chain(chrome_pid)
        assert os.getpid() in ancestry and chrome_pid != os.getpid()
        # Recheck the exact process immediately before the only destructive step.
        assert command_line(chrome_pid) == chrome_argv
        with tx(aid) as connection:
            before_session = one(connection, 'SELECT * FROM lead.browser_sessions WHERE id=%s', (old_session,))
            before_lease = one(connection, 'SELECT * FROM lead.profile_leases WHERE profile_id=%s', (actor.profile['id'],))
        assert before_session['state'] == 'ready' and before_session['epoch'] == old_epoch
        assert before_lease['state'] == 'held' and before_lease['epoch'] == old_epoch
        sites = (context['board'], context['boards']['93.184.215.15'])
        session_cookies = [cookie for site in sites for cookie in await actor.context.cookies(site.origin)
                           if cookie['name'] == 'board_session']
        assert len(session_cookies) == 2
        assert all(cookie['expires'] > time.time() for cookie in session_cookies)
        # Each fresh host issued exactly one token. Require its persistent SQLite
        # row before the crash, then compare the exact restored token below.
        assert all(len(site.state.sessions) == 1 for site in sites)
        for cookie, site in zip(session_cookies, sites, strict=True):
            original_token = cookie['value'] in site.state.sessions
            assert original_token
        async with asyncio.timeout(45):
            while not committed_session_hosts(storage, session_cookies):
                await asyncio.sleep(.05)
        committed_hosts = sorted(committed_session_hosts(storage, session_cookies))
        assert committed_hosts == sorted(cookie['domain'] for cookie in session_cookies)
        assert command_line(chrome_pid) == chrome_argv
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
            'persistent_cookie_hosts_committed_before_kill': committed_hosts,
            'new_model_requests': len(context['model_calls'])-before_model}
        print('CHROME_CRASH_DIAGNOSTICS ' + json.dumps(diagnostics, sort_keys=True))
        assert diagnostics['runtime_closed'] and diagnostics['actor_disconnected'], diagnostics
        assert diagnostics['old_session_state'] != 'ready' and diagnostics['old_session_ended'] and epoch_invalid, diagnostics
        assert all(diagnostics['pages_closed']) and all(scope_errors) and all(authority_errors), diagnostics
        assert diagnostics['forward_token_revoked'] and diagnostics['new_model_requests'] == 0, diagnostics

        public_after = await public.command('read')
        public_diagnostics = {'error': public_after.get('error'),
            'same_original_page': public_actor.research_pages[public.run_id].page is public_page,
            'same_epoch': public_actor.epoch == public_epoch, 'same_session': str(public_actor.session) == public_session,
            'same_process': command_line(public_pid) == public_argv,
            'forward_token_current': public_actor.native_runtime.forward_token() is not None,
            'read_nonempty': bool(public_after.get('text')), 'page_closed': public_page.is_closed()}
        print('CHROME_PUBLIC_SURVIVOR_DIAGNOSTICS ' + json.dumps(public_diagnostics, sort_keys=True))
        assert not public_diagnostics['error'] and public_diagnostics['read_nonempty'], public_diagnostics
        assert all(public_diagnostics[key] for key in ('same_original_page', 'same_epoch', 'same_session', 'same_process', 'forward_token_current')), public_diagnostics
        assert not public_diagnostics['page_closed']

        # This is the ordinary factory recovery entry, with the original profile
        # and persistent storage. Never write session, epoch, cookie or health.
        replacement = await browser.gateway.get(aid, actor.profile['id'])
        recovered = replacement.native_runtime
        for cookie in session_cookies:
            restored = [item for item in await replacement.context.cookies() if
                        item['domain'] == cookie['domain'] and item['name'] == cookie['name'] and item['path'] == cookie['path']]
            assert len(restored) == 1
            original_token = restored[0]['value'] == cookie['value']
            assert original_token
        new_exit = (recovered.execution_ref, recovered.transport_endpoint,
                    recovered.network_stamp, recovered.profile_exit, replacement.forward_proxy.upstream)
        recovery = {'new_actor': replacement is not actor, 'same_storage': str((replacement.folder/'storage').resolve()) == storage,
            'same_registered_exit': new_exit == old_exit, 'new_session': str(replacement.session) != old_session,
            'old_epoch': old_epoch, 'new_epoch': replacement.epoch, 'old_targets_rejected': [], 'private_self': []}
        for scope in scopes:
            try:
                await recovered.scope_view(scope['target_id'])
                recovery['old_targets_rejected'].append(False)
            except Exception:
                recovery['old_targets_rejected'].append(True)
        restored_controllers = [NativeTask(replacement, ref['task_id']) for ref in (ref_a, ref_b)]
        try:
            for controller, site in zip(restored_controllers, (context['board'], context['boards']['93.184.215.15'])):
                try:
                    await controller.start(site.origin + '/home')
                    account = await controller.verify_private_self()
                    recovery['private_self'].append({'authenticated': True, 'target_id': controller.lease['target_id'],
                        'generation': controller.lease['generation'], 'health': account['state']})
                except Exception as error:
                    with tx(aid) as connection:
                        health = one(connection, 'SELECT state FROM lead.social_accounts WHERE id=%s',
                                     (account_a if site is context['board'] else account_b['id'],))['state']
                    recovery['private_self'].append({'authenticated': False, 'error_type': type(error).__name__,
                        'reason': str(error)[:250], 'database_health': health})
        finally:
            for controller in restored_controllers:
                await controller.close()
            await public.finish('succeeded')
        print('CHROME_RECOVERY_DIAGNOSTICS ' + json.dumps(recovery, sort_keys=True))
        assert all(recovery[key] for key in ('new_actor', 'same_storage', 'same_registered_exit', 'new_session')), recovery
        assert replacement.epoch > old_epoch and all(recovery['old_targets_rejected']), recovery
        assert len(recovery['private_self']) == 2 and all(row['authenticated'] for row in recovery['private_self']), recovery
        assert all(row['generation'] == replacement.epoch and row['target_id'] not in {scope['target_id'] for scope in scopes}
                   for row in recovery['private_self']), recovery
        assert len(context['model_calls']) == before_model
        return {'death': diagnostics, 'public': public_diagnostics, 'recovery': recovery, 'killed_actual_chrome_main': True}

    result = await verify_login(monkeypatch, after_discovery=check)
    assert result['action_check']['killed_actual_chrome_main']
