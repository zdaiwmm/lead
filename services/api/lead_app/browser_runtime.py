"""Instance-owned Chromium control transport; no agent loop or business ledger.

The namespace exposes CDP only on a private Unix socket. The existing instance
manager owns this authenticated, loopback-only bridge for native SDK workers.
It must revoke access and close it before releasing the browser or proxy.
"""
import asyncio
import contextlib
import hmac
import secrets
from http import HTTPStatus
from urllib.parse import urlsplit

from websockets.asyncio.client import unix_connect
from websockets.asyncio.server import serve


class ControlBridge:
    """An unguessable header is required on every new control WebSocket.

    This is a transport for trusted fixed-target SDK workers, not a public CDP
    API. It doesn't make arbitrary CDP commands safe for models or website JS.
    """
    def __init__(self, socket_path, browser_path):
        if not browser_path.startswith('/devtools/browser/') or urlsplit(browser_path).query:
            raise ValueError('invalid_browser_control_path')
        self.socket_path = str(socket_path)
        self.browser_path = browser_path
        self.secret = secrets.token_urlsafe(32)
        self.server = None
        self.connections = set()
        self.ready = False
        self.url = None

    @property
    def headers(self):
        return {'Authorization': 'Bearer ' + self.secret}

    def authenticate(self, connection, request):
        supplied = request.headers.get_all('Authorization')
        if (not self.ready or request.path != self.browser_path or len(supplied) != 1
                or not hmac.compare_digest(supplied[0], self.headers['Authorization'])):
            return connection.respond(HTTPStatus.FORBIDDEN, 'Control unavailable\n')
        # Browser-originated WebSockets must never borrow a worker token.
        if request.headers.get_all('Origin'):
            return connection.respond(HTTPStatus.FORBIDDEN, 'Browser origins forbidden\n')
        return None

    async def relay(self, downstream):
        self.connections.add(downstream)
        try:
            async with unix_connect(self.socket_path, uri='ws://localhost'+self.browser_path,
                                    max_size=200*1024*1024, open_timeout=5) as upstream:
                self.connections.add(upstream)
                async def pipe(source, destination):
                    async for message in source:
                        if not self.ready:
                            return
                        await destination.send(message)
                tasks = [asyncio.create_task(pipe(downstream, upstream)),
                         asyncio.create_task(pipe(upstream, downstream))]
                try:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    self.connections.discard(upstream)
        finally:
            self.connections.discard(downstream)

    async def start(self):
        if self.server:
            raise ValueError('control_bridge_already_started')
        self.server = await serve(self.relay, '127.0.0.1', 0,
                                  process_request=self.authenticate,
                                  max_size=200*1024*1024, open_timeout=5, close_timeout=2)
        port = self.server.sockets[0].getsockname()[1]
        self.url = f'ws://127.0.0.1:{port}'+self.browser_path
        self.ready = True
        return self

    async def close(self):
        self.ready = False
        server, self.server = self.server, None
        if server:
            server.close()
        # Include upstream connections: revocation closes both active halves.
        await asyncio.gather(*(connection.close() for connection in list(self.connections)),
                             return_exceptions=True)
        if server:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), 5)


class NativeNetworkGate:
    """One root Fetch/recursive-target owner, sharing the browser's request context.

    Permissions and request claims are callbacks into the original task/action
    transaction. There is intentionally no replacement authorization ledger here.
    """
    def __init__(self, context, control, *, authorize, claim, lost, owner_timeout=.7, proxy=None):
        self.context = context
        self.control = control
        self.proxy=proxy
        self.auth_seen=set()
        self.scripts={}
        self.authorize = authorize
        self.claim = claim
        self.lost = lost
        self.owner_timeout = owner_timeout
        self.ws = None
        self.reader = None
        self.sequence = 0
        self.pending = {}
        self.tasks = set()
        self.targets = {}
        self.owners = {}
        self.initialized = set()
        self.roots = {}
        self.network = {}
        self.changed = asyncio.Event()
        self.context_id = None
        self.ready = False
        self.stopping = False

    @property
    def token(self):
        from websockets.protocol import State
        return self.ws if self.ready and self.ws and self.ws.state is State.OPEN else None

    async def send(self, method, params=None, session=None):
        import json
        self.sequence += 1
        number = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[number] = future
        message = {'id': number, 'method': method, 'params': params or {}}
        if session:
            message['sessionId'] = session
        try:
            await self.ws.send(json.dumps(message))
            return await asyncio.wait_for(future, 10)
        finally:
            self.pending.pop(number, None)

    def schedule(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def receive(self):
        import json
        try:
            async for raw in self.ws:
                message = json.loads(raw)
                if number := message.get('id'):
                    future = self.pending.get(number)
                    if future and not future.done():
                        if 'error' in message:
                            future.set_exception(ValueError('cdp_command_rejected'))
                        else:
                            future.set_result(message.get('result', {}))
                    continue
                event, session = message.get('params', {}), message.get('sessionId')
                method = message.get('method')
                if method == 'Target.attachedToTarget':
                    self.schedule(self.attached(event, session))
                elif method == 'Target.targetInfoChanged':
                    info=event['targetInfo']
                    for current in self.targets.values():
                        if current['targetId']==info['targetId']:
                            current.update(info)
                elif method == 'Target.detachedFromTarget':
                    root=self.owners.get(event['sessionId'])
                    if root in self.roots:
                        self.roots[root]['document_revision']=self.roots[root].get('document_revision',0)+1
                    self.scripts={key:value for key,value in self.scripts.items() if key[0]!=event['sessionId']}
                    self.targets.pop(event['sessionId'], None)
                    self.owners.pop(event['sessionId'], None)
                    self.initialized.discard(event['sessionId'])
                    self.changed.set()
                elif method == 'Network.requestWillBeSent' and session in self.targets:
                    self.network[(event['requestId'], session)] = event
                    # Request correlation is bounded and never persists permission.
                    if len(self.network) > 4096:
                        self.network.pop(next(iter(self.network)))
                    self.changed.set()
                elif method=='Runtime.executionContextsCleared':
                    self.scripts={key:value for key,value in self.scripts.items() if key[0]!=session}
                elif method=='Runtime.executionContextDestroyed':
                    self.scripts={key:value for key,value in self.scripts.items() if key[0]!=session or value.get('executionContextId')!=event['executionContextId']}
                elif method=='Debugger.scriptParsed' and session in self.targets:
                    self.scripts[(session,event['scriptId'])]=event
                    if len(self.scripts)>2048:
                        self.scripts.pop(next(iter(self.scripts)))
                elif method=='Fetch.authRequired':
                    self.schedule(self.authenticate_proxy(event))
                elif method == 'Fetch.requestPaused':
                    self.schedule(self.paused(event))
                elif method in ('Page.frameNavigated','Page.navigatedWithinDocument','Page.frameDetached'):
                    root=self.owners.get(session)
                    if root in self.roots:
                        self.roots[root]['document_revision']=self.roots[root].get('document_revision',0)+1
                        frame=event.get('frame',{})
                        if method=='Page.frameNavigated' and not frame.get('parentId'):
                            self.roots[root]['document_token']=[frame.get('id'),frame.get('loaderId'),frame.get('url')]
        finally:
            self.ready = False
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(ConnectionError('browser_control_lost'))
            if not self.stopping:
                # The caller first invalidates the proxy token, then actively
                # closes existing TLS/header connections and records uncertainty.
                await self.lost()

    async def authenticate_proxy(self,event):
        request=event['requestId']
        challenge=event.get('authChallenge',{})
        response={'response':'CancelAuth'}
        if (self.token and self.proxy and challenge.get('source')=='Proxy'
                and origin_control(challenge.get('origin',''))==origin_control(self.proxy['server'])
                and request not in self.auth_seen):
            self.auth_seen.add(request)
            response={'response':'ProvideCredentials','username':self.proxy['username'],'password':self.proxy['password']}
        await self.send('Fetch.continueWithAuth',{'requestId':request,'authChallengeResponse':response})

    async def attached(self, event, parent):
        info, session = event['targetInfo'], event['sessionId']
        target, kind = info['targetId'], info['type']
        try:
            self.targets[session] = info
            if kind in ('page', 'iframe'):
                root = target if target in self.roots else self.owners.get(parent)
                if kind == 'page' and info.get('openerId'):
                    root = next((r for r in self.roots if r == info['openerId']), None)
                    if not root or not self.roots[root].get('connection_ref'):
                        raise ValueError('popup_not_reserved')
                if root:
                    self.owners[session] = root
                await self.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True,
                    'flatten': True, 'filter': [{'type': 'iframe', 'exclude': False}, {'exclude': True}]}, session)
            await self.send('Network.enable', {}, session)
            if kind in ('page','iframe','service_worker'):
                await self.send('Runtime.enable',{},session)
                await self.send('Debugger.enable',{},session)
            if kind in ('page','iframe'):
                await self.send('Page.enable',{},session)
            await self.send('Runtime.runIfWaitingForDebugger', {}, session)
            self.initialized.add(session)
            self.changed.set()
        except Exception:
            self.targets.pop(session, None)
            self.owners.pop(session, None)
            with contextlib.suppress(Exception):
                await self.send('Target.closeTarget', {'targetId': target})

    async def start(self, socket_path, browser_path, context_id):
        self.context_id = context_id
        self.ws = await unix_connect(socket_path, uri='ws://localhost'+browser_path,
                                     max_size=200*1024*1024, open_timeout=5)
        self.reader = asyncio.create_task(self.receive())
        await self.send('Target.setDiscoverTargets',{'discover':True})
        await self.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}],'handleAuthRequests':True})
        await self.send('Target.setAutoAttach', {'autoAttach': True, 'waitForDebuggerOnStart': True,
            'flatten': True, 'filter': [{'type': 'page', 'exclude': False},
                                       {'type': 'service_worker', 'exclude': False}, {'exclude': True}]})
        self.ready = True
        return self

    async def register(self, target, scope):
        if target in self.roots and self.roots[target] != scope:
            raise ValueError('page_lease_cannot_be_rebound')
        self.roots[target] = scope
        deadline = asyncio.get_running_loop().time()+10
        while True:
            for session, info in self.targets.items():
                if info['targetId'] == target and session in self.initialized:
                    self.owners[session] = target
                    return
            if asyncio.get_running_loop().time() >= deadline:
                self.roots.pop(target, None)
                raise ValueError('page_control_not_ready')
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), .1)

    async def resolve_owner(self, event):
        request = event['request']
        identity = event.get('networkId')
        deadline = asyncio.get_running_loop().time()+self.owner_timeout
        while identity:
            current=[(session,recorded) for (request_id,session),recorded in self.network.items()
                     if request_id==identity and session in self.targets]
            matches=[(session,recorded) for session,recorded in current
                     if recorded.get('frameId')==event.get('frameId')
                     and all(recorded['request'].get(k)==request.get(k) for k in ('url','method','postData'))]
            # Chromium's SW script bootstrap has an initiating Fetch frame but
            # omits Network.frameId. Its unique network identity and current
            # managed SW session remain necessary; URL-only guesses never bind.
            if not matches and len(current)==1:
                session,recorded=current[0]
                target=self.targets[session]
                if (target.get('type')=='service_worker' and self.context_id
                        and target.get('browserContextId')==self.context_id and not recorded.get('frameId')
                        and all(recorded['request'].get(k)==request.get(k) for k in ('url','method','postData'))):
                    matches=current
            if len(matches) == 1:
                session, recorded = matches[0]
                return session, self.targets[session]
            if len(matches) > 1:
                raise ValueError('request_owner_ambiguous')
            left = deadline-asyncio.get_running_loop().time()
            if left <= 0:
                break
            self.changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), left)
        raise ValueError('request_owner_unresolved')

    async def reject(self, event, status=403):
        import base64
        await self.send('Fetch.fulfillRequest', {'requestId': event['requestId'], 'responseCode': status,
            'responseHeaders': [{'name': 'Content-Type', 'value': 'application/json'},
                                {'name': 'Cache-Control', 'value': 'no-store'}],
            'body': base64.b64encode(b'{"state":"request_not_authorized"}').decode()})

    async def paused(self, event):
        import base64
        try:
            if not self.token:
                raise ValueError('control_not_ready')
            session, target = await self.resolve_owner(event)
            root = self.owners.get(session)
            scope = self.roots.get(root)
            expected_context=scope.get('probe_context_id') if scope and scope.get('kind')=='identity_probe' else self.context_id
            if not expected_context or target.get('browserContextId') != expected_context:
                raise ValueError('request_context_unmanaged')
            request = {**event['request'],'_native_network':{'session':session,'network_id':event.get('networkId')}}
            if request.get('hasPostData') and 'postData' not in request:
                raise ValueError('request_body_unavailable')
            root = self.owners.get(session)
            scope = self.roots.get(root)
            decision = await self.authorize(scope, target, request)
            if not decision or decision.get('kind') not in ('read', 'authentication', 'action'):
                raise ValueError('request_scope_denied')
            if decision['kind'] != 'action':
                if decision['kind'] == 'authentication' and (not scope or not scope.get('connection_ref') or target['type'] == 'service_worker'):
                    raise ValueError('connection_role_not_owned')
                if not self.token:
                    raise ValueError('control_lost_before_continue')
                return await self.send('Fetch.continueRequest', {'requestId': event['requestId']})
            decision['sender_frame']=event.get('frameId')
            # Current permission checks and request claim are one original DB
            # transaction. A prior claim returns False: only reconcile the action.
            if not await self.claim(scope, target, request, decision):
                return await self.reject(event, 409)
            if not self.token:
                raise ValueError('control_lost_after_claim')
            headers = {k: v for k, v in request.get('headers', {}).items()
                       if k.lower() not in ('host', 'content-length', 'connection', 'cookie')}
            headers['Cookie']=decision.get('cookie_header','')
            try:
                response = await self.context.request.fetch(request['url'], method=request['method'], headers=headers,
                    data=(request.get('postData') or '').encode(), max_redirects=0, max_retries=0, timeout=15000)
                if decision.get('record_transport'):
                    await decision['record_transport']({'status':response.status})
                await self.send('Fetch.fulfillRequest', {'requestId': event['requestId'],
                    'responseCode': response.status, 'responseHeaders': [header for header in response.headers_array
                        if header['name'].lower() not in ('content-length','content-encoding','transfer-encoding')],
                    'body': base64.b64encode(await response.body()).decode()})
                await response.dispose()
            except Exception as error:
                if decision.get('record_transport'):
                    await decision['record_transport']({'error_type':type(error).__name__})
                # DB remains dispatching; a transport response is never a receipt.
                await self.reject(event, 502)
        except Exception:
            with contextlib.suppress(Exception):
                await self.send('Fetch.failRequest', {'requestId': event['requestId'], 'errorReason': 'BlockedByClient'})

    async def close(self):
        self.ready = False
        self.stopping = True
        # The manager closes its proxy BEFORE disabling Fetch/closing control;
        # Chrome releases paused requests on control loss.
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)
        if self.ws:
            await self.ws.close()
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)


class PersistentRuntime:
    """The existing Actor's Chromium/context/proxy/control lifecycle owner."""
    def __init__(self, actor):
        from .db import tx
        from .execution_routes import endpoint, resolve
        from .managed_network import policy

        self.actor = actor
        # The instance keeps its original execution location and managed exit.
        # A later route edit cannot lend this already-running process a new role.
        reference=resolve(actor.aid,profile_id=actor.profile['id'])
        self.execution_ref = reference.model_dump(mode='json')
        self.transport_endpoint=endpoint(reference)
        self.network_stamp = managed_exit_stamp(policy(actor.aid))
        with tx(actor.aid) as c:
            self.profile_exit=profile_exit_stamp(c,actor.aid,actor.profile['id'])
        self.context = None
        self.playwright = None
        self.gate = None
        self.bridge = None
        self.control_directory = None
        self.closed = False
        self.closing = False
        self.slots = asyncio.Semaphore(2)
        self.internal_slots = asyncio.Semaphore(2)
        self.pages = {}
        self.probes={}
        self.identity_options={}

    def forward_token(self):
        return (self.actor.epoch, self.gate.token) if not self.closed and self.gate and self.gate.token else None

    async def lost(self):
        from .browser_actions import mark_untrusted
        self.closed = True
        self.actor.disconnected = True
        if self.actor.forward_proxy:
            await self.actor.forward_proxy.close()
        if self.actor.session:
            await asyncio.to_thread(mark_untrusted, self.actor, self.actor.epoch)
        if self.bridge:
            await self.bridge.close()

    async def start(self, upstream):
        import os
        import socket
        import tempfile
        from pathlib import Path

        import httpx
        from playwright.async_api import async_playwright

        from . import network_namespace
        from .config import ROOT, STATE, env
        from .forward_proxy import BrowserForwardProxy

        actor = self.actor
        self.actor.forward_proxy = await BrowserForwardProxy(actor.aid, upstream, str(actor.profile['id']),
                                                             forward_guard=self.forward_token).start()
        storage = actor.folder/'storage'
        storage.mkdir(parents=True, exist_ok=True, mode=0o700)
        assets = STATE/'assets'/actor.aid
        assets.mkdir(parents=True, exist_ok=True, mode=0o700)
        controls = STATE/'sockets'
        controls.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.control_directory = Path(tempfile.mkdtemp(prefix='cdp-',dir=controls)).resolve()
        with socket.socket() as chooser:
            chooser.bind(('127.0.0.1',0))
            port = chooser.getsockname()[1]
        self.playwright = await async_playwright().start()
        assets_root = ROOT/'services/browser-runtime'
        if env().get('LEAD_DEPLOYMENT_MODE') != 'production':
            assets_root = Path(env().get('LEAD_BROWSER_RUNTIME_DIR') or assets_root)
        binary = assets_root/'chromium/chrome-linux64/chrome'
        if env().get('LEAD_DEPLOYMENT_MODE') != 'production' and not binary.is_file():
            binary = Path(self.playwright.chromium.executable_path)
        if not binary.is_file():
            raise ValueError('verified_chromium_runtime_unavailable')
        options = {'executable_path': str(binary), 'env': {k:v for k,v in os.environ.items()
            if k in ('PATH','HOME','LANG','LC_ALL','TZ')}, 'args': [f'--remote-debugging-port={port}',
            '--remote-debugging-address=127.0.0.1','--disable-quic','--disable-background-networking',
            '--proxy-bypass-list=<-loopback>','--force-webrtc-ip-handling-policy=disable_non_proxied_udp']}
        options = network_namespace.chromium_options(options,actor.aid,actor.forward_proxy,storage,
                                                      control_directory=self.control_directory,cdp_port=port)
        from .managed_network import policy
        network_policy=policy(actor.aid) or {}
        self.identity_options={key:value for key,value in {'locale':network_policy.get('locale'),
            'timezone_id':network_policy.get('timezone')}.items() if value}
        self.context = await self.playwright.chromium.launch_persistent_context(str(storage),headless=True,
            offline=True,service_workers='allow',accept_downloads=False,proxy=actor.forward_proxy.browser_proxy,
            viewport={'width':1280,'height':800},**self.identity_options,**options)
        actor.context = self.context
        actor.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        session = await self.context.new_cdp_session(actor.page)
        info = (await session.send('Target.getTargetInfo'))['targetInfo']
        await session.detach()
        socket_path = str(self.control_directory/'cdp.sock')
        async with httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=socket_path),timeout=5) as client:
            response = await client.get('http://localhost/json/version')
            response.raise_for_status()
            browser_path = urlsplit(response.json()['webSocketDebuggerUrl']).path
        self.gate = NativeNetworkGate(self.context,None,authorize=self.authorize_request,
                                      claim=self.claim_request,lost=self.lost,proxy=actor.forward_proxy.browser_proxy)
        await self.gate.start(socket_path,browser_path,info['browserContextId'])
        self.bridge = await ControlBridge(socket_path,browser_path).start()
        # No page scope has been granted yet. Existing SW requests remain behind
        # the root gate while ordinary Service Worker functionality stays enabled.
        await self.context.set_offline(False)
        return self

    async def authorize_request(self, scope, target, request):
        from .browser_execution import match_request, origin
        from .db import one, tx
        if not self.forward_token():
            return None
        if scope and scope.get('kind')=='identity_probe':
            await asyncio.to_thread(self.current_scope,scope)
            return {'kind':'read'} if request['method']=='GET' and canonical_probe_url(request['url']) in {canonical_probe_url(url) for url in scope['probe_urls']} else None
        endpoint=(origin(request['url']),urlsplit(request['url']).path)
        with tx(self.actor.aid) as c:
            auth_rows=c.execute("""SELECT endpoint FROM lead.takeover_sessions t,
                LATERAL jsonb_array_elements_text(coalesce(t.connection_data->'authentication_endpoints','[]')) AS endpoints(endpoint)
                WHERE t.profile_id=%s AND t.purpose='login'""",(self.actor.profile['id'],)).fetchall()
        known_auth={(origin(row['endpoint']),urlsplit(row['endpoint']).path) for row in auth_rows}
        if endpoint in known_auth and (not scope or not scope.get('connection_ref') or target['type']=='service_worker'):
            return None
        if scope:
            await asyncio.to_thread(self.current_scope, scope)
            permitted = {origin(url) for url in scope['origins']}
            if origin(request['url']) not in permitted:
                return None
            endpoint = (origin(request['url']),urlsplit(request['url']).path)
            authentication = {(origin(url),urlsplit(url).path) for url in scope.get('authentication_endpoints',[])}
            if endpoint in authentication:
                if not scope.get('connection_ref') or target['type']=='service_worker':
                    return None
                if request['method'] not in ('GET','HEAD'):
                    import json
                    from urllib.parse import parse_qs

                    from .connection_control import unseal
                    with tx(self.actor.aid) as c:
                        connection=one(c,'SELECT encrypted_login,control_mode FROM lead.takeover_sessions WHERE id=%s',(scope['connection_ref'],))
                    if not connection or connection['control_mode']!='automatic':
                        return {'kind':'authentication'} if connection and connection['control_mode']=='owner' else None
                    credentials=unseal(self.actor.aid,scope['connection_ref'],connection['encrypted_login'])
                    fields=scope.get('authentication_fields',{})
                    try:
                        body=json.loads(request.get('postData',''))
                    except ValueError:
                        values=parse_qs(request.get('postData',''),keep_blank_values=True)
                        if any(len(items)!=1 for items in values.values()):
                            return None
                        body={key:items[0] for key,items in values.items()}
                    if (not credentials or not isinstance(body,dict) or not fields
                            or 'saved_password' not in fields.values()
                            or any(body.get(key)!=credentials[value.removeprefix('saved_')] for key,value in fields.items())):
                        return None
                    scope['authentication_dispatched']=True
                return {'kind':'authentication'}
            if request['method'] in ('GET','HEAD'):
                return {'kind':'read'} if not scope.get('read_urls') or request['url'] in scope['read_urls'] else None
            if request['method']=='POST' and scope.get('kind')!='identity_probe':
                from .native_read_roles import authorize_search
                if proof:=await authorize_search(self,scope,target,request):
                    return {'kind':'read','read_proof':proof}
            if scope.get('action'):
                try:
                    match_request(scope['action']['intent']['browser_method'],request,scope['action']['intent'])
                except (KeyError,ValueError):
                    return None
                return {'kind':'action','action_id':str(scope['action']['id'])}
        # A worker URL cannot establish the initiating account/page. Only one
        # exact existing action envelope is eligible, with all current scope
        # checks performed again by the original claim transaction.
        if target['type'] == 'service_worker':
            # Preserve ordinary worker network reads in its registered context,
            # but never borrow connection/authentication roles from a page.
            readers = []
            for value in self.pages.values():
                lease = value['scope']
                try:
                    await asyncio.to_thread(self.current_scope, lease)
                    allowed = {origin(url) for url in lease['origins']}
                    auth_paths = {(origin(url),urlsplit(url).path) for url in lease.get('authentication_endpoints',[])}
                    if ((origin(request['url']),urlsplit(request['url']).path) in auth_paths):
                        return None
                    if origin(target['url']) in allowed and origin(request['url']) in allowed:
                        if lease.get('read_urls') and request['url'] not in lease['read_urls']:
                            continue
                        readers.append(lease)
                except (ValueError,KeyError):
                    continue
            identities = {(item.get('account_id'),item.get('binding_version')) for item in readers}
            if request['method'] in ('GET','HEAD') and readers and len(identities) == 1:
                return {'kind':'read'}
            if request['method']=='POST':
                from .native_read_roles import authorize_maintenance
                if proof:=await authorize_maintenance(self,target,request):
                    return {'kind':'read','read_proof':proof}
            candidates = []
            for value in self.pages.values():
                lease = value['scope']
                if not lease.get('action'):
                    continue
                try:
                    await asyncio.to_thread(self.current_scope,lease)
                    if origin(target['url']) not in {origin(url) for url in lease['origins']}:
                        continue
                    match_request(lease['action']['intent']['browser_method'],request,lease['action']['intent'])
                    candidates.append(lease)
                except (ValueError,KeyError):
                    continue
            if len(candidates) == 1:
                return {'kind':'action','action_id':str(candidates[0]['action']['id']), 'scope':candidates[0]}
        return None

    def current_scope(self, scope):
        from .channel_contracts import ExecutionRef
        from .db import one, tx
        from .execution_routes import endpoint
        from .growth.contracts import utcnow
        from .managed_network import policy

        if scope.get('fragment_deadline'):
            from datetime import datetime
            if datetime.fromisoformat(scope['fragment_deadline'])<=utcnow():
                raise ValueError('native_fragment_deadline')

        network=policy(self.actor.aid)
        if ((network is not None and not network['usable'])
                or managed_exit_stamp(network)!=self.network_stamp):
            raise ValueError('browser_exit_changed')
        with tx(self.actor.aid) as c:
            reconcile=None
            if scope.get('reconcile_action_id'):
                reconcile=one(c,"SELECT * FROM lead.actions WHERE id=%s AND task_id=%s AND state IN ('unknown','dispatching','confirmed')",
                    (scope['reconcile_action_id'],scope['task_id']))
                if (not reconcile or reconcile['intent'].get('execution_ref')!=self.execution_ref
                        or scope['origins']!=[reconcile['intent']['browser_method']['target_url']]):
                    raise ValueError('original_action_execution_unavailable')
            if profile_exit_stamp(c,self.actor.aid,self.actor.profile['id'])!=self.profile_exit:
                raise ValueError('browser_profile_exit_changed')
            route=one(c,'SELECT * FROM lead.profile_execution_routes WHERE profile_id=%s',(self.actor.profile['id'],))
            if (not reconcile and (not route or any(str(route[key])!=str(self.execution_ref[ref]) for key,ref in
                    (('profile_id','profile_id'),('executor_ref','executor_ref'),('transport_ref','transport_ref'),('generation','route_generation'))))) or endpoint(ExecutionRef.model_validate(self.execution_ref))!=self.transport_endpoint:
                raise ValueError('browser_execution_route_changed')
            if scope.get('kind')=='identity_probe':
                valid=self.probes.get(scope.get('probe_ref'))
                if not valid or valid is not scope:
                    raise ValueError('identity_probe_scope_expired')
                if not one(c,'''SELECT l.profile_id FROM lead.profile_leases l
                    JOIN lead.browser_sessions b ON b.profile_id=l.profile_id AND b.epoch=l.epoch
                    JOIN lead.agents a ON a.id=l.agent_id WHERE l.profile_id=%s AND l.epoch=%s AND l.state='held'
                    AND l.expires_at>now() AND b.id=%s AND b.ended_at IS NULL
                    AND a.control_version=%s AND a.desired_state<>'archived' ''',
                    (self.actor.profile['id'],self.actor.epoch,self.actor.session,scope['control_version'])):
                    raise ValueError('identity_probe_instance_changed')
                return None
            row = one(c,'''SELECT t.*,a.control_version,a.desired_state FROM lead.tasks t
                JOIN lead.agents a ON a.id=t.agent_id WHERE t.id=%s''',(scope['task_id'],))
            owner_connection=bool(scope.get('connection_ref') and scope.get('control_mode')=='owner')
            if (not row or row['state'] not in (('running','waiting_user','planned','queued') if owner_connection else ('running',)) or row['control_version'] != scope['control_version']
                    or row['input'].get('task_generation') != scope['task_generation']):
                raise ValueError('browser_task_scope_changed')
            if not one(c,'''SELECT 1 FROM lead.profile_leases WHERE profile_id=%s AND state='held'
                    AND epoch=%s AND expires_at>now()''',(self.actor.profile['id'],self.actor.epoch)):
                raise ValueError('browser_profile_lease_expired')
            if not one(c,'''SELECT id FROM lead.browser_sessions WHERE id=%s AND profile_id=%s
                    AND epoch=%s AND state='ready' AND ended_at IS NULL''',
                    (self.actor.session,self.actor.profile['id'],self.actor.epoch)):
                raise ValueError('browser_instance_changed')
            runtime = (row['result'] or {}).get('runtime',{})
            from datetime import datetime
            if not owner_connection and (runtime.get('control_version')!=scope['control_version']
                    or runtime.get('attempt_token') != scope['claim']['attempt_token'] or not runtime.get('lease_until')
                    or datetime.fromisoformat(runtime['lease_until']) <= utcnow()):
                raise ValueError('browser_task_lease_expired')
            if scope.get('connection_ref'):
                connection = one(c,"SELECT * FROM lead.takeover_sessions WHERE id=%s AND purpose='login'",(scope['connection_ref'],))
                if (not connection or connection['expires_at'] <= utcnow() or connection['state'] != 'active'
                        or connection.get('control_mode') != scope.get('control_mode','automatic') or str(connection['browser_session_id']) != str(self.actor.session)
                        or str(connection['profile_id']) != str(self.actor.profile['id'])
                        or connection['control_version'] != scope['control_version']
                        or connection['challenge_version'] != scope.get('challenge_version')
                        or not owner_connection and connection['input_seq'] != scope.get('input_seq')):
                    raise ValueError('connection_control_changed')
            elif row['desired_state'] != 'running' and not reconcile:
                raise ValueError('browser_project_paused')
            if scope.get('account_id'):
                account = one(c,'SELECT * FROM lead.social_accounts WHERE id=%s',(scope['account_id'],))
                if (not account or str(account['profile_id']) != str(self.actor.profile['id'])
                        or account['binding_version'] != scope['binding_version']
                        or not scope.get('connection_ref') and account['state'] != 'connected'):
                    raise ValueError('browser_account_changed')
                if not scope.get('connection_ref'):
                    intent = one(c,'SELECT desired,revision FROM lead.account_operating_intents WHERE account_id=%s',(scope['account_id'],))
                    if not intent or intent['desired'] != 'running' or intent['revision'] != scope.get('intent_revision'):
                        raise ValueError('browser_account_intent_changed')
            if not scope.get('connection_ref') and one(c,'''SELECT id FROM lead.takeover_sessions
                    WHERE profile_id=%s AND state IN ('requested','active','ending') AND expires_at>now()
                    AND (purpose<>'login' OR EXISTS(SELECT FROM lead.tasks w
                        WHERE w.input#>>'{spec,payload,takeover_id}'=lead.takeover_sessions.id::text
                        AND w.input#>>'{spec,payload,connection_account_id}'=%s))''',
                    (self.actor.profile['id'],str(scope.get('account_id')))):
                raise ValueError('browser_owner_control_active')
            if scope.get('internal_parent'):
                parent=self.pages.get(scope['internal_parent'])
                if not parent or any(parent['scope'].get(key)!=scope.get(key) for key in
                        ('task_id','account_id','binding_version','control_version','intent_revision')):
                    raise ValueError('browser_internal_parent_changed')
            return row

    async def claim_request(self, scope, target, request, decision):
        scope = decision.get('scope') or scope
        if not scope or not scope.get('action'):
            return False
        controller=self.pages[scope['target_id']].get('action_controller')
        return await controller.verify_request(request,decision) if controller else False

    async def acquire_identity_probe(self,urls):
        from .db import one, tx
        await asyncio.wait_for(self.slots.acquire(),30)
        context=None
        token=secrets.token_urlsafe(24)
        try:
            context=await self.context.browser.new_context(offline=True,service_workers='block',
                viewport={'width':1280,'height':800},**self.identity_options)
            page=await context.new_page()
            session=await context.new_cdp_session(page)
            info=(await session.send('Target.getTargetInfo'))['targetInfo']
            await session.detach()
            with tx(self.actor.aid) as c:
                agent=one(c,'SELECT control_version FROM lead.agents WHERE id=%s',(self.actor.aid,))
            scope={'kind':'identity_probe','probe_ref':token,'task_id':'identity:'+token,
                'control_version':agent['control_version'],'origins':list(urls),'probe_urls':list(urls),
                'probe_context_id':info['browserContextId'],'target_id':info['targetId'],'generation':self.actor.epoch}
            self.probes[token]=scope
            self.pages[info['targetId']]={'page':page,'scope':scope,'slot_held':True,'probe_context':context}
            await self.gate.register(info['targetId'],scope)
            await context.set_offline(False)
            return page,info['targetId']
        except BaseException:
            self.probes.pop(token,None)
            for target,entry in list(self.pages.items()):
                if entry.get('probe_context') is context:
                    self.pages.pop(target,None)
                    self.gate.roots.pop(target,None)
            if context:
                await context.close()
            self.slots.release()
            raise

    async def acquire_page(self, scope, url, *, internal_parent=None):
        from .browser_execution import origin
        if self.closed or not self.forward_token():
            raise ValueError('browser_control_unavailable')
        scope=dict(scope)
        if internal_parent:
            scope['internal_parent']=internal_parent
        await asyncio.to_thread(self.current_scope,scope)
        if origin(url) not in {origin(value) for value in scope['origins']}:
            raise ValueError('page_url_outside_task')
        # asyncio's semaphore wakes queued waiters in order; waiting doesn't
        # consume a model call or retry. The original task supplies its deadline.
        slots=self.internal_slots if internal_parent else self.slots
        await asyncio.wait_for(slots.acquire(),timeout=30)
        page = None
        try:
            page = await self.context.new_page()
            cdp = await self.context.new_cdp_session(page)
            info = (await cdp.send('Target.getTargetInfo'))['targetInfo']
            await cdp.detach()
            scope = {**scope,'target_id':info['targetId'],'generation':self.actor.epoch}
            self.pages[scope['target_id']] = {'scope':scope,'page':page,'slot_held':True,'internal':bool(internal_parent)}
            await self.gate.register(scope['target_id'],scope)
            await page.goto(url,wait_until='domcontentloaded',timeout=45000)
            return scope
        except BaseException:
            if page:
                for target,value in list(self.pages.items()):
                    if value["page"] is page:
                        self.pages.pop(target,None)
                        self.gate.roots.pop(target,None)
                await page.close()
            slots.release()
            raise

    async def scope_view(self, target):
        entry = self.pages.get(target)
        if not entry or not self.forward_token():
            raise ValueError('page_lease_missing')
        scope = entry['scope']
        await asyncio.to_thread(self.current_scope,scope)
        targets = {info['targetId']:target for session,info in self.gate.targets.items()
                   if self.gate.owners.get(session) == target}
        return {'task_id':scope['task_id'],'generation':scope['generation'],'target_id':target,
                'origins':scope['origins'],'targets':targets,
                **{key:scope[key] for key in ('connection_ref','control_mode','challenge_version') if key in scope}}

    async def yield_page(self,target):
        entry=self.pages.get(target)
        if entry and entry.get('slot_held',True):
            entry['slot_held']=False
            (self.internal_slots if entry.get('internal') else self.slots).release()

    async def release_page(self, target):
        for child,entry in list(self.pages.items()):
            if entry['scope'].get('internal_parent')==target:
                await self.release_page(child)
        entry = self.pages.pop(target,None)
        self.gate.roots.pop(target,None)
        if entry:
            self.probes.pop(entry['scope'].get('probe_ref'),None)
            if entry.get('probe_context'):
                await entry['probe_context'].close()
            else:
                await entry['page'].close()
            if entry.get('slot_held',True):
                (self.internal_slots if entry.get('internal') else self.slots).release()

    async def close(self):
        import shutil
        if self.closing:
            return
        self.closing = True
        self.closed = True
        # Exit revocation precedes CDP shutdown. Never release Fetch while the
        # original authenticated proxy still has live upstream connections.
        if self.actor.forward_proxy:
            await self.actor.forward_proxy.close()
        if self.actor.session:
            from .browser_actions import mark_untrusted
            await asyncio.to_thread(mark_untrusted,self.actor,self.actor.epoch)
        if self.bridge:
            await self.bridge.close()
        if self.gate:
            await self.gate.close()
        if self.context:
            await self.context.close()
        if self.playwright:
            await self.playwright.stop()
        if self.control_directory:
            shutil.rmtree(self.control_directory)


async def launch_native_actor(actor):
    """Use the original Actor locks, Profile lease and browser session rows."""
    import fcntl
    import os
    from types import SimpleNamespace

    from . import reddit_login
    from .browser_actions import mark_untrusted
    from .config import STATE, env, proxy_for
    from .db import all_rows, one, require_agent, tx, uid
    from .managed_network import policy
    from .scope import current_tenant

    actor.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    actor.project_file = (STATE/'profiles'/actor.aid/'profiles.lock').open('a')
    fcntl.flock(actor.project_file,fcntl.LOCK_SH|fcntl.LOCK_NB)
    actor.file = (actor.folder/'actor.lock').open('a')
    fcntl.flock(actor.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    upstream = reddit_login.assign_exit(actor.aid,actor.profile) or proxy_for(actor.aid)
    if env().get('LEAD_DEPLOYMENT_MODE') == 'production' and (not upstream or not (policy(actor.aid) or {}).get('usable')):
        raise ValueError('browser_exit_not_ready')
    with tx(actor.aid) as c:
        previous = all_rows(c,'SELECT * FROM lead.browser_sessions WHERE profile_id=%s AND ended_at IS NULL',(actor.profile['id'],))
    for old in previous:
        prior = SimpleNamespace(aid=actor.aid,profile=actor.profile,session=old['id'],epoch=old['epoch'])
        mark_untrusted(prior,old['epoch'])
    with tx(actor.aid) as c:
        agent = require_agent(c,actor.aid)
        if one(c,"SELECT 1 FROM lead.profile_maintenance WHERE state='active' AND expires_at>now()"):
            raise ValueError('browser_maintenance_active')
        c.execute("UPDATE lead.browser_sessions SET state='closed',ended_at=now() WHERE profile_id=%s AND ended_at IS NULL",(actor.profile['id'],))
        lease = one(c,"""UPDATE lead.profile_leases SET epoch=epoch+1,owner_node_id='local',owner_actor_id=%s,
            control_version=%s,state='held',expires_at=now()+interval '90 seconds' WHERE profile_id=%s RETURNING epoch""",
            (str(os.getpid()),agent['control_version'],actor.profile['id']))
        actor.epoch,actor.session = lease['epoch'],uid()
        c.execute("""INSERT INTO lead.browser_sessions(id,tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state,heartbeat_at)
            VALUES(%s,%s,%s,%s,'local',%s,%s,%s,'starting',now())""",
            (actor.session,current_tenant(),actor.aid,actor.profile['id'],str(os.getpid()),actor.epoch,agent['control_version']))
    runtime = PersistentRuntime(actor)
    actor.native_runtime = runtime
    try:
        await runtime.start(upstream)
        actor.disconnected = False
        actor.context.on('close',actor.browser_closed)
        version = await runtime.gate.send('Browser.getVersion')
        with tx(actor.aid) as c:
            c.execute("UPDATE lead.browser_sessions SET state='ready' WHERE id=%s AND ended_at IS NULL",(actor.session,))
            c.execute("UPDATE lead.browser_profiles SET state='ready',engine_version=%s,sdk_version='browser-use/0.13.14' WHERE id=%s",
                      (version['product'],actor.profile['id']))
    except BaseException:
        await runtime.close()
        raise


def origin_control(url):
    parsed=urlsplit(url)
    return parsed.scheme,parsed.hostname,parsed.port or (443 if parsed.scheme=='https' else 80)


def canonical_probe_url(url):
    parsed=urlsplit(url)
    return parsed._replace(path=parsed.path or '/',fragment='').geturl()


def managed_exit_stamp(row):
    if row is None:
        return None
    import hashlib
    return (hashlib.sha256(bytes(row['cipher'])).hexdigest(),str(row['expected_ip']),
            row['region'],row['timezone'],row['locale'])


def profile_exit_stamp(c,aid,profile_id):
    from .account_profiles import isolated_login_platforms
    from .config import env
    from .db import one
    from .reddit_login import PROVIDER
    row=one(c,'''SELECT p.network_identity_id,n.provider_ref,n.endpoint_ref,n.secret_ref,n.mode,n.expected_ip,
        n.expected_region,n.policy_version,n.state FROM lead.browser_profiles p
        JOIN lead.network_identities n ON n.id=p.network_identity_id WHERE p.id=%s''',(profile_id,))
    if not row or row['state']=='retired':
        raise ValueError('browser_profile_exit_changed')
    fields=tuple(str(row[key]) for key in ('network_identity_id','provider_ref','endpoint_ref','secret_ref',
        'mode','expected_ip','expected_region','policy_version'))
    if row['provider_ref']==PROVIDER:
        configured=env().get('LEAD_REDDIT_IPV4_'+str(aid).replace('-','').upper())
        members={value['platform'] for value in c.execute('SELECT platform FROM lead.social_accounts WHERE profile_id=%s',(profile_id,))}
        permitted={'reddit'} | (isolated_login_platforms(aid) & {'facebook','x'})
        if configured!=str(row['expected_ip']) or members-permitted:
            raise ValueError('browser_profile_exit_changed')
        fields+=(configured,)
    return fields
