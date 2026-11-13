"""One original SSO/SW/Action chain with owner-approved public frame input.

The frozen site and the existing SW composition/HTTP model recipe are reused
unchanged. This variant adds only one static, query-free fourth-origin document
and a home-page iframe. Its permission comes from the real owner API after the
original connection naturally hands its new claim back to the owner.
"""
import asyncio
import hashlib
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')

PUBLIC_ORIGIN = 'http://readonly-reference.test'
PUBLIC_URL = PUBLIC_ORIGIN + '/public-reference'
PUBLIC_IP = '93.184.215.198'
PUBLIC_TEXT = 'Public static member reference for the combined browser test'
PUBLIC_DOCUMENT = '<!doctype html><meta charset="utf-8"><main><p>' + PUBLIC_TEXT + '</p></main>'


@pytest.mark.asyncio
async def test_original_sso_owner_public_frame_and_worker_actions_share_one_instance(monkeypatch):
    import native_browser_boundary_fixture as frozen
    import test_native_auth_flow_product as sso
    import test_native_boundary_product as boundary
    import test_native_sw_action_composition_product as composition
    from lead_app import api, network_namespace
    from lead_app.account_connection import execute_account
    from lead_app.browser_runtime import NativeNetworkGate, PersistentRuntime
    from lead_app.config import STATE
    from lead_app.db import one, tx
    from lead_app.forward_proxy import BrowserForwardProxy
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.tools import AccountInput
    from lead_app.native_action import NativeAction
    from lead_app.native_frame_roles import readonly_node
    from lead_app.native_task import NativeTask
    from lead_app.security import internal_token, token

    requests, visibility, roles, decisions, auth_events, action_phases, evidence = [], [], [], [], [], [], {}

    class PublicDocument(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append({'method': 'GET', 'exact_resource': self.path == '/public-reference',
                'cookie': bool(self.headers.get('Cookie')), 'referer': bool(self.headers.get('Referer'))})
            body = PUBLIC_DOCUMENT.encode() if self.path == '/public-reference' else b''
            self.send_response(200 if body else 404)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append({'method': 'POST', 'exact_resource': self.path == '/public-reference',
                'cookie': bool(self.headers.get('Cookie')), 'referer': bool(self.headers.get('Referer'))})
            self.send_response(405)
            self.send_header('Content-Length', '0')
            self.end_headers()

    public = ThreadingHTTPServer(('127.0.0.1', 0), PublicDocument)
    threading.Thread(target=public.serve_forever, daemon=True).start()

    class PublicFrameSite(frozen.BoundarySite):
        def _handler(self):
            handler = super()._handler()
            original_send = handler.send

            def send(current, status, body='', **kwargs):
                if current.role == 'main' and current.path_only == '/home' and 'Signed in as ' in body:
                    body = body.replace('</h1>', '</h1><iframe title="Public member reference" '
                        'style="height:50px;width:95%" src="' + PUBLIC_URL + '"></iframe>', 1)
                return original_send(current, status, body, **kwargs)

            handler.send = send
            return handler

    monkeypatch.setattr(frozen, 'BoundarySite', PublicFrameSite)
    original_setattr = monkeypatch.setattr

    # The old harness installs its strict routes later. Extend only this exact
    # fourth transport input, preserving every existing resolver/proxy check.
    def with_public_transport(target, name, value, *args, **kwargs):
        if target is BrowserForwardProxy and name == 'resolve':
            previous = value

            async def resolve(proxy, host):
                return PUBLIC_IP if host == urlsplit(PUBLIC_ORIGIN).hostname else await previous(proxy, host)

            value = resolve
        elif target is BrowserForwardProxy and name == 'connect':
            previous = value

            async def connect(proxy, ip, port):
                if ip == PUBLIC_IP and port == 80:
                    return await asyncio.open_connection('127.0.0.1', public.server_port)
                return await previous(proxy, ip, port)

            value = connect
        elif target is network_namespace and name == 'chromium_options':
            previous = value

            def options(*values, **options_kwargs):
                result = previous(*values, **options_kwargs)
                result['args'] = [arg + ',' + PUBLIC_ORIGIN if arg.startswith(
                    '--unsafely-treat-insecure-origin-as-secure=') and PUBLIC_ORIGIN not in arg else arg
                    for arg in result['args']]
                return result

            value = options
        return original_setattr(target, name, value, *args, **kwargs)

    monkeypatch.setattr = with_public_transport
    original_model = composition.mechanical_model

    def observed_model(site, calls):
        server, state, connection = original_model(site, calls)
        original_post = server.RequestHandlerClass.do_POST

        def post(current):
            raw = current.rfile.read(int(current.headers['Content-Length']))
            visibility.append({'kind': state['kind'], 'step': state['step'],
                'public_text_visible': PUBLIC_TEXT.encode() in raw})
            current.rfile = io.BytesIO(raw)
            return original_post(current)

        server.RequestHandlerClass.do_POST = post
        return server, state, connection

    monkeypatch.setattr(composition, 'mechanical_model', observed_model)
    original_descriptor = NativeTask.descriptor

    async def descriptor(controller):
        result = await original_descriptor(controller)
        actual = [role for role in controller.lease.get('frame_roles', []) if role['resource_url'] == PUBLIC_URL]
        if actual:
            roles.append({'task_id': controller.task_id, 'runtime': id(controller.runtime),
                'origin_projected': PUBLIC_ORIGIN in result['origins'],
                'read_only': all(role.get('frame_target') and readonly_node(controller.lease,
                    {'target_id': role['frame_target']}) for role in actual)})
        return result

    monkeypatch.setattr(NativeTask, 'descriptor', descriptor)

    def action_phase(controller, operation, error=None):
        scope = controller.lease or {}
        claim = scope.get('claim') or {}
        row = {'operation': operation, 'error_type': type(error).__name__ if error else None,
            'value_error_code': str(error) if isinstance(error, ValueError) and re.fullmatch(r'[a-z_]{1,100}', str(error)) else None,
            'task_present': bool(controller.task_id), 'generation_present': scope.get('task_generation') is not None,
            'target_present': bool(scope.get('target_id') in controller.runtime.pages),
            'role_claims': [{'equals_scope': role.get('claim') == scope.get('claim'),
                'attempt_token_equal': (role.get('claim') or {}).get('attempt_token') == claim.get('attempt_token')}
                for role in scope.get('frame_roles', [])]}
        try:
            with tx(controller.actor.aid) as c:
                task = one(c, "SELECT result#>>'{runtime,native_phase}' AS phase FROM lead.tasks WHERE id=%s", (controller.task_id,))
            row['phase'] = task['phase'] if task and re.fullmatch(r'[a-z_]{1,100}', task['phase'] or '') else None
        except Exception as observer_error:
            row['observer_error_type'] = type(observer_error).__name__
        action_phases.append(row)

    def observed_action(operation, original):
        async def call(controller, *args, **kwargs):
            try:
                result = await original(controller, *args, **kwargs)
            except Exception as error:
                action_phase(controller, operation, error)
                raise
            action_phase(controller, operation)
            return result
        return call

    for operation in ('start', 'descriptor', 'baseline'):
        monkeypatch.setattr(NativeAction, operation, observed_action(operation, getattr(NativeAction, operation)))
    original_authorize = PersistentRuntime.authorize_request
    auth_paths = {'/sso/start', '/authorize', '/session/complete'}

    async def authorize(runtime, scope, target, request):
        path = urlsplit(request['url']).path
        try:
            decision = await original_authorize(runtime, scope, target, request)
        except Exception as error:
            if path in auth_paths:
                auth_events.append({'stage': 'authorize', 'path': path, 'method': request['method'],
                    'error_type': type(error).__name__, 'scope_present': bool(scope), 'target_type': target.get('type')})
            raise
        if path in auth_paths:
            auth_events.append({'stage': 'authorize', 'path': path, 'method': request['method'],
                'allowed': bool(decision), 'kind': (decision or {}).get('kind'), 'target_type': target.get('type')})
        if request['url'] == PUBLIC_URL and decision:
            decisions.append({'method': request['method'], 'runtime': id(runtime),
                'public_frame': decision.get('public_frame') is True, 'kind': decision.get('kind')})
        return decision

    monkeypatch.setattr(PersistentRuntime, 'authorize_request', authorize)
    original_owner = NativeNetworkGate.resolve_owner

    async def resolve_owner(gate, event):
        path = urlsplit(event['request']['url']).path
        try:
            result = await original_owner(gate, event)
        except Exception as error:
            if path in auth_paths:
                auth_events.append({'stage': 'resolve_owner', 'path': path, 'method': event['request']['method'],
                    'error_type': type(error).__name__, 'resource_type': event.get('resourceType')})
            raise
        if path in auth_paths:
            auth_events.append({'stage': 'resolve_owner', 'path': path, 'method': event['request']['method'],
                'target_type': result[1].get('type'), 'resource_type': event.get('resourceType')})
        return result

    monkeypatch.setattr(NativeNetworkGate, 'resolve_owner', resolve_owner)

    async def sso_with_review(patch, owner_entry):
        assert patch is monkeypatch and owner_entry is True

        async def check(data):
            actor, controller, page, site = (data[key] for key in ('actor', 'controller', 'page', 'site'))
            aid, task, claim, pending = actor.aid, data['task'], data['claim'], data['pending']
            connection = str(pending['takeover_id'])
            initial_calls = len(data['context']['model_calls'])
            evidence['runtime'] = id(actor.native_runtime)
            evidence['profile_id'] = str(actor.profile['id'])
            await actor.context.clear_cookies(name='board_session', domain=site.hosts['main'])
            await page.goto(site.origin() + '/home', wait_until='domcontentloaded')
            outsider = await actor.context.new_page()
            try:
                await outsider.evaluate('(url)=>{window.open(url,"wrong-opener");}', site.origin() + '/sso/start')
                await asyncio.sleep(.15)
                assert data['source_count']('GET', '/sso/start') == 0
            finally:
                await outsider.close()
            await controller.handoff('请在原窗口选择身份登录并完成本人步骤。')
            waiting = await execute_account(aid, task, AccountInput.model_validate(task['input']['spec']['payload']))
            assert waiting.outcome == 'waiting' and waiting.wait.responsibility == 'owner'
            finish(claim, waiting)

            async with httpx.AsyncClient(base_url=data['context']['gateway_endpoint'],
                headers={'Authorization': 'Bearer ' + internal_token()}, timeout=20) as gateway, \
                httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8088',
                    cookies={'lead_session': token('owner')}, headers={'Origin': 'http://127.0.0.1:3100',
                    'X-Lead-Request': '1'}, timeout=20) as owner:
                async def current():
                    response = await owner.get(f'/api/agents/{aid}/connections/{connection}')
                    assert response.status_code == 200, response.text
                    return response.json()

                async def send(kind, **values):
                    status = await current()
                    response = await gateway.post(f'/agents/{aid}/input', json={'takeover': connection,
                        'challenge_version': status['challenge_version'], 'kind': kind, **values})
                    assert response.status_code == 200, response.text

                async def resume():
                    status = await current()
                    stale = await owner.post(f'/api/agents/{aid}/connections/{connection}/control', json={
                        'action': 'resume', 'challenge_version': status['challenge_version'] - 1})
                    assert stale.status_code == 409
                    response = await owner.post(f'/api/agents/{aid}/connections/{connection}/control', json={
                        'action': 'resume', 'challenge_version': status['challenge_version']})
                    assert response.status_code == 200, response.text

                bounds = await page.get_by_role('button', name='Use identity sign-in').bounding_box()
                async with actor.context.expect_page() as opened:
                    await send('click', x=bounds['x'] + bounds['width']/2, y=bounds['y'] + bounds['height']/2)
                popup = await opened.value
                try:
                    await popup.wait_for_url(site.origin('identity') + '/authorize?*')
                except Exception:
                    flow = controller.lease.get('auth_flow') or {}
                    gate = actor.native_runtime.gate
                    popup_target = flow.get('popup_target')
                    evidence['sso_failure'] = {'popup_path': urlsplit(popup.url).path,
                        'root_path': urlsplit(page.url).path, 'flow_present': bool(flow),
                        'popup_reserved': bool(popup_target), 'opener_is_root': flow.get('opener_target') == controller.lease['target_id'],
                        'callback': bool(flow.get('callback')), 'completed': bool(flow.get('completed')),
                        'foreign_seen': bool(flow.get('foreign_seen')), 'hops': flow.get('hops'),
                        'paths': [urlsplit(url).path for url in flow.get('urls', [])],
                        'popup_document_present': popup_target in gate.documents,
                        'scope_document_current': controller.lease.get('document_token') == gate.documents.get(controller.lease['target_id']),
                        'source_start_gets': data['source_count']('GET', '/sso/start'),
                        'source_idp_gets': data['source_count']('GET', '/authorize'),
                        'source_idp_posts': data['source_count']('POST', '/authorize'),
                        'source_callback_gets': data['source_count']('GET', '/session/complete')}
                    raise
                assert popup is await controller.owner_page()
                assert controller.lease['auth_flow']['popup_target'] not in (
                    await actor.native_runtime.scope_view(controller.lease['target_id']))['targets']
                assert data['source_count']('POST', '/authorize') == 0
                for field, value in (('username', site.username), ('password', site.password)):
                    bounds = await popup.locator('input[name=' + field + ']').bounding_box()
                    await send('click', x=bounds['x'] + bounds['width']/2, y=bounds['y'] + bounds['height']/2)
                    await send('text', text=value)
                await send('key', text='Enter')
                await popup.wait_for_url(site.origin() + '/session/complete?*', timeout=5000)
                assert data['source_count']('POST', '/authorize') == site.oracle()['counts']['sso_completions'] == 1
                for callback in (popup.url, popup.url + '&unrelated=changed'):
                    assert await page.evaluate('(url)=>fetch(url).then(()=>false).catch(()=>true)', callback)
                    assert data['source_count']('GET', '/session/complete') == 1
                await asyncio.sleep(2.1)
                await resume()
                ref = {'agent_id': aid, 'task_id': str(task['id']), 'task_generation': task['input']['task_generation']}
                first = advance(ref)
                assert first['state'] == 'ready', first
                with tx(aid) as c:
                    task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (task['id'],))
                    reservations_before = {str(row['id']) for row in c.execute('SELECT id FROM lead.budget_reservations').fetchall()}
                connection_workdir = STATE/'browser-use-runs'/('connection-' + connection)
                workdir_before = connection_workdir.exists()
                # This is the real native preflight, not a test-induced handoff.
                review_wait = await execute_account(aid, task, AccountInput.model_validate(task['input']['spec']['payload']))
                with tx(aid) as c:
                    reservations_after = {str(row['id']) for row in c.execute('SELECT id FROM lead.budget_reservations').fetchall()}
                evidence['before_approval'] = {'task_id': str(task['id']), 'connection_id': connection,
                    'reservation_ids_before': sorted(reservations_before), 'reservation_ids_after': sorted(reservations_after),
                    'workdir_before': workdir_before, 'workdir_after': connection_workdir.exists(),
                    'sdk_absence_evidence': 'native_runner.connect reserves before create_subprocess_exec; no reservation or workdir added'}
                assert review_wait.outcome == 'waiting' and review_wait.wait.responsibility == 'owner', review_wait
                assert reservations_after == reservations_before
                assert not workdir_before and not connection_workdir.exists()
                finish(first['claim'], review_wait)
                assert controller.page is page and popup.is_closed() and page.url == site.origin() + '/home'
                assert len(data['context']['model_calls']) == initial_calls and not requests
                with tx(aid) as c:
                    assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (task['id'],))['state'] == 'waiting_user'
                listing = await owner.get(f'/api/agents/{aid}/connections/{connection}/read-dependencies')
                assert listing.status_code == 200, listing.text
                review = listing.json()
                assert len(review['candidates']) == 1, review
                candidate = review['candidates'][0]
                assert candidate['state'] == 'pending' and candidate['resource_url'] == PUBLIC_URL
                assert candidate['parent_url'] == site.origin() + '/home' and candidate['role'] == 'read_only_frame'
                approved = await owner.post(f'/api/agents/{aid}/connections/{connection}/read-dependencies', json={
                    'candidate_id': candidate['candidate_id'], 'challenge_version': review['challenge_version'], 'decision': 'approve'})
                assert approved.status_code == 200, approved.text
                assert approved.json()['candidates'][0]['state'] == 'approved'
                await page.frame_locator('iframe[title="Public member reference"]').get_by_text(PUBLIC_TEXT, exact=True).wait_for(timeout=8000)
                assert requests and all(row == {'method': 'GET', 'exact_resource': True, 'cookie': False, 'referer': False} for row in requests)
                await asyncio.sleep(2.1)
                await resume()
                final = advance(ref)
                assert final['state'] == 'ready', final
                assert final['claim']['attempt_token'] != first['claim']['attempt_token']

            await controller.start()
            assert controller.page is page and controller.lease['authentication_dispatched']
            await controller.inspect_self({'self_link_selector': 'a[rel~=me]', 'acting_subject_selector': 'main > p:first-of-type',
                'self_subject_prefix': 'Signed in as ', 'self_subject_suffix': ''})
            observed = await controller.report_identity({'subject_selector': 'main > p'})
            assert observed['status'] == 'authenticated' and observed['operating_subject']['external_id'] == site.username
            with tx(aid) as c:
                task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (task['id'],))
            result = await execute_account(aid, task, AccountInput.model_validate(task['input']['spec']['payload']))
            finish(final['claim'], result)
            assert result.outcome == 'completed'
            with tx(aid) as c:
                account = one(c, 'SELECT state,external_account_id,binding_version FROM lead.social_accounts WHERE id=%s', (pending['account_id'],))
                assert account['state'] == 'connected' and account['external_account_id'] == site.username
                source = one(c, 'SELECT connection_data FROM lead.takeover_sessions WHERE id=%s', (connection,))['connection_data']['read_dependencies'][0]
                assert source['state'] == 'approved' and source['bound']['binding_version'] == account['binding_version']
                assert source['resource_url'] == PUBLIC_URL
                assert one(c, 'SELECT state FROM lead.tasks WHERE id=%s', (task['id'],))['state'] == 'succeeded'
            assert len(data['context']['model_calls']) == initial_calls and not site.oracle()['posts']
            callback = next(url for url in controller.lease['auth_flow']['urls'] if urlsplit(url).path == '/session/complete')
            worker = next(worker for worker in actor.context.service_workers if worker.url == site.origin() + '/sw.js')
            await worker.evaluate('(url)=>fetch(url).then(response=>response.status).catch(error=>error.name)', callback)
            assert data['source_count']('GET', '/session/complete') == 1
            evidence.update(owner_reviews=1, owner_waits=2, original_connection_completed=True,
                extra_sso_model_requests=0, approved_source_bound=True)
            return {'original_connection_completed': True, 'owner_idp_post': 1, 'extra_model_requests': 0}

        return await boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch, boundary_hook=check)

    monkeypatch.setattr(sso, 'test_observed_popup_owner_identity_and_callback_complete_original_connection', sso_with_review)
    try:
        await composition.test_sso_bound_original_actions_use_worker_transport_and_replay_no_new_post(monkeypatch)
        assert any(row['public_text_visible'] and row['kind'] == 'discovery' for row in visibility), visibility
        assert roles and all(row['read_only'] and row['origin_projected'] and row['runtime'] == evidence['runtime'] for row in roles)
        assert decisions and all(row['public_frame'] and row['kind'] == 'read' and row['method'] == 'GET'
            and row['runtime'] == evidence['runtime'] for row in decisions)
        assert requests and all(row == {'method': 'GET', 'exact_resource': True, 'cookie': False, 'referer': False} for row in requests)
        evidence.update(same_instance=True, sdk_public_text_seen=True, all_public_reads_anonymous=True,
            source_gets=len(requests), readonly_task_ids=sorted({row['task_id'] for row in roles}))
    finally:
        evidence.update(requests=requests, model_visibility=visibility, roles=roles, decisions=decisions, auth_events=auth_events,
            action_phases=action_phases,
            variant_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            static_document_sha256=hashlib.sha256(PUBLIC_DOCUMENT.encode()).hexdigest(),
            original_composition_sha256=hashlib.sha256(Path(composition.__file__).read_bytes()).hexdigest(),
            frozen_fixture_sha256=hashlib.sha256(Path(frozen.__file__).read_bytes()).hexdigest(),
            explicit_transport={'origin': PUBLIC_ORIGIN, 'public_ip': PUBLIC_IP, 'resource': PUBLIC_URL},
            automatic_permission_inference=False, nested_foreign_frame=False)
        (STATE/'native-sw-sso-public-frame.json').write_text(json.dumps(evidence, indent=2))
        monkeypatch.setattr = original_setattr
        public.shutdown()
        public.server_close()
