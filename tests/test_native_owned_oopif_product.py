"""Real SDK/namespace OOPIF scope regression, with explicit test-server grants.

This does not infer business permissions or create accounts. The server grants
three known origins to A and one to B, using actual gate-owned target IDs.
No model, blind fixture, product authority patch, or opaque-frame fallback.
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

if __name__ != '__main__':
    import pytest
    pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
        reason='Explicit isolated Linux native SDK run required')]


async def sdk_worker(data):
    import logging

    from browser_use import BrowserSession
    from lead_app.browser_execution import (
        ScopeViolation,
        guarded_tools,
        scoped_session_class,
    )

    logging.disable(logging.CRITICAL)
    lock = asyncio.Lock()

    def emit(value):
        print(json.dumps(value), flush=True)

    async def read_scope():
        async with lock:
            emit({'kind': 'scope'})
            return json.loads(await asyncio.to_thread(sys.stdin.readline))['scope']

    async def authorize(name, values, node):
        return {'allowed': name == 'click' and node['target_id'] in session._scope['targets']
            and node['attributes'].get('id') in {'root-click', 'outer-click', 'nested-click', 'b-click',
                'load-late', 'late-outer', 'late-inner'}}

    collected = [0]
    original_summary = BrowserSession.get_browser_state_summary

    async def counted_summary(self, *args, **kwargs):
        collected[0] += 1
        return await original_summary(self, *args, **kwargs)

    BrowserSession.get_browser_state_summary = counted_summary
    session = scoped_session_class()(cdp_url=data['url'], headers=data['headers'], keep_alive=True,
        enable_default_extensions=False)
    await session.start_scoped(data['lease'], read_scope)
    manager = session.session_manager
    original_attached = manager._handle_target_attached

    async def trace_attached(event):
        target = event['targetInfo']
        if target.get('type') == 'iframe' and target['targetId'] not in session._scope['targets']:
            emit({'kind': 'early_attached', 'target_id': target['targetId'], 'at': time.monotonic()})
        return await original_attached(event)

    manager._handle_target_attached = trace_attached
    tools = guarded_tools(session, authorize)

    async def observe():
        state = await session.get_browser_state_summary(include_screenshot=False)
        assert len(state.tabs) == 1 and state.tabs[0].target_id == data['lease']['target_id']
        nodes = state.dom_state.selector_map
        return nodes, {node.attributes['id']: node.target_id for node in nodes.values()
            if node.attributes.get('id') in {'root-click', 'outer-click', 'nested-click', 'b-click',
                'load-late', 'late-outer', 'late-inner'}}

    async def click(identifier):
        nodes, observed = await observe()
        index = next(index for index, node in nodes.items() if node.attributes.get('id') == identifier)
        result = await tools.registry.execute_action('guarded_click', {'index': index})
        assert not result.error, result.error
        return observed

    emit({'kind': 'ready'})
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            operation = command['operation']
            if operation == 'stop':
                emit({'kind': 'result', 'value': {'stopped': True}})
                break
            if operation == 'observe':
                _, value = await observe()
            elif operation == 'click':
                value = await click(command['id'])
            elif operation == 'late':
                await click('load-late')
                value = await click('late-outer')
            elif operation == 'revoked':
                before = collected[0]
                try:
                    await session.get_browser_state_summary(include_screenshot=True, cached=True)
                except ScopeViolation:
                    value = {'refused': True, 'sdk_collection_delta': collected[0] - before}
                else:
                    raise AssertionError('Revoked nested observation was accepted')
                assert value['sdk_collection_delta'] == 0
            else:
                raise AssertionError('Unknown bounded test operation')
            emit({'kind': 'result', 'value': value})
    finally:
        await session.stop()


async def test_allowed_nested_and_late_oopif_survive_revocation_without_affecting_b(monkeypatch):
    import httpx
    from lead_app import network_namespace
    from lead_app.browser_runtime import ControlBridge, NativeNetworkGate
    from lead_app.config import ROOT, STATE, env
    from lead_app.forward_proxy import BrowserForwardProxy
    from playwright.async_api import async_playwright

    assert sys.platform == 'linux' and env().get('LEAD_NETWORK_NAMESPACE') == '1'
    hosts = {'main': 'board.c29-main.test', 'outer': 'panel.c29-frame.test', 'nested': 'deep.c29-nested.test'}
    origins = {role: 'http://' + host for role, host in hosts.items()}
    addresses = {host: '93.184.215.' + str(104 + index) for index, host in enumerate(hosts.values())}
    source_requests, authorizations, early, delayed, children = [], [], [], [], []

    def button(identifier):
        return '<button type=button id="' + identifier + '" onclick="this.dataset.count=String(Number(this.dataset.count||0)+1);this.textContent=\'Expanded \'+this.dataset.count">Expand ' + identifier + '</button>'

    class Site(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            host, path = self.headers.get('Host', '').split(':')[0], urlsplit(self.path).path
            source_requests.append({'host': host, 'path': path, 'method': self.command, 'at': time.monotonic()})
            if host == hosts['main'] and path == '/a':
                body = button('root-click') + '<iframe src="' + origins['outer'] + '/panel"></iframe>'
                body += '<button type=button id=load-late onclick="this.disabled=true;const frame=document.createElement(\'iframe\');frame.src=\'' + origins['outer'] + '/late\';document.body.append(frame)">Load another panel</button>'
            elif host == hosts['main'] and path == '/b':
                body = button('b-click')
            elif host == hosts['outer'] and path in ('/panel', '/late'):
                body = button('late-outer' if path == '/late' else 'outer-click')
                if path == '/panel':
                    body += '<iframe src="' + origins['nested'] + path + '"></iframe>'
            elif host == hosts['nested'] and path == '/panel':
                body = button('nested-click')
            else:
                self.send_error(403)
                return
            raw = ('<!doctype html><html><head><title>Scoped panel</title></head><body>' + body + '</body></html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=site.serve_forever, daemon=True).start()

    async def resolve(_proxy, host):
        if host not in addresses:
            raise ValueError('No other test DNS destination is authorized')
        return addresses[host]

    async def connect(_proxy, ip, port):
        if ip not in addresses.values() or port != 80:
            raise ValueError('No alternate transport is authorized')
        return await asyncio.open_connection('127.0.0.1', site.server_port)

    monkeypatch.setattr(BrowserForwardProxy, 'resolve', resolve)
    monkeypatch.setattr(BrowserForwardProxy, 'connect', connect)
    gate = bridge = context = playwright = None
    proxy = BrowserForwardProxy(str(uuid4()), slot_id='c29-' + uuid4().hex,
        forward_guard=lambda: gate.token if gate else None)
    await proxy.start()
    granted, pages, leases = {}, {}, {}
    late = [False]

    async def until(predicate, timeout=8):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(.02)

    async def stable_frames():
        stable, previous = 0, None
        async with asyncio.timeout(6):
            while stable < 3:
                current = tuple(sorted((session, info['targetId']) for session, info in gate.targets.items()
                    if gate.owners.get(session) == leases['a']['target_id']))
                ready = not any(not item.get('initialized') for item in delayed) and all(
                    urlsplit(frame.url).scheme == 'http' for frame in pages['a'].frames)
                stable = stable + 1 if ready and current == previous else 0
                previous = current
                await asyncio.sleep(.05)

    def scope(name):
        root = leases[name]['target_id']
        return {**leases[name], 'origins': list(granted[name]),
            'targets': {info['targetId']: root for session, info in gate.targets.items() if gate.owners.get(session) == root}}

    class Worker:
        def __init__(self, name, child):
            self.name, self.child = name, child

        async def receive(self):
            while raw := await asyncio.wait_for(self.child.stdout.readline(), 25):
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if message['kind'] == 'scope':
                    if late[0] and self.name == 'a':
                        await stable_frames()
                    self.child.stdin.write((json.dumps({'scope': scope(self.name)}) + '\n').encode())
                    await self.child.stdin.drain()
                elif message['kind'] == 'early_attached':
                    early.append({'worker': self.name, **message})
                elif message['kind'] in ('ready', 'result'):
                    return message.get('value')
            error = (await self.child.stderr.read()).decode()[-3000:]
            raise AssertionError('Native SDK worker ended: ' + error)

        async def command(self, operation, **values):
            self.child.stdin.write((json.dumps({'operation': operation, **values}) + '\n').encode())
            await self.child.stdin.drain()
            return await self.receive()

    temporary = tempfile.TemporaryDirectory(prefix='c29-', dir='/tmp')
    try:
        folder = Path(temporary.name)
        for name in ('storage', 'control'):
            (folder/name).mkdir(mode=0o700)
        (STATE/'assets'/proxy.aid).mkdir(parents=True, mode=0o700)
        with socket.socket() as chooser:
            chooser.bind(('127.0.0.1', 0))
            port = chooser.getsockname()[1]
        binary = Path(env()['LEAD_BROWSER_RUNTIME_DIR'])/'chromium/chrome-linux64/chrome'
        options = network_namespace.chromium_options({'executable_path': str(binary), 'env': {
            key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ')},
            'args': ['--remote-debugging-port=' + str(port), '--remote-debugging-address=127.0.0.1',
                '--site-per-process', '--disable-quic', '--disable-background-networking', '--proxy-bypass-list=<-loopback>']},
            proxy.aid, proxy, folder/'storage', control_directory=folder/'control', cdp_port=port)
        playwright = await async_playwright().start()
        context = await playwright.chromium.launch_persistent_context(str(folder/'storage'), headless=True,
            offline=True, service_workers='allow', proxy=proxy.browser_proxy, **options)
        cdp = await context.new_cdp_session(context.pages[0])
        info = (await cdp.send('Target.getTargetInfo'))['targetInfo']
        await cdp.detach()
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(folder/'control/cdp.sock'))) as client:
            version = (await client.get('http://localhost/json/version')).json()
        browser_path = urlsplit(version['webSocketDebuggerUrl']).path
        async def authorize(lease, target, request):
            url = urlsplit(request['url'])
            allowed = bool(lease and request['method'] == 'GET' and (url.scheme + '://' + url.netloc) in lease['origins'])
            authorizations.append({'method': request['method'], 'host': url.hostname, 'path': url.path,
                'target_id': target['targetId'], 'allowed': allowed, 'at': time.monotonic()})
            if allowed:
                return {'kind': 'read'}
        async def no_claim(*_):
            raise AssertionError('Read-only frame fixture cannot claim an action')
        async def lost():
            return None
        gate = NativeNetworkGate(context, None, authorize=authorize, claim=no_claim, lost=lost, proxy=proxy.browser_proxy)
        await gate.start(str(folder/'control/cdp.sock'), browser_path, info['browserContextId'])
        bridge = await ControlBridge(str(folder/'control/cdp.sock'), browser_path).start()
        await context.set_offline(False)
        for name in ('a', 'b'):
            pages[name] = await context.new_page()
            session = await context.new_cdp_session(pages[name])
            target = (await session.send('Target.getTargetInfo'))['targetInfo']['targetId']
            await session.detach()
            granted[name] = list(origins.values()) if name == 'a' else [origins['main']]
            leases[name] = {'task_id': 'explicit-c29-' + name, 'generation': 1, 'target_id': target}
            await gate.register(target, {**leases[name], 'origins': granted[name]})
            await pages[name].goto(origins['main'] + '/' + name)
        await until(lambda: len(pages['a'].frames) == 3 and all(urlsplit(frame.url).scheme == 'http' for frame in pages['a'].frames))
        await stable_frames()
        attached = gate.attached
        async def delayed_owner(event, parent):
            row = None
            if late[0] and event['targetInfo']['type'] == 'iframe' and gate.owners.get(parent) == leases['a']['target_id']:
                row = {'target_id': event['targetInfo']['targetId'], 'delayed_at': time.monotonic()}
                delayed.append(row)
                # Bounded fixture fault injection, not Chrome's usual ordering:
                # release exactly this event's debugger pause before ownership
                # registration, so the SDK sees attached first. Global Fetch,
                # owner resolution and original authorization remain active.
                await gate.send('Runtime.runIfWaitingForDebugger', {}, event['sessionId'])
                await asyncio.sleep(.35)
                row['gate_owner_start'] = time.monotonic()
            await attached(event, parent)
            if row is not None:
                row['initialized'] = True
        gate.attached = delayed_owner
        workers = []
        for name in ('a', 'b'):
            child_env = {key: value for key, value in os.environ.items() if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TZ')}
            child_env.update(PYTHONPATH=str(Path(env()['LEAD_BROWSER_RUNTIME_DIR'])/'site-packages') + ':' + str(ROOT/'services/api'),
                ANONYMIZED_TELEMETRY='false', BROWSER_USE_CLOUD_SYNC='false', BROWSER_USE_VERSION_CHECK='false', BROWSER_USE_SETUP_LOGGING='false')
            child = await asyncio.create_subprocess_exec(env()['LEAD_BROWSER_RUNTIME_PYTHON'], '-B', '-S', __file__,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env)
            children.append(child)
            child.stdin.write((json.dumps({'url': bridge.url, 'headers': bridge.headers, 'lease': scope(name)}) + '\n').encode())
            await child.stdin.drain()
            worker = Worker(name, child)
            await worker.receive()
            workers.append(worker)
        a, b = workers
        observed = await a.command('observe')
        assert {'root-click', 'outer-click', 'nested-click'} <= set(observed), observed
        assert len({observed[key] for key in ('root-click', 'outer-click', 'nested-click')}) == 3
        for identifier in ('outer-click', 'nested-click'):
            assert any(info['targetId'] == observed[identifier] and info['type'] == 'iframe' for info in gate.targets.values())
            await a.command('click', id=identifier)
            frame = next(frame for frame in pages['a'].frames if frame.url == origins[
                'outer' if identifier == 'outer-click' else 'nested'] + '/panel')
            assert await frame.locator('#' + identifier).get_attribute('data-count') == '1'
        granted['a'].remove(origins['nested'])
        gate.roots[leases['a']['target_id']]['origins'] = granted['a']
        revoked = await a.command('revoked')
        assert revoked == {'refused': True, 'sdk_collection_delta': 0}
        assert 'b-click' in await b.command('click', id='b-click')
        assert await pages['b'].locator('#b-click').get_attribute('data-count') == '1'
        granted['a'].append(origins['nested'])
        restored = await a.command('click', id='nested-click')
        assert 'nested-click' in restored
        nested = next(frame for frame in pages['a'].frames if frame.url == origins['nested'] + '/panel')
        assert await nested.locator('#nested-click').get_attribute('data-count') == '2'
        late[0] = True
        observed = await a.command('late')
        late[0] = False
        assert 'late-outer' in observed
        frame = next(frame for frame in pages['a'].frames if frame.url == origins['outer'] + '/late')
        assert await frame.locator('#late-outer').get_attribute('data-count') == '1'
        assert delayed and any(event['worker'] == 'a' and event['target_id'] == item['target_id']
            and event['at'] < item['gate_owner_start'] for event in early for item in delayed), (early, delayed)
        assert all(any(decision['allowed'] and decision['at'] <= request['at'] and all(
            decision[key] == request[key] for key in ('method', 'host', 'path')) for decision in authorizations)
            for request in source_requests), 'A source request lacked an original gate read authorization'
        print('C29_OWNED_FRAME_DIAGNOSTICS ' + json.dumps({'early': early, 'delayed': delayed,
            'source_requests': source_requests, 'observed': observed, 'revoked': revoked,
            'explicit_test_permissions': origins, 'automatic_business_grant_inference': False}))
        for worker in workers:
            await worker.command('stop')
            await asyncio.wait_for(worker.child.wait(), 8)
    finally:
        print('C29_FRAME_EVENT_DIAGNOSTICS ' + json.dumps({'early': early, 'delayed': delayed,
            'source_requests': source_requests, 'authorizations': authorizations, 'explicit_test_permissions': origins,
            'late_attach_order_forced_by_test': True,
            'automatic_business_grant_inference': False}))
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
        await proxy.close()
        if bridge:
            await bridge.close()
        if gate:
            await gate.close()
        if context:
            await context.close()
        if playwright:
            await playwright.stop()
        site.shutdown()
        site.server_close()
        temporary.cleanup()


if __name__ == '__main__':
    asyncio.run(sdk_worker(json.loads(sys.stdin.readline())))
