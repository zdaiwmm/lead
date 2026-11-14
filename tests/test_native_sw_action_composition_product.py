"""One real SSO-bound account discovers and executes worker-owned Actions.

This explicit test-site variant adds member rules, a target link, value spans
and transparent same-request SW forwarding. It omits the unrelated foreign
iframe: C30 frame authorization is not exercised. Frozen BoundarySite is intact.
Only synthetic model HTTP responses and a post-append response loss are fake.
"""
import asyncio
import contextvars
import hashlib
import json
import os
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get('LEAD_NATIVE_PRODUCT_TESTS') != '1',
    reason='Explicit isolated Linux native product run required')

RULES = ('You are an ordinary member. Members may publish text posts and reply to existing posts. '
    'Automated browser tools are permitted for these member actions. You have no administrator privileges.')
WORKER = '''
self.addEventListener('fetch',event=>{
 const request=event.request,url=new URL(request.url);
 if(url.origin===self.location.origin&&request.method==='POST'&&['/posts','/replies'].includes(url.pathname))
  event.respondWith((async()=>{
   const body=await request.clone().text();
   const pending={url:request.url,body,type:request.headers.get('content-type')};
   await (await caches.open('native-action-pending-test')).put('/pending-last',new Response(JSON.stringify(pending)));
   return fetch(request);
  })());
});
'''


def mechanical_model(site, calls):
    state = {'kind': 'discovery', 'step': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(raw)
            parts = []
            for message in request.get('messages', []):
                value = message.get('content')
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, list):
                    parts.extend(item.get('text', '') for item in value if isinstance(item, dict))
            lines = '\n'.join(parts).splitlines()
            nodes = [(int(match[1]), '\n'.join(lines[number:number+3])) for number,line in enumerate(lines)
                if (match := re.search(r'\[(\d+)\]', line)) and any(tag in line.lower() for tag in ('<textarea','<button','<a '))]
            step, kind = state['step'], state['kind']
            state['step'] += 1
            action = {'done': {'text': 'Synthetic mechanical transport', 'success': True}}
            if kind == 'discovery':
                if step == 0:
                    action = {'guarded_navigate': {'url': site.origin()+'/home'}}
                elif step == 1:
                    action = {'propose_method': {'target_url': site.origin()+'/home', 'operation': 'publish',
                        'selectors': {'self_subject':'main > p:first-of-type', 'editor_target':'#editor-target',
                            'editor_body':'form[action="/posts"] textarea', 'submit':'form[action="/posts"] button'},
                        'self_subject_prefix':'Signed in as ', 'self_subject_suffix':'',
                        'permission_selector':'section p', 'permission_quote':RULES}}
            elif kind == 'action' and step == 0:
                action = {'guarded_input': {'index':next(index for index,text in nodes if '<textarea' in text.lower()),
                    'text':state['body'], 'clear':True}}
            elif kind == 'action' and step == 1:
                action = {'guarded_click': {'index':[index for index,text in nodes if '<button' in text.lower()][-1]}}
            elif kind == 'reconcile' and step == 0:
                action = {'guarded_click': {'index':next(index for index,text in nodes if '<a ' in text.lower() and state['body'] in text)}}
            elif kind == 'action' and step == 2 or kind == 'reconcile' and step == 1:
                action = {'report_receipt': {'selectors': {'author':'#author','body':'#post-body','url':'a[rel~=canonical]'}}}
            calls.append({'kind':kind,'step':step,'action':next(iter(action)),
                'credential_literal_seen':any(value.encode() in raw for value in (site.username,site.password))})
            tool = request.get('tools',[{'function':{'name':'AgentOutput'}}])[0]['function']['name']
            response = json.dumps({'id':'synthetic-sw','object':'chat.completion','model':'fixture-transport',
                'choices':[{'index':0,'message':{'role':'assistant','content':None,'tool_calls':[{'id':'synthetic',
                    'type':'function','function':{'name':tool,'arguments':json.dumps({'action':[action]})}}]},'finish_reason':'tool_calls'}],
                'usage':{'prompt_tokens':0,'completion_tokens':0,'total_tokens':0}}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server,state,{'provider':'deepseek','key':'synthetic-transport-only','model':'fixture-transport',
        'base_url':'http://127.0.0.1:'+str(server.server_port)}


@pytest.mark.asyncio
async def test_sso_bound_original_actions_use_worker_transport_and_replay_no_new_post(monkeypatch):
    import native_browser_boundary_fixture as frozen
    import test_native_auth_flow_product as sso
    import test_native_boundary_product as boundary
    from lead_app import native_runner
    from lead_app.account_connection import discover
    from lead_app.browser_runtime import NativeNetworkGate, PersistentRuntime
    from lead_app.config import STATE
    from lead_app.db import all_rows, j, one, tx
    from lead_app.growth.contracts import TaskSpec, utcnow
    from lead_app.growth.runtime import advance, finish
    from lead_app.growth.store import digest, submit_work
    from lead_app.growth.tools import ChannelInput, execute_channel
    from lead_app.native_task import NativeTask
    from test_native_manager_death_product import tls_contexts

    class SiteVariant(frozen.BoundarySite):
        revision = 1
        drop_next_response = False

        def _worker(self):
            return super()._worker()+WORKER+'\n// Controlled action transport revision '+str(self.revision)+'\n'

        def _handler(self):
            handler = super()._handler()
            original_send = handler.send
            def send(current,status,body='',**kwargs):
                if current.role == 'main':
                    body = body.replace('<p>Members may publish and reply. Automated member actions are permitted.</p>',
                        '<section><h2>Member rules</h2><p>'+RULES+'</p></section><a id="editor-target" href="/home">Posting to: Member board</a>')
                    body = re.sub(r'<iframe title="Visitor document"[^>]+></iframe>','',body)
                    body = re.sub(r'<article><p>Author: ([^<]+)</p><p>([^<]*)</p>',
                        r'<article><p>Author: <span id="author">\1</span></p><div id="post-body">\2</div>',body)
                    if current.command == 'POST' and current.path_only == '/posts' and status == 303 and self.drop_next_response:
                        self.drop_next_response = False
                        current.close_connection = True
                        return
                return original_send(current,status,body,**kwargs)
            handler.send = send
            return handler

    monkeypatch.setattr(frozen,'BoundarySite',SiteVariant)
    original_boundary = boundary.test_connection_session_keeps_sw_cache_and_nonpublication_posts
    original_authorize = PersistentRuntime.authorize_request
    original_owner = NativeNetworkGate.resolve_owner
    events, calls, workers, report = [], [], [], {}

    async def observed_authorize(runtime,scope,target,request):
        decision = await original_authorize(runtime,scope,target,request)
        if urlsplit(request['url']).path == '/posts':
            events.append({'method':request['method'],'target_type':target['type'],'target_id':target['targetId'],
                'kind':(decision or {}).get('kind'),'action_id':(decision or {}).get('action_id')})
        return decision

    monkeypatch.setattr(PersistentRuntime,'authorize_request',observed_authorize)
    async def observed_owner(gate,event):
        request = event['request']
        if urlsplit(request['url']).path != '/posts':
            return await original_owner(gate,event)
        row = {'kind':'owner','method':request['method'],'fetch_frame_id':event.get('frameId'),
            'network_id':event.get('networkId'),'resource_type':event.get('resourceType'),
            'has_post_data':request.get('hasPostData',False),'post_data_present':'postData' in request}
        events.append(row)
        try:
            result = await original_owner(gate,event)
            row.update(target_type=result[1]['type'],target_id=result[1]['targetId'])
            return result
        except Exception as error:
            row.update(error=type(error).__name__,reason=str(error)[:120])
            raise
        finally:
            row['candidates'] = [{'session':sid,'frame_id':value.get('frameId'),'method':value['request']['method'],
                'path':urlsplit(value['request']['url']).path,'post_data_present':'postData' in value['request'],
                'target_type':gate.targets.get(sid,{}).get('type'),'context_id':gate.targets.get(sid,{}).get('browserContextId')}
                for (identity,sid),value in gate.network.items() if identity==event.get('networkId')]
    monkeypatch.setattr(NativeNetworkGate,'resolve_owner',observed_owner)

    async def continuation(data):
        actor,site = data['actor'],data['site']
        aid,account = actor.aid,data['pending']['account_id']
        await data['controller'].reconcile()
        server,model,connection = mechanical_model(site,calls)
        active = contextvars.ContextVar('synthetic_sw_action_task')
        original_run = native_runner.run_task
        async def run(controller,*args,**kwargs):
            token = active.set(str(controller.task_id))
            try:
                return await original_run(controller,*args,**kwargs)
            finally:
                workers.append({'task_id':str(controller.task_id),'target_id':controller.lease.get('target_id'),
                    'epoch':actor.epoch,'profile_id':str(actor.profile['id'])})
                active.reset(token)
        monkeypatch.setattr(native_runner,'run_task',run)
        monkeypatch.setattr(native_runner,'resolve_model',lambda _:connection if active.get() else None)
        hold = None
        try:
            with tx(aid) as c:
                ref = submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=account,
                    payload={'target_urls':[site.origin()+'/home']},review_at=utcnow()+timedelta(hours=1)))
            claim = advance(ref)['claim']
            with tx(aid) as c:
                task = one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
            learned = await discover(aid,task)
            finish(claim,learned)
            assert learned.outcome == 'completed' and len(learned.output['targets']) == 1, learned
            target = learned.output['targets'][0]['target_id']
            with tx(aid) as c:
                grants = all_rows(c,'SELECT * FROM lead.authorization_grants ORDER BY id')
                intent = one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(account,))

            def work(body):
                content_body = {'body':body,'title':'','format':'text','media':[]}
                with tx(aid) as c:
                    content = one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
                        VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(content_body),digest(content_body)))
                    payload = ChannelInput(content_id=content['id'],content_hash=content['content_hash'])
                    ref = submit_work(c,aid,TaskSpec(kind='channel',purpose='publish',account_id=account,target_id=target,
                        payload=payload.model_dump(mode='json'),review_at=utcnow()+timedelta(hours=1)))
                return ref,payload,body

            async def execute(item,kind='action'):
                ref,payload,body = item
                admitted = advance(ref,now=utcnow()+timedelta(minutes=6))
                assert admitted['state'] == 'ready',admitted
                with tx(aid) as c:
                    task = one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
                model.update(kind=kind,step=0,body=body)
                try:
                    result = await execute_channel(aid,task,payload,admitted['claim'])
                except Exception as error:
                    response = getattr(error,'response',None)
                    report['publish_error'] = {'type':type(error).__name__,
                        'dto':response.json() if response is not None else None}
                    raise
                finish(admitted['claim'],result)
                with tx(aid) as c:
                    action = one(c,'SELECT * FROM lead.actions WHERE task_id=%s',(ref['task_id'],))
                    attempt = one(c,'SELECT * FROM lead.action_attempts WHERE action_id=%s',(action['id'],))
                report.setdefault('executions',[]).append({'task_id':str(ref['task_id']),'kind':kind,
                    'outcome':result.outcome,'failure_code':result.failure_code,'action_state':action['state'],
                    'action_id':str(action['id']),'attempt':attempt['attempt'],
                    'claimed':attempt['observation'].get('browser_request',{}).get('claimed')})
                return result,action,attempt

            normal = await execute(work('Controlled normal worker-owned publication'))
            assert normal[0].outcome == 'completed' and normal[1]['state'] == 'confirmed'
            unknown_item = work('Controlled worker-owned publication with no acknowledgement')
            site.drop_next_response = True
            unknown = await execute(unknown_item)
            assert unknown[0].outcome == 'unknown' and unknown[1]['state'] == 'unknown'
            assert not site.drop_next_response and len(site.oracle()['posts']) == 2
            for item in (normal,unknown):
                assert item[2]['attempt'] == 1 and item[2]['observation']['browser_request']['claimed']
                assert any(event['kind']=='action' and event['target_type']=='service_worker'
                    and event['action_id']==str(item[1]['id']) for event in events),events

            # A real new account Task holds a current business page while its
            # worker/controller updates; this is not a fabricated Action scope.
            with tx(aid) as c:
                hold_ref = submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=account,
                    payload={'target_urls':[site.origin()+'/home']},dedupe_key='controller-replay-observation',
                    review_at=utcnow()+timedelta(hours=1)))
            assert advance(hold_ref)['state'] == 'ready'
            hold = NativeTask(actor,hold_ref['task_id'])
            await hold.start()
            page = hold.page
            await page.get_by_text('Background service ready',exact=True).wait_for()
            gate = actor.native_runtime.gate
            page_session = next(sid for sid,value in gate.targets.items() if value['targetId']==hold.lease['target_id'])
            def current():
                return next((value for value in gate.worker_versions_by_page.get(page_session,{}).values()
                    if value['status']=='activated' and hold.lease['target_id'] in value.get('controlledClients',[])),None)
            async def until(predicate):
                async with asyncio.timeout(8):
                    while not predicate():
                        await asyncio.sleep(.02)
            await until(current)
            old_target = current()['targetId']
            site.revision = 2
            await page.evaluate("navigator.serviceWorker.getRegistration('/home').then(reg=>reg.update())")
            await until(lambda:current() and current()['targetId']!=old_target)
            worker = next(worker for worker in actor.context.service_workers if worker.url==site.origin()+'/sw.js')
            before = data['source_count']('POST','/posts')
            # Fault injection only: replay the site's cached original request
            # twice after controller replacement, never a substitute for SDK publish.
            replay = await worker.evaluate('''async()=>{
                const pending=await (await (await caches.open('native-action-pending-test')).match('/pending-last')).json();
                return Promise.all([0,1].map(()=>fetch(pending.url,{method:'POST',credentials:'include',
                  headers:{'Content-Type':pending.type},body:pending.body}).then(r=>r.status).catch(e=>e.name)));
            }''')
            assert data['source_count']('POST','/posts') == before == 2,replay
            restored = await execute(unknown_item,'reconcile')
            assert restored[0].outcome == 'completed' and restored[1]['state'] == 'confirmed'
            assert restored[1]['id'] == unknown[1]['id'] and restored[1]['intent'] == unknown[1]['intent']
            assert restored[2]['id'] == unknown[2]['id'] and restored[2]['attempt'] == 1
            assert len(site.oracle()['posts']) == data['source_count']('POST','/posts') == 2
            with tx(aid) as c:
                assert all_rows(c,'SELECT * FROM lead.authorization_grants ORDER BY id') == grants
                assert one(c,'SELECT * FROM lead.account_operating_intents WHERE account_id=%s',(account,)) == intent
            report.update(normal_action=str(normal[1]['id']),unknown_action=str(unknown[1]['id']),
                source_posts=2,replay=replay,old_worker_target=old_target,new_worker_target=current()['targetId'],
                same_unknown_attempt=True,original_grants_unchanged=True)

            # Final independent fault stage: lose only Gate's control socket,
            # keeping the original manager, Chrome and current SW alive.
            with TemporaryDirectory(prefix='control-only-tls-', dir=STATE) as temporary:
                folder = Path(temporary)
                tls_server,tls_client = tls_contexts(folder)
                received,closed = [],asyncio.Event()
                async def stream(reader,writer):
                    try:
                        received.append((await reader.readline()).decode().strip())
                        writer.write(b'BEFORE\n')
                        await writer.drain()
                        await reader.read()
                    finally:
                        writer.close()
                        closed.set()
                listener = await asyncio.start_server(stream,'127.0.0.1',0,ssl=tls_server)
                proxy = actor.forward_proxy
                previous_resolve = proxy.resolve
                async def tls_resolve(host):
                    # This sole test destination uses the exact local TLS mapping
                    # below; every business hostname keeps the strict fixture DNS.
                    if host=='93.184.215.230':
                        return host
                    return await previous_resolve(host)
                monkeypatch.setattr(proxy,'resolve',tls_resolve)
                previous_connect = proxy.connect
                async def tls_transport(ip,port):
                    if ip=='93.184.215.230' and port==443:
                        return await asyncio.open_connection('127.0.0.1',listener.sockets[0].getsockname()[1])
                    return await previous_connect(ip,port)
                monkeypatch.setattr(proxy,'connect',tls_transport)
                writer = partial = None
                try:
                    reader,writer = await asyncio.open_connection('127.0.0.1',proxy.port)
                    writer.write(('CONNECT 93.184.215.230:443 HTTP/1.1\r\nHost: 93.184.215.230:443\r\n'
                        +'Proxy-Authorization: '+proxy.auth+'\r\n\r\n').encode())
                    await writer.drain()
                    assert b'200' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),5)
                    await writer.start_tls(tls_client,server_hostname='stream.fixture.test')
                    writer.write(b'BEFORE\n')
                    await writer.drain()
                    assert await asyncio.wait_for(reader.readline(),5) == b'BEFORE\n'
                    incomplete,partial = await asyncio.open_connection('127.0.0.1',proxy.port)
                    partial.write(b'CONNECT ')
                    await partial.drain()
                    await until(lambda:len(proxy.tasks)>=2)
                    before_models = len(calls)
                    runtime = actor.native_runtime
                    await gate.ws.close()
                    await until(lambda:runtime.closed and actor.disconnected and not proxy.tasks)
                    assert await asyncio.wait_for(reader.read(1),5) == b''
                    assert await asyncio.wait_for(incomplete.read(1),5) == b''
                    await asyncio.wait_for(closed.wait(),5)
                    assert not page.is_closed() and await page.evaluate('1') == 1
                    assert await worker.evaluate('self.registration.active.scriptURL') == site.origin()+'/sw.js'
                    await asyncio.wait_for(worker.evaluate('''async()=>{const p=await (await (await caches.open('native-action-pending-test')).match('/pending-last')).json();
                        return fetch(p.url,{method:'POST',headers:{'Content-Type':p.type},body:p.body}).then(r=>r.status).catch(e=>e.name)}'''),5)
                    assert data['source_count']('POST','/posts') == 2 and len(calls) == before_models
                    assert runtime.forward_token() is None and received == ['BEFORE']
                    with pytest.raises(OSError):
                        await asyncio.open_connection('127.0.0.1',proxy.port)
                    with tx(aid) as c:
                        assert all(row['state']=='confirmed' for row in all_rows(c,'SELECT state FROM lead.actions'))
                        assert all_rows(c,'SELECT attempt FROM lead.action_attempts') == [{'attempt':1},{'attempt':1}]
                    report['control_only'] = {'manager_pid':os.getpid(),'chrome_cdp_alive':True,'same_worker_alive':True,
                        'tls_eof':True,'partial_header_eof':True,'old_worker_source_delta':0,'new_model_requests':0}
                finally:
                    for output in (writer,partial):
                        if output:
                            output.close()
                    listener.close()
                    await listener.wait_closed()
        finally:
            if hold:
                await hold.close()
            with tx(aid) as c:
                report['actions'] = all_rows(c,'SELECT id,task_id,state FROM lead.actions ORDER BY created_at')
                report['attempts'] = all_rows(c,'SELECT action_id,attempt,outcome FROM lead.action_attempts ORDER BY started_at')
            report.update(events=events,model_calls=calls,workers=workers,
                test_variant_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                frozen_fixture_sha256=hashlib.sha256(Path(frozen.__file__).read_bytes()).hexdigest(),
                foreign_frame_composition=False,source_posts=len(site.oracle()['posts']),
                source_post_requests=data['source_count']('POST','/posts'))
            (STATE/'native-sw-action-composition.json').write_text(json.dumps(report,default=str,indent=2))
            server.shutdown()
            server.server_close()

    async def compose(*args,boundary_hook=None,**kwargs):
        async def complete(data):
            result = await boundary_hook(data)
            await continuation(data)
            return result
        return await original_boundary(*args,boundary_hook=complete,**kwargs)
    monkeypatch.setattr(boundary,'test_connection_session_keeps_sw_cache_and_nonpublication_posts',compose)
    await sso.test_observed_popup_owner_identity_and_callback_complete_original_connection(monkeypatch,owner_entry=True)
    assert report['source_posts'] == 2 and not any(call['credential_literal_seen'] for call in calls)
