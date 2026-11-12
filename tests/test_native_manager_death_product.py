"""Kill one actual manager after request CAS, before upstream transmission.

Only synthetic local HTTP model/board/TLS fixtures are reachable. The manager,
Actor, PostgreSQL, namespace Chrome, proxy and Action attempt are the real ones.
"""

import asyncio
import hashlib
import json
import os
import signal
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != 'linux' or os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required',
)


async def until(predicate, *, seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError('synthetic product phase timed out')


def process_fact(pid):
    try:
        raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {'ppid': int(raw[1]), 'start': raw[19]}
    except (FileNotFoundError, ProcessLookupError):
        return None


def descendants(pid):
    owned = {pid: process_fact(pid)}
    changed = True
    while changed:
        changed = False
        for folder in Path('/proc').iterdir():
            if folder.name.isdecimal() and int(folder.name) not in owned:
                fact = process_fact(int(folder.name))
                if fact and fact['ppid'] in owned:
                    owned[int(folder.name)] = fact
                    changed = True
    return owned


def kill_recorded(owned):
    for pid, fact in reversed(list(owned.items())):
        current = process_fact(pid)
        if current and fact and current['start'] == fact['start']:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def tls_contexts(folder):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'stream.fixture.test')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('stream.fixture.test')]), critical=False)
            .sign(key, hashes.SHA256()))
    certificate, private_key = folder / 'stream.crt', folder / 'stream.key'
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    private_key.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                            serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    private_key.chmod(0o600)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate, private_key)
    client = ssl.create_default_context(cafile=str(certificate))
    return server, client


@pytest.mark.asyncio
async def test_entire_manager_death_after_claim_closes_tunnel_and_only_reconciles(monkeypatch):
    import uvicorn
    from lead_app import account_profiles, browser, native_runner
    from lead_app.activities import browser_rpc
    from lead_app.config import STATE, env
    from lead_app.db import j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from native_browser_fixture import NativeBoard
    from native_connection_product import verify_login
    from native_manager_death_child import private_json

    assert env().get('LEAD_DB_NAME', '').startswith('manager_death_')
    assert env().get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    original_handler = NativeBoard.handler

    def persistent_login_handler(board):
        class Handler(original_handler(board)):
            def send_header(self, name, value):
                if name.lower() == 'set-cookie' and value.startswith('board_session='):
                    # Real login still mints and proves this opaque cookie. The
                    # synthetic website opts into a persistent login so a normal
                    # Chromium restart does not discard a session-only cookie.
                    value += '; Max-Age=3600'
                return super().send_header(name, value)
        return Handler

    monkeypatch.setattr(NativeBoard, 'handler', persistent_login_handler)
    parent_servers = []
    original_server = uvicorn.Server

    def capture_server(*args, **kwargs):
        server = original_server(*args, **kwargs)
        parent_servers.append(server)
        return server

    monkeypatch.setattr(uvicorn, 'Server', capture_server)

    async def cut(context):
        aid, board = context['aid'], context['board']
        folder = STATE / ('manager-death-' + uuid4().hex[:10])
        folder.mkdir(mode=0o700)
        report = {'cut': 'after_original_request_cas_before_upstream', 'real_model_requests': 0}
        report['fixture'] = {
            'file_sha256': hashlib.sha256(Path(sys.modules[NativeBoard.__module__].__file__).read_bytes()).hexdigest(),
            'response_header_change': 'Only board_session Set-Cookie adds Max-Age=3600',
            'authentication': 'Original browser /session login and fresh private-self checks; no cookie injection',
            'limitation': 'Restart proof assumes a website-issued persistent login cookie',
        }
        phases = []

        def phase(name):
            phases.append(name)
            private_json(folder / 'phases.json', phases)

        await browser.gateway.close(aid)
        assert len(parent_servers) == 1
        parent_servers[0].should_exit = True
        endpoint = context['gateway_endpoint']
        port = urlsplit(endpoint).port
        for _ in range(100):
            try:
                _, writer = await asyncio.open_connection('127.0.0.1', port)
                writer.close()
                await writer.wait_closed()
            except OSError:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError('parent endpoint did not close')
        phase('parent_actor_closed_endpoint_released')

        received = []
        stream_closed = asyncio.Event()

        async def echo(reader, writer):
            try:
                while line := await reader.readline():
                    received.append(line.decode('ascii').strip())
                    writer.write(line)
                    await writer.drain()
            finally:
                stream_closed.set()
                writer.close()

        tls_server, tls_client = tls_contexts(folder)
        stream = await asyncio.start_server(echo, '127.0.0.1', 0, ssl=tls_server)
        destinations = {f'{ip}:80': fixture.server.server_port for ip, fixture in context['boards'].items()}
        destinations['93.184.215.16:443'] = stream.sockets[0].getsockname()[1]
        model = native_runner.resolve_model('fixture-only')
        assert model['model'] == 'fixture-transport' and urlsplit(model['base_url']).hostname == '127.0.0.1'
        child_script = Path(__file__).with_name('native_manager_death_child.py').resolve()
        children, logs, owned = [], [], {}
        active = None
        writer = None
        pending = None

        async def launch(cut_enabled):
            nonce = uuid4().hex
            boot_path = folder / (nonce + '.json')
            boot = {'nonce': nonce, 'endpoint': endpoint, 'model': model,
                    'destinations': destinations, 'cut_after_claim': cut_enabled,
                    'ready': str(folder / (nonce + '-ready.json')), 'cut': str(folder / (nonce + '-cut.json'))}
            private_json(boot_path, boot)
            log = (folder / (nonce + '.log')).open('w')
            logs.append(log)
            child = subprocess.Popen([sys.executable, '-B', '-S', str(child_script), str(boot_path)],
                                     env=dict(os.environ), stdout=log, stderr=log)
            children.append(child)
            for _ in range(200):
                assert child.poll() is None, 'synthetic manager exited before listening'
                try:
                    _, probe = await asyncio.open_connection('127.0.0.1', port)
                    probe.close()
                    await probe.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            else:
                raise AssertionError('child endpoint did not open')
            opened = await browser_rpc(f'/agents/{aid}/open', {
                'platform': 'web', 'account_id': str(context['pending']['account_id'])})
            await until(lambda: Path(boot['ready']).exists())
            ready = json.loads(Path(boot['ready']).read_text())
            assert ready['pid'] == child.pid and ready['ppid'] == os.getpid()
            assert ready['nonce'] == nonce and ready['session'] == opened['session_id']
            assert ready['boundary']['state'] == 'verified'
            assert ready['boundary']['only_loopback'] and ready['boundary']['direct_udp_blocked']
            return child, boot, ready

        try:
            active, boot, ready = await launch(True)
            report['first_manager'] = {key: value for key, value in ready.items() if key != 'proxy_auth'}
            phase('independent_manager_namespace_verified')
            reader, writer = await asyncio.open_connection('127.0.0.1', ready['proxy_port'])
            writer.write(('CONNECT 93.184.215.16:443 HTTP/1.1\r\nHost: 93.184.215.16:443\r\n'
                          f"Proxy-Authorization: {ready['proxy_auth']}\r\n\r\n").encode())
            await writer.drain()
            assert b'200' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            await writer.start_tls(tls_client, server_hostname='stream.fixture.test')
            writer.write(b'BEFORE\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 5) == b'BEFORE\n'
            phase('live_connect_tls_stream')

            body = {'body': 'Controlled whole manager death publication', 'title': '', 'format': 'text', 'media': []}
            with tx(aid) as c:
                content = one(c, """INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                    VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""", (aid, j(body), digest(body)))
                data = ChannelInput(content_id=content['id'], content_hash=content['content_hash'])
                ref = submit_work(c, aid, TaskSpec(kind='channel', purpose='publish', account_id=context['pending']['account_id'],
                    target_id=context['discovery'].output['targets'][0]['target_id'], payload=data.model_dump(mode='json'),
                    review_at=utcnow() + timedelta(hours=1)))
            claim = advance(ref)['claim']
            with tx(aid) as c:
                task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (ref['task_id'],))
            context['workflow'].update(kind='action', step=0, body=body['body'], operation='publish')
            pending = asyncio.create_task(execute_channel(aid, task, data, claim))
            await until(lambda: Path(boot['cut']).exists() or pending.done(), seconds=50)
            if pending.done():
                try:
                    await pending
                except Exception as error:
                    response = getattr(error, 'response', None)
                    detail = response.json() if response is not None else type(error).__name__
                    raise AssertionError(f'original product refused before the cut: {detail}') from error
                raise AssertionError('original product returned before the cut')
            frozen = json.loads(Path(boot['cut']).read_text())
            assert frozen['pid'] == active.pid and frozen['nonce'] == boot['nonce']
            with tx(aid) as c:
                action = one(c, 'SELECT * FROM lead.actions WHERE task_id=%s', (task['id'],))
                attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
            assert action['state'] == 'dispatching'
            assert attempt['observation']['browser_request']['claimed'] is True
            assert not attempt['observation'].get('browser_transport')
            assert not board.oracle()['posts']
            phase('actual_claim_committed_source_zero')

            fact = process_fact(active.pid)
            argv = Path(f'/proc/{active.pid}/cmdline').read_bytes().split(b'\0')
            assert fact['ppid'] == os.getpid() and str(child_script).encode() in argv
            assert str(folder / (boot['nonce'] + '.json')).encode() in argv
            owned = descendants(active.pid)
            os.kill(active.pid, signal.SIGKILL)
            await asyncio.to_thread(active.wait, 5)
            assert active.returncode == -signal.SIGKILL
            report['killed_manager'] = {'pid': active.pid, **fact, 'argv_and_parent_verified': True}
            assert await asyncio.wait_for(reader.read(1), 5) == b''
            await asyncio.wait_for(stream_closed.wait(), 5)
            try:
                writer.write(b'AFTER\n')
                await writer.drain()
            except (OSError, ssl.SSLError):
                pass
            with pytest.raises(OSError):
                await asyncio.open_connection('127.0.0.1', ready['proxy_port'])
            await asyncio.sleep(0.5)
            assert received == ['BEFORE'] and not board.oracle()['posts']
            report['tunnel'] = {'tls_before_received': True, 'eof_after_manager_death': True,
                                'new_connection_refused': True, 'source_lines': received}
            phase('whole_manager_dead_tunnel_closed_source_zero')
            result = await asyncio.wait_for(pending, 10)
            assert result.outcome == 'unknown'
            finish(claim, result)
            # Cleanup only descendants captured while their ancestry led to our
            # verified child. This is after the EOF/source-zero evidence.
            kill_recorded(owned)
            owned = {}
            before_reads = len(board.oracle()['requests'])
            active, _, restarted = await launch(False)
            assert restarted['epoch'] > ready['epoch'] and restarted['session'] != ready['session']
            report['restart_manager'] = {key: value for key, value in restarted.items() if key != 'proxy_auth'}
            with tx(aid) as c:
                assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action['id'],))['state'] == 'unknown'
            resumed = advance(ref, now=utcnow() + timedelta(minutes=6))
            assert resumed['state'] == 'ready', resumed
            with tx(aid) as c:
                current = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (task['id'],))
            context['workflow'].update(kind='reconcile', step=0, body=body['body'], operation='publish')
            result = await execute_channel(aid, current, data, resumed['claim'])
            finish(resumed['claim'], result)
            assert result.outcome == 'unknown'
            with tx(aid) as c:
                actions = c.execute('SELECT id,state FROM lead.actions WHERE task_id=%s', (task['id'],)).fetchall()
                attempts = c.execute('SELECT id,attempt,outcome FROM lead.action_attempts WHERE action_id=%s', (action['id'],)).fetchall()
                billing = c.execute('SELECT state,units FROM lead.budget_reservations').fetchall()
                session = one(c, 'SELECT state,ended_at FROM lead.browser_sessions WHERE id=%s', (ready['session'],))
                assert one(c, 'SELECT 42 AS alive')['alive'] == 42
            assert actions == [{'id': action['id'], 'state': 'unknown'}]
            assert len(attempts) == 1 and attempts[0]['id'] == attempt['id'] and attempts[0]['attempt'] == 1
            assert attempts[0]['outcome'] == 'unknown' and session['ended_at'] is not None
            oracle = board.oracle()
            assert not oracle['posts']
            assert not [item for item in oracle['requests'] if item['method'] == 'POST' and item['path'] == '/posts']
            assert any(item['method'] == 'GET' for item in oracle['requests'][before_reads:])
            report.update(actions=actions, attempts=attempts, billing=billing, oracle=oracle,
                          parent_and_database_survived=True, fake_model_requests=len(context['model_calls']))
            phase('original_unknown_read_only_reconciled_source_zero')
            private_json(STATE / 'manager-death-report.json', report)
            return {'state': 'unknown', 'source_posts': 0, 'attempts': 1}
        finally:
            private_json(folder / 'source-final.json', board.oracle())
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            if writer:
                writer.close()
            kill_recorded(owned)
            for child in children:
                if child.poll() is None:
                    captured = descendants(child.pid)
                    child.terminate()
                    try:
                        await asyncio.to_thread(child.wait, 8)
                    except subprocess.TimeoutExpired:
                        kill_recorded(captured)
                        await asyncio.to_thread(child.wait, 5)
            for log in logs:
                log.close()
            stream.close()
            await stream.wait_closed()
            private_json(folder / 'cleanup.json', {
                'managers': [{'pid': child.pid, 'returncode': child.poll()} for child in children],
                'source_parent_survived': os.getpid(), 'shared_pg_not_stopped': True,
            })

    result = await verify_login(monkeypatch, after_discovery=cut)
    assert result['action_check'] == {'state': 'unknown', 'source_posts': 0, 'attempts': 1}
