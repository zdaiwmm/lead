"""Real manager/gate diagnostics for the frozen three-site HTTP fixture.

This is a current ConnectionRef/session entry, not a claim that the boundary
site has a completed private-self producer or final NativeResearch permission.
No ActionRef, publication, model tool recipe or synthesized DB claim is used.
Only test DNS/proxy transport aliases and exact HTTP trust origins are changed.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required',
)


@pytest.mark.asyncio
async def test_connection_session_keeps_sw_cache_and_nonpublication_posts(monkeypatch, *, boundary_hook=None):
    from lead_app import account_profiles, api, browser, network_namespace
    from lead_app.account_connection import begin
    from lead_app.browser_runtime import NativeNetworkGate, PersistentRuntime
    from lead_app.connection_control import LoginInput
    from lead_app.db import one, tx
    from lead_app.forward_proxy import BrowserForwardProxy
    from lead_app.growth.runtime import advance
    from lead_app.native_connection import NativeConnection
    from lead_app.security import token
    from native_browser_boundary_fixture import BoundarySite
    from native_connection_product import verify_login

    assert sys.platform == 'linux' and os.environ.get('LEAD_NETWORK_NAMESPACE') == '1'
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    site = BoundarySite()
    listen_port = site.server.server_port
    # Canonical :80 virtual authorities are only transport aliases. The frozen
    # handler/DOM/SW/forms/POST semantics remain unchanged on the local listener.
    site.origin = lambda role='main': 'http://' + site.hosts[role]
    origins = [site.origin(role) for role in site.hosts]
    addresses = {host: '93.184.215.' + str(94 + number) for number, host in enumerate(site.hosts.values())}
    decisions = []
    upstream_events = {}
    original_paused = NativeNetworkGate.paused
    original_resolve_owner = NativeNetworkGate.resolve_owner
    original_send = NativeNetworkGate.send
    post_seen = {path: asyncio.Event() for path in ('/session', '/session/refresh', '/read')}
    original_options = network_namespace.chromium_options
    original_authorize = PersistentRuntime.authorize_request

    def test_http_options(options, *args, **kwargs):
        options = {**options, 'args': [*options['args'],
            '--unsafely-treat-insecure-origin-as-secure=' + ','.join(origins)]}
        return original_options(options, *args, **kwargs)

    async def test_resolve(_proxy, host):
        if host in addresses:
            return addresses[host]
        if host == '93.184.215.14':  # the original real login bootstrap only
            return host
        raise ValueError('The test has no other DNS or network destination')

    async def trace_gate(runtime, scope, target, request):
        parsed = urlsplit(request['url'])
        raised = None
        try:
            decision = await original_authorize(runtime, scope, target, request)
        except Exception as error:
            raised = error
            decision = None
        if parsed.hostname in addresses and (request['method'] == 'POST' or parsed.path in ('/sw.js', '/cache/rules')):
            decisions.append({'method': request['method'], 'url': request['url'], 'target_type': target['type'],
                'gate_error': type(raised).__name__ if raised else None,
                'scope_task_id': (scope or {}).get('task_id'),
                'scope_connection_ref': (scope or {}).get('connection_ref'),
                'scope_account_id': (scope or {}).get('account_id'),
                'allowed': bool(decision), 'decision_kind': (decision or {}).get('kind'),'read_proof':(decision or {}).get('read_proof')})
            if request['method'] == 'POST' and parsed.path in post_seen:
                post_seen[parsed.path].set()
        if raised:
            raise raised
        return decision

    async def trace_paused(gate, event):
        if urlsplit(event['request']['url']).hostname in addresses:
            upstream_events[event['requestId']] = {'fetch_request_id': event['requestId'],
                'network_id': event.get('networkId'), 'fetch_frame_id': event.get('frameId'),
                'resource_type': event.get('resourceType'), 'url': event['request']['url'],
                'method': event['request']['method'], 'control_token_present': bool(gate.token),
                'managed_context_id': gate.context_id, 'has_post_data': event['request'].get('hasPostData', False),
                'post_data_present': 'postData' in event['request']}
        return await original_paused(gate, event)

    async def trace_owner(gate, event):
        entry = upstream_events.get(event['requestId'])
        try:
            result = await original_resolve_owner(gate, event)
            if entry is not None:
                session, target = result
                entry.update(resolved_session=session, resolved_target_type=target.get('type'),
                    resolved_target_id=target.get('targetId'), resolved_target_url=target.get('url'),
                    resolved_context_id=target.get('browserContextId'), owner_root=gate.owners.get(session))
            return result
        except Exception as error:
            if entry is not None:
                entry.update(resolve_error=type(error).__name__, resolve_reason=str(error)[:120])
            raise
        finally:
            if entry is not None:
                entry['network_candidates'] = [{'network_id': request_id, 'session': session,
                    'frame_id': recorded.get('frameId'), 'network_type': recorded.get('type'),
                    'request_url': recorded['request'].get('url'), 'request_method': recorded['request'].get('method'),
                    'target_type': gate.targets.get(session, {}).get('type'),
                    'target_context_id': gate.targets.get(session, {}).get('browserContextId'),
                    'session_current': session in gate.targets}
                    for (request_id, session), recorded in gate.network.items() if request_id == event.get('networkId')]

    async def trace_fetch_result(gate, method, params=None, session=None):
        entry = upstream_events.get((params or {}).get('requestId'))
        if entry is not None and method in ('Fetch.failRequest', 'Fetch.continueRequest', 'Fetch.fulfillRequest'):
            entry.update(fetch_terminal=method, fetch_error_reason=(params or {}).get('errorReason'))
        return await original_send(gate, method, params, session)

    monkeypatch.setattr(NativeNetworkGate, 'paused', trace_paused)
    monkeypatch.setattr(NativeNetworkGate, 'resolve_owner', trace_owner)
    monkeypatch.setattr(NativeNetworkGate, 'send', trace_fetch_result)

    monkeypatch.setattr(network_namespace, 'chromium_options', test_http_options)
    monkeypatch.setattr(BrowserForwardProxy, 'resolve', test_resolve)
    monkeypatch.setattr(PersistentRuntime, 'authorize_request', trace_gate)
    site.start()

    def source_count(method, path):
        return sum(row['method'] == method and row['path'] == path for row in site.oracle()['requests'])

    async def check(context):
        aid = context['aid']
        actor = await browser.gateway.get(aid, context['task']['input']['spec']['payload']['profile_id'])
        assert context['result'].outcome == 'completed'
        await actor.connection_pages[str(context['pending']['takeover_id'])].reconcile()
        previous_connect = BrowserForwardProxy.connect

        async def local_transport(proxy, ip, port):
            if ip in addresses.values() and port == 80:
                return await asyncio.open_connection('127.0.0.1', listen_port)
            return await previous_connect(proxy, ip, port)
        monkeypatch.setattr(BrowserForwardProxy, 'connect', local_transport)
        pending = await begin(aid, 'web', site_url=site.origin() + '/login',
            login=LoginInput(username=site.username, password=site.password))
        with tx(aid) as connection:
            task = one(connection, "SELECT * FROM lead.tasks WHERE input#>>'{spec,payload,takeover_id}'=%s", (str(pending['takeover_id']),))
        assert str(task['input']['spec']['payload']['profile_id']) == str(actor.profile['id'])
        ref = {'agent_id': aid, 'task_id': str(task['id']), 'task_generation': task['input']['task_generation']}
        claim = advance(ref)['claim']
        assert claim['attempt_token']
        controller = NativeConnection(actor, task['id'], pending['takeover_id'])
        actor.connection_pages[str(pending['takeover_id'])] = controller
        await controller.start()
        page = controller.page
        assert page.context is actor.context and await page.evaluate('isSecureContext')
        before_model = len(context['model_calls'])
        snapshot = {}
        page_errors = []
        request_failures = []
        page.on('pageerror', lambda error: page_errors.append(str(error)[:700]))
        actor.context.on('requestfailed', lambda request: request_failures.append({
            'method': request.method, 'url': request.url, 'failure': request.failure})
            if urlsplit(request.url).hostname in addresses else None)
        try:
            cdp = await actor.context.new_cdp_session(page)
            try:
                document = (await cdp.send('DOM.getDocument'))['root']['nodeId']

                async def authorized_node(selector, name, values):
                    node = await cdp.send('DOM.querySelector', {'nodeId': document, 'selector': selector})
                    described = await cdp.send('DOM.describeNode', {'nodeId': node['nodeId']})
                    allowed = await controller.authorize({'name': name, 'values': values, 'node': {
                        'target_id': controller.lease['target_id'], 'backend_node_id': described['node']['backendNodeId']}})
                    assert allowed.get('allowed'), allowed
                for field, value in (('username', site.username), ('password', site.password)):
                    selector = 'input[name=' + field + ']'
                    await authorized_node(selector, 'input', {'text': 'saved_' + field})
                    await page.locator(selector).fill(value)
                await authorized_node('form button', 'click', {})
                await page.locator('form button').click()
            finally:
                await cdp.detach()
            await page.wait_for_url(site.origin() + '/home')
            assert source_count('POST', '/session') == 1
            cookie = next(item for item in await actor.context.cookies(site.origin()) if item['name'] == 'board_session')
            assert cookie['httpOnly'] and 'board_session' not in await page.evaluate('document.cookie')
            try:
                await page.get_by_text('Background service ready', exact=True).wait_for(timeout=10000)
            except Exception as error:
                # Preserve the first SW bootstrap failure, but still make an
                # actual ordinary reading POST under the same current gate.
                reading = await page.evaluate("""async()=>{try{const response=await fetch('/read',{method:'POST',
                    body:new URLSearchParams({query:'welcome',nonce:'read-after-sw-block'})});
                    return {status:response.status};}catch(error){return {error:error.name};}}""")
                await asyncio.wait_for(post_seen['/read'].wait(), 5)
                registrations = await page.evaluate("""async()=>({secure:isSecureContext,url:location.href,
                    controller:navigator.serviceWorker.controller?.scriptURL||null,
                    registrations:(await navigator.serviceWorker.getRegistrations()).map(reg=>({scope:reg.scope,
                    active:reg.active?.state||null,installing:reg.installing?.state||null,waiting:reg.waiting?.state||null}))})""")
                refs = await page.locator('[src],a[href],form[action]').evaluate_all("""nodes=>nodes.map(node=>({tag:node.tagName,
                    ref:node.src||node.href||node.action}))""")
                children, pending_pids = set(), [os.getpid()]
                while pending_pids:
                    pid = pending_pids.pop()
                    try:
                        direct = (Path('/proc')/str(pid)/'task'/str(pid)/'children').read_text().split()
                    except OSError:
                        continue
                    for child in direct:
                        if child not in children:
                            children.add(child)
                            pending_pids.append(int(child))
                argv = []
                for pid in children:
                    try:
                        args = (Path('/proc')/pid/'cmdline').read_bytes().decode().split('\0')
                    except OSError:
                        continue
                    if any(arg.startswith('--remote-debugging-port=') for arg in args):
                        argv.append([arg for arg in args if arg])
                snapshot.update(sw_bootstrap_ready=False, sw_state=registrations, page_errors=page_errors,
                    request_failures=request_failures, actual_chromium_argv=argv, html_refs=refs,
                    html_has_random_listener_port=any(':'+str(listen_port) in item['ref'] for item in refs),
                    source_counts={path:source_count(method,path) for method,path in (
                        ('POST','/session'),('GET','/sw.js'),('GET','/cache/rules'),('POST','/read'),('POST','/session/refresh'))},
                    read_result=reading, gate_events=decisions, upstream_fetch_events=list(upstream_events.values()),
                    mapped_virtual_origins=origins,
                    actual_local_listener='127.0.0.1:' + str(listen_port), transport_aliases=addresses,
                    new_model_requests=len(context['model_calls'])-before_model,
                    refresh_attempted=False, phase='current ConnectionRef/session; SW bootstrap unresolved')
                print('NATIVE_BOUNDARY_BOOTSTRAP_DIAGNOSTICS ' + json.dumps(snapshot, sort_keys=True))
                raise AssertionError('service_worker_not_ready', snapshot) from error
            cache_before = source_count('GET', '/cache/rules')
            synthetic = await page.evaluate("fetch('/sw/synthetic').then(response=>response.text())")
            cache = await page.evaluate("fetch('/cache/rules').then(response=>response.text())")
            snapshot.update(synthetic_text_ok=synthetic == 'Background synthetic note',
                synthetic_source_count=source_count('GET', '/sw/synthetic'),
                cache_text_ok=cache == 'Members may post and reply.', cache_install_source_count=cache_before,
                cache_hit_source_delta=source_count('GET', '/cache/rules')-cache_before,
                httponly_session=True, login_source_post_count=1)

            if boundary_hook:
                return await boundary_hook({'context':context,'actor':actor,'controller':controller,'page':page,'site':site,
                    'source_count':source_count,'decisions':decisions,'post_seen':post_seen,
                    'pending':pending,'task':task,'claim':claim})

            # Normal SW-owned refresh and a normal reading POST keep their real
            # methods and bodies. Neither is converted into an Action publish.
            await page.get_by_role('button', name='Refresh membership').click()
            await asyncio.wait_for(post_seen['/session/refresh'].wait(), 5)
            reading = await page.evaluate("""async()=>{try {
              const response=await fetch('/read',{method:'POST',body:new URLSearchParams({query:'welcome',nonce:'real-read-post'})});
              return {status:response.status,text:await response.text()};
            } catch(error) {return {error:error.name};}}""")
            await asyncio.wait_for(post_seen['/read'].wait(), 5)
            await asyncio.sleep(.1)
            proofs=[row['read_proof'] for row in decisions if row.get('read_proof')]
            assert {proof['role'] for proof in proofs}=={'search','session_maintenance'}
            maintenance=next(proof for proof in proofs if proof['role']=='session_maintenance')
            assert maintenance['task_id']==str(task['id']) and maintenance['page_target_id']==controller.lease['target_id']
            assert len(maintenance['worker_script_hash'])==64 and len(maintenance['page_script_hash'])==64
            post_seen['/session/refresh'].clear()
            await page.get_by_role('button',name='Refresh membership').click()
            await asyncio.wait_for(post_seen['/session/refresh'].wait(),5)
            await asyncio.sleep(.05)
            assert source_count('POST','/session/refresh')==1
            denied=await page.evaluate('''async()=>{try{await fetch('/read',{method:'POST',body:new URLSearchParams({query:'x',nonce:'fresh-read-nonce',body:'an-ungranted-write'})});return false}catch{return true}}''')
            assert denied and source_count('POST','/read')==1
            snapshot.update(upstream_fetch_events=list(upstream_events.values()),
                refresh_source_post_count=source_count('POST', '/session/refresh'),
                reading_source_post_count=source_count('POST', '/read'), read_status=reading.get('status'),
                read_error=reading.get('error'), refreshes=site.oracle()['counts'].get('refreshes', 0),
                reads=site.oracle()['counts'].get('reads', 0), nonpublication_post_gate_decisions=[
                    row for row in decisions if urlsplit(row['url']).path in ('/session/refresh', '/read')])
        finally:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8088',
                    cookies={'lead_session': token('owner')},
                    headers={'Origin': 'http://127.0.0.1:3100', 'X-Lead-Request': '1'}) as owner:
                with tx(aid) as connection:
                    current = one(connection, 'SELECT state,challenge_version FROM lead.takeover_sessions WHERE id=%s', (pending['takeover_id'],))
                if current['state']=='active':
                    ended = await owner.post(f"/api/agents/{aid}/connections/{pending['takeover_id']}/control",
                        json={'challenge_version': current['challenge_version'], 'action': 'cancel'})
                    assert ended.status_code == 200, ended.text
            await controller.reconcile()

        # The ended real connection's recorded authentication endpoint must not
        # be borrowed by a surviving worker in the same browser context.
        post_seen['/session'].clear()
        prior_session_posts = source_count('POST', '/session')
        prior_decisions = len(decisions)
        workers = [worker for worker in actor.context.service_workers if urlsplit(worker.url).hostname == site.hosts['main']]
        ended_attempt_error = None
        if workers:
            try:
                await workers[0].evaluate("""async()=>{try{const response=await fetch('/session',{method:'POST',credentials:'include',
                body:new URLSearchParams({username:'ended-worker-attempt',password:'not-a-credential',nonce:'ended-auth'})});
                return response.status;}catch(error){return error.name;}}""")
                await asyncio.wait_for(post_seen['/session'].wait(), 5)
            except Exception as error:
                ended_attempt_error = type(error).__name__
        snapshot.update(ended_auth_worker_attempt_observed=bool(decisions[prior_decisions:]),
            ended_auth_attempt_error=ended_attempt_error,
            ended_auth_source_post_delta=source_count('POST', '/session')-prior_session_posts,
            ended_auth_gate_decisions=decisions[prior_decisions:], new_model_requests=len(context['model_calls'])-before_model,
            mapped_virtual_origins=origins, actual_local_listener='127.0.0.1:' + str(listen_port),
            transport_aliases=addresses, finished_private_self_binding=False,
            phase='real ConnectionRef/current HttpOnly session, not final NativeResearch',
            next_scopes_not_fabricated=['nested OOPIF origin permissions', 'first SSO popup role', 'completed boundary-site private-self binding'])
        print('NATIVE_BOUNDARY_DIAGNOSTICS ' + json.dumps(snapshot, sort_keys=True))
        assert snapshot['synthetic_text_ok'] and snapshot['synthetic_source_count'] == 0, snapshot
        assert snapshot['cache_text_ok'] and snapshot['cache_install_source_count'] == 1 and snapshot['cache_hit_source_delta'] == 0, snapshot
        assert snapshot['new_model_requests'] == 0
        assert snapshot['refresh_source_post_count'] == 1 and snapshot['refreshes'] == 1, snapshot
        assert snapshot['reading_source_post_count'] == 1 and snapshot['reads'] == 1 and snapshot['read_status'] == 200, snapshot
        assert snapshot['ended_auth_worker_attempt_observed'] and snapshot['ended_auth_source_post_delta'] == 0, snapshot
        return snapshot

    try:
        await verify_login(monkeypatch, mode='boundary_transport', owner_hook=check)
    finally:
        site.close()
