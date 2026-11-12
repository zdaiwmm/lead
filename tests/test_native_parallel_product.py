"""Two original native tasks overlap; unknown receipt reads do not stop channel B.

Only local model HTTP responses and source timing are controlled. Original
claims, manager, SDK workers, pages, network gate and action receipts execute.
"""
import asyncio
import contextvars
import json
import os
import re
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')


def model_transport(board, secrets, calls):
    state = {'kind': 'discovery', 'step': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            request = json.loads(raw)
            parts = []
            for message in request.get('messages', []):
                value = message.get('content')
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, list):
                    parts.extend(item.get('text', '') for item in value if isinstance(item, dict))
            lines = '\n'.join(parts).splitlines()
            nodes = [(int(match[1]), '\n'.join(lines[number:number+3]))
                for number, line in enumerate(lines) if (match := re.search(r'\[(\d+)\]', line))
                and any(tag in line.lower() for tag in ('<textarea', '<button', '<a '))]
            step, kind = state['step'], state['kind']
            state['step'] += 1
            action = {'done': {'text': 'Synthetic mechanical transport', 'success': True}}
            if kind == 'discovery':
                if step == 0:
                    action = {'guarded_navigate': {'url': board.origin + '/home'}}
                elif step == 1:
                    action = {'propose_method': {'target_url': board.origin + '/home', 'operation': 'publish',
                        'selectors': {'self_subject': 'main > p', 'editor_target': '#editor-target',
                            'editor_body': 'textarea[name=body]', 'submit': 'form button'},
                        'self_subject_prefix': 'Signed in as ', 'self_subject_suffix': ' · Your account settings',
                        'permission_selector': 'section p', 'permission_quote':
                            'You are an ordinary member. Members may publish text posts and reply to existing posts. '
                            'Automated browser tools are permitted for these member actions. You have no administrator privileges.'}}
            elif kind == 'action' and step == 0:
                matches = [index for index, text in nodes if '<textarea' in text.lower()]
                if matches:
                    action = {'guarded_input': {'index': matches[-1], 'text': state['body'], 'clear': True}}
            elif kind == 'action' and step == 1:
                matches = [index for index, text in nodes if '<button' in text.lower()]
                if matches:
                    action = {'guarded_click': {'index': matches[-1]}}
            elif kind == 'reconcile' and step == 0:
                matches = [index for index, text in nodes if '<a ' in text.lower() and state['body'] in text]
                if matches:
                    action = {'guarded_click': {'index': matches[-1]}}
            elif kind == 'action' and step == 2 or kind == 'reconcile' and step == 1:
                action = {'report_receipt': {'selectors': {'author': '#author', 'body': '#post-body', 'url': 'a[rel~=canonical]'}}}
            calls.append({'origin': board.origin, 'kind': kind, 'step': step,
                'action': next(iter(action)), 'credential_literal_seen': any(secret.encode() in raw for secret in secrets)})
            tool = request.get('tools', [{'function': {'name': 'AgentOutput'}}])[0]['function']['name']
            response = json.dumps({'id': 'synthetic-parallel', 'object': 'chat.completion', 'model': 'fixture-transport',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'synthetic-call', 'type': 'function', 'function': {'name': tool, 'arguments': json.dumps({'action': [action]})}}]},
                    'finish_reason': 'tool_calls'}], 'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state, {'provider': 'deepseek', 'key': 'synthetic-transport-only',
        'model': 'fixture-transport', 'base_url': f'http://127.0.0.1:{server.server_port}'}


@pytest.mark.asyncio
async def test_original_workers_overlap_and_unknown_does_not_stop_other_channel(monkeypatch):
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
    issued = {}
    original_advance = bootstrap.advance

    def observe_advance(*args, **kwargs):
        result = original_advance(*args, **kwargs)
        if result.get('claim'):
            claim = result['claim']
            issued[claim['ref']['task_id']] = claim
        return result

    monkeypatch.setattr(bootstrap, 'advance', observe_advance)

    async def check(context):
        aid, actor = context['aid'], context['actor']
        boards = [context['board'], context['boards']['93.184.215.15']]
        with tx(aid) as c:
            accounts = all_rows(c, "SELECT * FROM lead.social_accounts WHERE profile_id=%s AND state='connected'", (actor.profile['id'],))
            account_ids = [str(next(item for item in accounts if item['connection_origin'] == board.origin)['id']) for board in boards]
            task_b = one(c, """SELECT * FROM lead.tasks WHERE state='running' AND type='growth.account'
                AND input#>>'{spec,account_id}'=%s AND input#>>'{spec,purpose}'='discover'""", (account_ids[1],))
        assert task_b and str(task_b['id']) in issued
        assert issued[str(task_b['id'])]['ref']['task_generation'] == task_b['input']['task_generation']
        calls, workers, intervals, servers, by_task = [], [], [], [], {}
        secrets = [value for board in boards for value in (board.state.username, board.state.password)]
        transports = []
        for board in boards:
            server, state, connection = model_transport(board, secrets, calls)
            servers.append(server)
            transports.append((state, connection))
        active = contextvars.ContextVar('synthetic_parallel_native_task')
        original_run = native_runner.run_task
        original_spawn = asyncio.create_subprocess_exec

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
        by_task[str(task_b['id'])] = transports[1][1]
        phase = {'name': 'none', 'barrier': None}
        entered = asyncio.Event()
        loop = asyncio.get_running_loop()

        def observe_source(board, original, method):
            def wrapped(handler):
                path = urlsplit(handler.path).path
                selected = phase['name'] == 'parallel' and method == 'POST' and path == '/posts' or (
                    phase['name'] == 'unknown' and (board is boards[0] and method == 'GET' and path == '/posts/1'
                        or board is boards[1] and method == 'POST' and path == '/posts'))
                if not selected:
                    return original(handler)
                row = {'phase': phase['name'], 'origin': board.origin, 'method': method, 'path': path, 'started': time.monotonic()}
                # Receipt uniqueness legitimately reads the same post again.
                # Synchronize one source interval per participant, not that
                # later manager-owned read, which has no second barrier peer.
                row['barrier_member'] = not any(item['phase']==row['phase'] and item['origin']==row['origin']
                    and item['method']==method for item in intervals)
                intervals.append(row)
                if phase['name'] == 'unknown' and board is boards[0]:
                    loop.call_soon_threadsafe(entered.set)
                try:
                    if row['barrier_member']:
                        phase['barrier'].wait(timeout=20)
                        time.sleep(0.5)
                    return original(handler)
                except threading.BrokenBarrierError:
                    row['barrier_timeout'] = True
                    return original(handler)
                finally:
                    row['ended'] = time.monotonic()
            return wrapped

        for board in boards:
            handler = board.server.RequestHandlerClass
            for method in ('GET', 'POST'):
                name = 'do_' + method
                monkeypatch.setattr(handler, name, observe_source(board, getattr(handler, name), method))

        def work(number, text, target):
            body = {'body': text, 'title': '', 'format': 'text', 'media': []}
            with tx(aid) as c:
                content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                    VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""", (aid, j(body), digest(body)))
                data = ChannelInput(content_id=content['id'], content_hash=content['content_hash'])
                ref = submit_work(c, aid, TaskSpec(kind='channel', purpose='publish', account_id=account_ids[number],
                    target_id=target, payload=data.model_dump(mode='json'), review_at=utcnow()+timedelta(hours=1)))
            by_task[str(ref['task_id'])] = transports[number][1]
            return ref, data

        async def execute(item, number, kind='action'):
            ref, data = item
            admitted = advance(ref, now=utcnow()+timedelta(minutes=6))
            assert admitted['state'] == 'ready', admitted
            with tx(aid) as c:
                task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
                body = one(c, 'SELECT body FROM lead.content_items WHERE id=%s', (data.content_id,))['body']['body']
            transports[number][0].update(kind=kind, step=0, body=body)
            result = await execute_channel(aid, task, data, admitted['claim'])
            finish(admitted['claim'], result)
            with tx(aid) as c:
                action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s', (ref['task_id'],))
                attempts = all_rows(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
                terminal = one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (ref['task_id'],))['state']
            return result, action, attempts, terminal

        try:
            learned = await discover(aid, task_b)
            assert learned.outcome == 'completed'
            finish(issued[str(task_b['id'])], learned)
            targets = [context['discovery'].output['targets'][0]['target_id'], learned.output['targets'][0]['target_id']]
            first = [work(index, f'Parallel channel {index} original publication', targets[index]) for index in range(2)]
            boards[0].state.drop_next_post_response = True
            phase.update(name='parallel', barrier=threading.Barrier(2))
            initial = await asyncio.wait_for(asyncio.gather(execute(first[0], 0), execute(first[1], 1)), 85)
            assert initial[0][0].outcome == 'unknown' and initial[0][1]['state'] == 'unknown'
            assert initial[1][0].outcome == 'completed' and initial[1][1]['state'] == 'confirmed' and initial[1][3] == 'succeeded'
            original = initial[0][1]
            other = work(1, 'Independent channel B publication during unknown receipt read', targets[1])
            phase.update(name='unknown', barrier=threading.Barrier(2))
            reading = asyncio.create_task(execute(first[0], 0, 'reconcile'))
            await asyncio.wait_for(entered.wait(), 30)
            with tx(aid) as c:
                assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (original['id'],))['state'] == 'unknown'
            restored, continued = await asyncio.wait_for(asyncio.gather(reading, execute(other, 1)), 65)
            assert continued[0].outcome == 'completed' and continued[1]['state'] == 'confirmed' and continued[3] == 'succeeded'
            assert restored[0].outcome == 'completed' and restored[1]['state'] == 'confirmed'
            assert restored[1]['id'] == original['id'] and restored[1]['intent'] == original['intent']
            assert len(restored[2]) == 1 and restored[2][0]['attempt'] == 1
            assert [len(board.oracle()['posts']) for board in boards] == [1, 2]
            assert [sum(row['method']=='POST' and row['path']=='/posts' for row in board.oracle()['requests']) for board in boards] == [1, 2]
            for name in ('parallel', 'unknown'):
                rows = [row for row in intervals if row['phase'] == name and row['barrier_member']]
                assert len(rows) == 2 and not any(row.get('barrier_timeout') for row in rows), rows
                overlap = min(row['ended'] for row in rows)-max(row['started'] for row in rows)
                assert overlap >= 0.4, rows
            pids = [row for row in workers if 'pid' in row]
            assert len({row['pid'] for row in pids}) == 5
            assert all(row['parent_pid'] == os.getpid() for row in pids)
            pages = [row for row in workers if 'target_id' in row]
            assert len({row['profile_id'] for row in pages}) == 1 and len({row['epoch'] for row in pages}) == 1
            assert len({row['target_id'] for row in pages}) == 5
            assert not any(row['credential_literal_seen'] for row in calls)
            return {'parallel_overlap': True, 'unknown_independent_continuation': True}
        finally:
            (STATE/'native-parallel-diagnostics.json').write_text(json.dumps({'intervals': intervals, 'workers': workers, 'model_calls': calls}, default=str, indent=2))
            for server in servers:
                server.shutdown()
                server.server_close()

    result = await bootstrap.verify_login(monkeypatch, after_discovery=check)
    assert result['action_check'] == {'parallel_overlap': True, 'unknown_independent_continuation': True}
