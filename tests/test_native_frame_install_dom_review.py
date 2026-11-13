"""Real Chromium DOM cuts around approval/installation; no DB, model or account.

A local first-party response releases a DOM mutation after the synthetic DB CAS.
This tests the actual JavaScript install guard, not a mocked evaluate result.
"""
import asyncio
import copy
import os
import threading
import time
from contextlib import contextmanager
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS') != '1',
    reason='Explicit isolated Chromium DOM verification required')


@pytest.mark.asyncio
@pytest.mark.parametrize('cut', ['approval_post_cas', 'prepare_await'])
async def test_frame_install_does_not_adopt_a_later_dom_without_fresh_proof(monkeypatch, cut):
    from lead_app import native_frame_roles as roles
    from lead_app.growth.contracts import utcnow

    ready, release = threading.Event(), threading.Event()
    checked = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path != '/cut':
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            ready.set()
            release.wait(5)
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    parent = f'http://localhost:{server.server_port}/parent'
    resource = f'http://127.0.0.1:{server.server_port}/public'
    changed = f'http://127.0.0.1:{server.server_port}/different'
    asset = Path(os.environ.get('LEAD_BROWSER_RUNTIME_DIR', '/missing')) / 'chromium/chrome-linux64/chrome'
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-background-networking'],
               'proxy': {'server': 'http://127.0.0.1:1', 'bypass': 'localhost,127.0.0.1'}}
    if asset.is_file():
        options['executable_path'] = str(asset)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**options)
            context = await browser.new_context(service_workers='block')
            async def route(request):
                if request.request.url == parent:
                    await request.fulfill(body=f'<!doctype html><iframe id=source src="{resource}"></iframe>')
                elif request.request.url.endswith('/cut'):
                    await request.continue_()  # Sole allowed network is the owned loopback barrier.
                else:
                    await request.abort()
            await context.route('**/*', route)
            page = await context.new_page()
            try:
                await page.goto(parent)
                cdp = await context.new_cdp_session(page)
                await cdp.send('Page.enable')
                info = (await cdp.send('Target.getTargetInfo'))['targetInfo']
                first = (await cdp.send('Page.getFrameTree'))['frameTree']['frame']
                root = info['targetId']
                document = [first['id'], first['loaderId'], first['url']]
                gate = SimpleNamespace(documents={root: copy.deepcopy(document)}, network_sequence=0)
                def navigated(event):
                    frame = event['frame']
                    if not frame.get('parentId'):
                        gate.documents[root] = [frame['id'], frame['loaderId'], frame['url']]
                cdp.on('Page.frameNavigated', navigated)
                async def protect(url):
                    pass
                gate.protect_frame_responses = protect
                scope = {'target_id': root, 'origins': [parent], 'account_id': 'synthetic-account',
                    'control_version': 1, 'task_id': 'synthetic-task', 'task_generation': 1,
                    'claim': {'attempt_token': 'synthetic-claim'}, 'generation': 7}
                source = {'candidate_id': 'a' * 64, 'parent_url': parent, 'resource_url': resource,
                    'state': 'pending' if cut == 'approval_post_cas' else 'approved', 'reason': None,
                    'role': 'read_only_frame', 'document': copy.deepcopy(document),
                    'expires_at': (utcnow() + timedelta(minutes=10)).isoformat(), 'challenge_version': 1,
                    'original_attributes': {'sandbox': None, 'credentialless': False, 'name': '', 'title': ''}}
                if cut == 'prepare_await':
                    source['source_proof'] = roles.digest(source)
                connection = {'id': '00000000-0000-0000-0000-000000000042', 'state': 'active',
                    'control_mode': 'owner', 'control_version': 1, 'challenge_version': 1, 'input_seq': 1,
                    'connection_data': {'read_dependencies': [source]}}
                runtime = SimpleNamespace(gate=gate, frame_role_locks={}, current_scope=lambda _: None,
                    actor=SimpleNamespace(aid='00000000-0000-0000-0000-000000000041',
                        profile={'id': 'synthetic-profile'}))
                controller = SimpleNamespace(runtime=runtime, actor=runtime.actor, lease=scope, page=page,
                    snapshot=lambda: ({}, connection, {}))
                committed = []
                class Result:
                    rowcount = 1
                    def fetchone(self):
                        return copy.deepcopy(connection)
                    def fetchall(self):
                        return [copy.deepcopy(connection)]
                class DB:
                    def execute(self, sql, values):
                        if sql.startswith('UPDATE'):
                            connection['connection_data'].update(copy.deepcopy(values[0]))
                            committed.append(copy.deepcopy(connection['connection_data']))
                        return Result()
                @contextmanager
                def tx(*args):
                    yield DB()
                    if cut == 'approval_post_cas' and any(row['state'] == 'approved'
                            for row in connection['connection_data']['read_dependencies']):
                        # The CAS is now committed. Chrome continues while this
                        # independent test boundary blocks the Python thread.
                        release.set()
                        time.sleep(.12)
                async def candidates(controller):
                    pass
                async def conflict(runtime, scope, url):
                    checked.append(True)
                    if cut == 'approval_post_cas':
                        await page.evaluate('url=>{window.cut=fetch("/cut").then(()=>{document.querySelector("iframe").src=url})}', changed)
                        assert await asyncio.to_thread(ready.wait, 3)
                    else:
                        await page.reload()
                    return None
                monkeypatch.setattr(roles, 'tx', tx)
                monkeypatch.setattr(roles, 'j', lambda value: value)
                monkeypatch.setattr(roles, 'require_agent', lambda *args: {'control_version': 1})
                monkeypatch.setattr(roles, 'candidates', candidates)
                monkeypatch.setattr(roles, 'known_conflict', conflict)
                monkeypatch.setattr(roles, 'connection_sources', lambda *args: copy.deepcopy(connection['connection_data']['read_dependencies']))
                if cut == 'approval_post_cas':
                    try:
                        await roles.decide(controller, {'candidate_id': 'a' * 64, 'challenge_version': 1, 'decision': 'approve'})
                    except Exception:
                        pass
                    assert release.is_set(), 'The test must reach the successful CAS cut'
                    await page.evaluate('window.cut')
                    assert await page.locator('iframe').get_attribute('src') == changed
                    assert not any(row['state'] == 'approved' for row in connection['connection_data']['read_dependencies'])
                else:
                    try:
                        await roles.prepare(runtime, scope, page)
                    except ValueError:
                        pass
                    else:
                        assert scope.get('frame_roles')
                        assert all(role['document'] == gate.documents[root] for role in scope['frame_roles'])
                    assert checked and gate.documents[root] != document, 'The awaited reload cut must really occur'
                await cdp.detach()
            finally:
                release.set()
                await context.close()
                await browser.close()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(2)
