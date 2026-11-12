"""Same-account unknown receipt reads coexist with a new independent write.

Only synthetic model HTTP responses and source timing are controlled. Claims,
admission, manager, SDK workers, network gate, Actions and receipts stay real.
"""
import asyncio
import contextvars
import json
import os
import threading
import time
from datetime import timedelta
from urllib.parse import urlsplit

import pytest
from test_native_parallel_product import model_transport

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
async def test_unknown_receipt_read_allows_same_account_independent_original_task(monkeypatch):
    import native_connection_product as bootstrap
    from lead_app import account_profiles, native_runner
    from lead_app.account_connection import discover
    from lead_app.config import STATE
    from lead_app.db import all_rows, j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel

    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    issued, original_advance = {}, bootstrap.advance

    def observe_advance(*args, **kwargs):
        result = original_advance(*args, **kwargs)
        if result.get('claim'):
            issued[result['claim']['ref']['task_id']] = result['claim']
        return result

    monkeypatch.setattr(bootstrap, 'advance', observe_advance)

    async def check(context):
        aid, board, actor = context['aid'], context['board'], context['actor']
        account = context['pending']['account_id']
        second_board = context['boards']['93.184.215.15']
        with tx(aid) as c:
            second = one(c, """SELECT t.* FROM lead.tasks t JOIN lead.social_accounts a
                ON a.id::text=t.input#>>'{spec,account_id}' WHERE t.state='running'
                AND t.type='growth.account' AND t.input#>>'{spec,purpose}'='discover'
                AND a.connection_origin=%s""", (second_board.origin,))
        assert second and str(second['id']) in issued
        calls, workers, intervals, servers, by_task, admissions = [], [], [], [], {}, []
        secrets = [value for site in context['boards'].values() for value in (site.state.username, site.state.password)]
        transports = []
        for site in (board, board, second_board):
            server, state, connection = model_transport(site, secrets, calls)
            servers.append(server)
            transports.append((state, connection))
        active = contextvars.ContextVar('synthetic_same_account_native_task')
        original_run, original_spawn = native_runner.run_task, asyncio.create_subprocess_exec

        async def run(controller, *args, **kwargs):
            token = active.set(str(controller.task_id))
            try:
                return await original_run(controller, *args, **kwargs)
            finally:
                workers.append({'task_id': str(controller.task_id), 'target_id': controller.lease.get('target_id'),
                    'profile_id': str(controller.actor.profile['id']), 'epoch': controller.actor.epoch})
                active.reset(token)

        async def spawn(*args, **kwargs):
            child = await original_spawn(*args, **kwargs)
            if any(str(value).endswith('browser_use_runner.py') for value in args):
                workers.append({'task_id': active.get(), 'pid': child.pid, 'parent_pid': os.getpid()})
            return child

        monkeypatch.setattr(native_runner, 'run_task', run)
        monkeypatch.setattr(native_runner, 'resolve_model', lambda _: by_task[active.get()])
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        by_task[str(second['id'])] = transports[2][1]
        phase, reading = {'active': False, 'barrier': threading.Barrier(2)}, None
        entered, loop = asyncio.Event(), asyncio.get_running_loop()
        handler = board.server.RequestHandlerClass

        def observe_source(original, method):
            def wrapped(request):
                path = urlsplit(request.path).path
                if not phase['active'] or (method, path) not in (('GET', '/posts/1'), ('POST', '/posts')):
                    return original(request)
                row = {'method': method, 'path': path, 'started': time.monotonic(),
                    'barrier_member': not any(item['method'] == method for item in intervals)}
                intervals.append(row)
                if method == 'GET':
                    loop.call_soon_threadsafe(entered.set)
                try:
                    if row['barrier_member']:
                        phase['barrier'].wait(timeout=20)
                        time.sleep(.5)
                    return original(request)
                except threading.BrokenBarrierError:
                    row['barrier_aborted'] = True
                    return original(request)
                finally:
                    row['ended'] = time.monotonic()
            return wrapped

        for method in ('GET', 'POST'):
            monkeypatch.setattr(handler, 'do_' + method, observe_source(getattr(handler, 'do_' + method), method))
        target = context['discovery'].output['targets'][0]['target_id']

        def work(text):
            body = {'body': text, 'title': '', 'format': 'text', 'media': []}
            with tx(aid) as c:
                content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                    VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""", (aid, j(body), digest(body)))
                data = ChannelInput(content_id=content['id'], content_hash=content['content_hash'])
                spec = TaskSpec(kind='channel', purpose='publish', account_id=account, target_id=target,
                    payload=data.model_dump(mode='json'), review_at=utcnow()+timedelta(hours=1))
                ref = submit_work(c, aid, spec)
            return ref, data, spec, text

        async def execute(item, number, kind='action'):
            ref, data, _, body = item
            admitted = advance(ref, now=utcnow()+timedelta(minutes=6))
            admissions.append({'task_id': str(ref['task_id']), 'kind': kind, 'state': admitted['state'],
                'wait': admitted.get('wait')})
            assert admitted['state'] == 'ready', admitted
            with tx(aid) as c:
                task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
            transports[number][0].update(kind=kind, step=0, body=body)
            result = await execute_channel(aid, task, data, admitted['claim'])
            finish(admitted['claim'], result)
            with tx(aid) as c:
                action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s', (ref['task_id'],))
                attempts = all_rows(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
                terminal = one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state']
            return result, action, attempts, terminal, admitted['claim']['attempt_token']

        try:
            # Finish the bootstrap's real second-account discovery so it does
            # not consume either shared profile slot during this same-account case.
            learned = await discover(aid, second)
            assert learned.outcome == 'completed'
            finish(issued[str(second['id'])], learned)
            first = work('Same account original publication with a missing acknowledgement')
            by_task[str(first[0]['task_id'])] = transports[0][1]
            board.state.drop_next_post_response = True
            initial = await execute(first, 0)
            assert initial[0].outcome == 'unknown' and initial[1]['state'] == 'unknown'
            assert len(initial[2]) == 1 and initial[2][0]['attempt'] == 1
            assert len(board.oracle()['posts']) == 1 and not board.state.drop_next_post_response
            with tx(aid) as c:
                original_intent = one(c, 'SELECT * FROM lead.account_operating_intents WHERE account_id=%s', (account,))
                original_grants = all_rows(c, 'SELECT * FROM lead.authorization_grants ORDER BY id')
                assert submit_work(c, aid, first[2]) == first[0]
            other = work('Same account independent publication during original unknown receipt read')
            assert other[0]['task_id'] != first[0]['task_id']
            by_task[str(other[0]['task_id'])] = transports[1][1]
            phase['active'] = True
            reading = asyncio.create_task(execute(first, 0, 'reconcile'))
            await asyncio.wait_for(entered.wait(), 30)
            with tx(aid) as c:
                active_task = one(c, 'SELECT state,result FROM lead.tasks WHERE id=%s', (first[0]['task_id'],))
                assert active_task['state'] == 'running'
                assert active_task['result']['runtime']['reconcile_only_action_id'] == str(initial[1]['id'])
                assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (initial[1]['id'],))['state'] == 'unknown'
                assert submit_work(c, aid, first[2]) == first[0]
            assert advance(first[0])['wait']['reason'] == 'work_leased'
            restored, continued = await asyncio.wait_for(asyncio.gather(reading, execute(other, 1)), 65)
            phase['active'] = False
            for result, action, attempts, terminal, _ in (restored, continued):
                assert result.outcome == 'completed' and action['state'] == 'confirmed' and terminal == 'succeeded'
                assert len(attempts) == 1 and attempts[0]['attempt'] == 1 and attempts[0]['outcome'] == 'confirmed'
            assert restored[1]['id'] == initial[1]['id'] and restored[1]['intent'] == initial[1]['intent']
            assert restored[4] != initial[4]
            with tx(aid) as c:
                assert one(c, 'SELECT * FROM lead.account_operating_intents WHERE account_id=%s', (account,)) == original_intent
                assert all_rows(c, 'SELECT * FROM lead.authorization_grants ORDER BY id') == original_grants
                assert submit_work(c, aid, first[2]) == first[0]
            before = len(calls)
            assert advance(first[0]) == {'state': 'terminal', 'reason': 'succeeded'}
            assert len(calls) == before
            assert len(board.oracle()['posts']) == 2
            assert sum(row['method'] == 'POST' and row['path'] == '/posts' for row in board.oracle()['requests']) == 2
            rows = [row for row in intervals if row['barrier_member']]
            assert len(rows) == 2 and not any(row.get('barrier_aborted') for row in rows), rows
            overlap = min(row['ended'] for row in rows) - max(row['started'] for row in rows)
            assert overlap >= .4, rows
            pages = [row for row in workers if 'target_id' in row and row['task_id'] in by_task and row['task_id'] != str(second['id'])]
            assert len(pages) == 3 and len({row['target_id'] for row in pages}) == 3
            assert {row['profile_id'] for row in pages} == {str(actor.profile['id'])}
            assert len({row['epoch'] for row in pages}) == 1
            pids = [row for row in workers if 'pid' in row and row['task_id'] != str(second['id'])]
            assert len(pids) == 3 and len({row['pid'] for row in pids}) == 3
            assert all(row['parent_pid'] == os.getpid() for row in pids)
            assert not any(row['credential_literal_seen'] for row in calls)
            return {'same_account_overlap': True, 'same_intent_reused': True, 'source_posts': 2}
        finally:
            phase['barrier'].abort()
            if reading and not reading.done():
                await asyncio.wait_for(reading, 35)
            with tx(aid) as c:
                actions = all_rows(c, 'SELECT id,task_id,state FROM lead.actions ORDER BY created_at')
                attempts = all_rows(c, 'SELECT action_id,attempt,outcome FROM lead.action_attempts ORDER BY started_at')
            (STATE/'native-same-account-diagnostics.json').write_text(json.dumps({'intervals': intervals,
                'workers': workers, 'model_calls': calls, 'admissions': admissions,
                'actions': actions, 'attempts': attempts,
                'source_posts': len(board.oracle()['posts']),
                'source_post_requests': sum(row['method'] == 'POST' and row['path'] == '/posts' for row in board.oracle()['requests'])}, default=str, indent=2))
            for server in servers:
                server.shutdown()
                server.server_close()

    result = await bootstrap.verify_login(monkeypatch, after_discovery=check)
    assert result['action_check'] == {'same_account_overlap': True, 'same_intent_reused': True, 'source_posts': 2}
