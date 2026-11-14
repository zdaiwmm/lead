"""Owner consents to one actual public frame; profile identity stays unavailable."""

import asyncio
import hashlib
import io
import json
import os
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')


@pytest.mark.asyncio
@pytest.mark.parametrize('conflict', [False, True, 'response_cookie'])
async def test_original_owner_confirms_one_isolated_public_frame(monkeypatch, conflict):
    import native_connection_product as bootstrap
    from lead_app import account_profiles, api, browser, network_namespace
    from lead_app.account_connection import execute_account
    from lead_app.browser_runtime import NativeNetworkGate, PersistentRuntime
    from lead_app.config import ROOT, STATE, env
    from lead_app.db import one, tx
    from lead_app.forward_proxy import BrowserForwardProxy
    from lead_app.growth.contracts import TaskRef, utcnow
    from lead_app.growth.runtime import advance, finish, recover_due
    from lead_app.growth.tools import AccountInput
    from lead_app.native_task import NativeTask
    from lead_app.security import internal_token, token
    from native_browser_fixture import NativeBoard
    from native_connection_product import verify_login

    runtime = Path(env().get('LEAD_BROWSER_RUNTIME_DIR') or ROOT/'services/browser-runtime')
    assert json.loads((runtime/'manifest.json').read_text())['native_smoke'] is True
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    origin = 'http://93.184.215.20'
    requests = []
    model_visibility=[]
    def model_server(*args,**kwargs):
        server=ThreadingHTTPServer(*args,**kwargs)
        handler=server.RequestHandlerClass
        original=handler.do_POST
        def post(instance):
            raw=instance.rfile.read(int(instance.headers.get('Content-Length','0')))
            model_visibility.append({'public_frame':b'Public anonymous frame' in raw})
            instance.rfile=io.BytesIO(raw)
            return original(instance)
        handler.do_POST=post
        return server
    monkeypatch.setattr(bootstrap,'ThreadingHTTPServer',model_server)
    decisions=[]
    original_authorize=PersistentRuntime.authorize_request
    async def trace(runtime,scope,target,request):
        try:
            result=await original_authorize(runtime,scope,target,request)
            if request['url'].startswith(origin):
                decisions.append({'allowed':bool(result),'target_type':target['type'],
                    'network':request.get('_native_network'), 'roles':len((scope or {}).get('frame_roles',[])),
                    'public_frame':bool((result or {}).get('public_frame')),'refuse_callback':callable((result or {}).get('refuse_response'))})
            return result
        except Exception as error:
            if request['url'].startswith(origin):
                decisions.append({'error':str(error) if isinstance(error,ValueError) else type(error).__name__})
            raise
    monkeypatch.setattr(PersistentRuntime,'authorize_request',trace)
    original_resolve=NativeNetworkGate.resolve_owner
    async def trace_owner(gate,event):
        try:
            result=await original_resolve(gate,event)
            if event['request']['url'].startswith(origin):
                decisions.append({'owner':True,'status':event.get('responseStatusCode'),'frame':event.get('frameId'),
                    'network_id':event.get('networkId'),'session':result[0],'type':result[1]['type'],
                    'response_cookie_header':any(row['name'].lower()=='set-cookie' for row in event.get('responseHeaders',[]))})
            return result
        except Exception as error:
            if event['request']['url'].startswith(origin):
                decisions.append({'owner_error':type(error).__name__,'status':event.get('responseStatusCode'),
                    'frame':event.get('frameId'),'network_id':event.get('networkId'),
                    'candidates':[{'frame':row.get('frameId'),'session':sid} for (rid,sid),row in gate.network.items() if rid==event.get('networkId')]})
            raise
    monkeypatch.setattr(NativeNetworkGate,'resolve_owner',trace_owner)
    document = '<main><p id=public>Public anonymous frame</p><script>window.executed=true;fetch("/write",{method:"POST"})</script></main>'

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if conflict=='response_cookie':
                print('SERVER_SET_COOKIE_ATTEMPTED',True,flush=True)
            requests.append({'method': 'GET', 'cookie': bool(self.headers.get('Cookie')),
                'referer':bool(self.headers.get('Referer')), 'path': self.path})
            raw = document.encode()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8')
            if conflict=='response_cookie':
                self.send_header('Set-Cookie','dependency_session=synthetic; HttpOnly; SameSite=None; Secure; Path=/')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            requests.append({'method': 'POST', 'path': self.path})
            self.send_response(409)
            self.send_header('Content-Length', '0')
            self.end_headers()

    external = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=external.serve_forever, daemon=True).start()
    options = network_namespace.chromium_options
    def trustworthy(*args,**kwargs):
        result=options(*args,**kwargs)
        result['args'].append('--unsafely-treat-insecure-origin-as-secure=http://93.184.215.14,http://93.184.215.20')
        return result
    monkeypatch.setattr(network_namespace, 'chromium_options',trustworthy)
    original_handler = NativeBoard.handler

    def handler_variant(board):
        handler = original_handler(board)
        original_page = handler.page

        def page(self, title, body, status=200):
            if self.path == '/home' and 'textarea' in body:
                body += '<iframe title="Public reference" src="'+origin+'/public"></iframe>'
            return original_page(self, title, body, status)

        handler.page = page
        return handler

    monkeypatch.setattr(NativeBoard, 'handler', handler_variant)
    original_connect = BrowserForwardProxy.connect

    # verify_login installs its A-only transport after this test starts. Patch
    # its actual setter narrowly, retaining that exact A route unchanged.
    original_setattr = monkeypatch.setattr

    def setattr_with_transport(target, name, value, *args, **kwargs):
        if target is BrowserForwardProxy and name == 'connect':
            route = value

            async def connect(proxy, ip, port):
                if ip == '93.184.215.20' and port == 80:
                    return await asyncio.open_connection('127.0.0.1', external.server_port)
                return await route(proxy, ip, port)

            value = connect
        return original_setattr(target, name, value, *args, **kwargs)

    monkeypatch.setattr = setattr_with_transport

    async def owner(context):
        assert context['result'].outcome == 'waiting'
        aid, pending = context['aid'], context['pending']
        connection = str(pending['takeover_id'])
        actor = await browser.gateway.get(aid, context['task']['input']['spec']['payload']['profile_id'], allow_challenge=True)
        controller = actor.connection_pages[connection]
        assert not requests
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url='http://127.0.0.1:8088',
            cookies={'lead_session': token('owner')}, headers={'Origin': 'http://127.0.0.1:3100', 'X-Lead-Request': '1'}) as client:
            status = await client.get(f'/api/agents/{aid}/connections/{connection}')
            assert status.json()['can_review_read_dependencies'] is True
            listing = await client.get(f'/api/agents/{aid}/connections/{connection}/read-dependencies')
            assert listing.status_code == 200, listing.text
            review = listing.json()
            assert len(review['candidates']) == 1 and review['candidates'][0]['state'] == 'pending', review
            candidate = review['candidates'][0]
            if conflict is True:
                await actor.context.add_cookies([{'url': origin, 'name': 'existing_identity', 'value': 'synthetic-existing'}])
            decision = await client.post(f'/api/agents/{aid}/connections/{connection}/read-dependencies', json={
                'candidate_id': candidate['candidate_id'], 'challenge_version': review['challenge_version'], 'decision': 'approve'})
            assert decision.status_code == 200, decision.text
            assert decision.json()['challenge_version'] == review['challenge_version']
            if conflict is True:
                assert decision.json()['candidates'][0]['state'] == 'refused'
                assert decision.json()['candidates'][0]['reason'] == 'profile_storage_present'
                assert not requests
                refused=await client.post(f'/api/agents/{aid}/connections/{connection}/control',
                    json={'action':'resume','challenge_version':review['challenge_version']})
                assert refused.status_code==409
                cancelled=await client.post(f'/api/agents/{aid}/connections/{connection}/control',
                    json={'action':'cancel','challenge_version':review['challenge_version']})
                assert cancelled.status_code==200
                return {'refused_existing_identity': True}
            if conflict=='response_cookie':
                until=asyncio.get_running_loop().time()+4
                while True:
                    with tx(aid) as c:
                        source=one(c,'SELECT connection_data FROM lead.takeover_sessions WHERE id=%s',(connection,))['connection_data']['read_dependencies'][0]
                    if source['state']=='refused' or asyncio.get_running_loop().time()>=until:
                        break
                    await asyncio.sleep(.03)
                assert source['state']=='refused' and source['reason']=='isolation_unsupported'
                assert requests==[{'method':'GET','cookie':False,'referer':False,'path':'/public'}]
                assert await actor.context.cookies(origin)==[]
                with pytest.raises(ValueError):
                    await actor.native_runtime.scope_view(controller.lease['target_id'])
                cancelled=await client.post(f'/api/agents/{aid}/connections/{connection}/control',
                    json={'action':'cancel','challenge_version':review['challenge_version']})
                assert cancelled.status_code==200
                return {'response_cookie_refused':True,'profile_cookie_unchanged':True}
            assert decision.json()['candidates'][0]['state'] == 'approved'
            child = next(frame for frame in controller.page.frames if frame.parent_frame)
            await child.locator('#public').wait_for(timeout=8000)
            view = await actor.native_runtime.scope_view(controller.lease['target_id'])
            assert origin in view['origins']
            actual = await child.evaluate('''()=>({credentialless:window.credentialless,origin:origin,
                executed:window.executed===true,storage:(()=>{try{localStorage.length;return false}catch(e){return e.name==='SecurityError'}})()})''')
            assert actual == {'credentialless': True, 'origin': 'null', 'executed': False, 'storage': True}
            assert requests == [{'method': 'GET', 'cookie': False, 'referer': False, 'path': '/public'}]
            # The original UI cannot interact with the read-only foreign frame.
            box = await controller.page.locator('iframe').bounding_box()
            async with httpx.AsyncClient(base_url=context['gateway_endpoint'], headers={'Authorization': 'Bearer '+internal_token()}) as gateway:
                denied = await gateway.post(f'/agents/{aid}/input', json={'takeover': connection, 'challenge_version': review['challenge_version'],
                    'kind': 'click', 'x': box['x']+10, 'y': box['y']+10})
                assert denied.status_code == 409
            await asyncio.sleep(2.1)
            resumed = await client.post(f'/api/agents/{aid}/connections/{connection}/control',
                json={'action': 'resume', 'challenge_version': review['challenge_version']})
            assert resumed.status_code == 200, resumed.text
        context['workflow'].update(kind='resume', step=0)
        ref = TaskRef(agent_id=aid, task_id=context['task']['id'], task_generation=context['task']['input']['task_generation'])
        recover_due(aid, now=utcnow()+timedelta(seconds=15))
        admitted = advance(ref, now=utcnow()+timedelta(seconds=15))
        assert admitted['state'] == 'ready', admitted
        with tx(aid) as c:
            task = one(c, 'SELECT * FROM lead.tasks WHERE id=%s', (context['task']['id'],))
        result = await execute_account(aid, task, AccountInput.model_validate(task['input']['spec']['payload']))
        finish(admitted['claim'], result)
        assert result.outcome == 'completed', result
        await controller.reconcile()
        with tx(aid) as c:
            completed = one(c, 'SELECT * FROM lead.takeover_sessions WHERE id=%s', (connection,))
            account=one(c,'SELECT binding_version FROM lead.social_accounts WHERE id=%s',(pending['account_id'],))
            assert completed['connection_data']['read_dependencies'][0]['bound']['binding_version'] == account['binding_version']
        # A fresh original discovery task derives a new task-bound role from the
        # old owner source; it cannot inherit the previous task's lease/claim.
        from lead_app.growth.contracts import TaskSpec
        from lead_app.growth.store import submit_work
        with tx(aid) as c:
            work = submit_work(c, aid, TaskSpec(kind='account', purpose='discover', account_id=pending['account_id'],
                payload={'target_urls': [context['board'].origin+'/home']}, review_at=utcnow()+timedelta(hours=1)))
        claim = advance(work)['claim']
        context['workflow'].update(kind='discovery',step=0)
        from lead_app.account_connection import discover
        with tx(aid) as c:
            discovery_task=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(work['task_id'],))
        learned=await discover(aid,discovery_task)
        assert learned.outcome=='completed' and learned.output['targets']
        assert any(row['public_frame'] for row in model_visibility)
        task_page = NativeTask(actor, work['task_id'])
        try:
            await task_page.start()
            child = next(frame for frame in task_page.page.frames if frame.parent_frame)
            await child.locator('#public').wait_for(timeout=8000)
            assert origin in (await task_page.descriptor())['origins']
            before = len(requests)
            with tx(aid) as c:
                old_intent=one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(pending['account_id'],))
                old_grants=c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()
            await task_page.page.locator('iframe').evaluate("node=>node.removeAttribute('sandbox')")
            with pytest.raises(ValueError, match='frame_isolation_removed'):
                await task_page.descriptor()
            assert len(requests) == before
            await task_page.page.locator('iframe').evaluate("node=>node.setAttribute('sandbox','')")
            with pytest.raises(ValueError,match='frame_resource_not_approved'):
                await task_page.descriptor()
            with tx(aid) as c:
                source=one(c,'SELECT connection_data FROM lead.takeover_sessions WHERE id=%s',(connection,))['connection_data']['read_dependencies'][0]
                assert source['state']=='refused' and source['reason']=='source_changed'
                assert one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(pending['account_id'],))==old_intent
                assert c.execute('SELECT * FROM lead.authorization_grants ORDER BY id').fetchall()==old_grants
        finally:
            await task_page.close()
            from lead_app.growth.contracts import WorkResult
            finish(claim, WorkResult(outcome='completed',output=learned.output))
        return {'actual_opaque_partition': True, 'source_bound': True, 'fresh_task_role': True,
            'sdk_foreign_text_seen':True,'attribute_removal_revoked': True}

    try:
        result = await verify_login(monkeypatch, owner_hook=owner)
        assert result['owner_check']
        (STATE/'frame-role-product-summary.json').write_text(__import__('json').dumps({'checks': result['owner_check'],
            'variant_sha256': hashlib.sha256(document.encode()).hexdigest(), 'requests': requests}))
    finally:
        print('FRAME_ROLE_DECISIONS',decisions,'SOURCE_REQUESTS',requests)
        monkeypatch.setattr = original_setattr
        BrowserForwardProxy.connect = original_connect
        external.shutdown()
        external.server_close()
