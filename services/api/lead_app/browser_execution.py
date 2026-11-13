"""Native browser-use execution scoped to a current manager-issued page lease.

No model loop lives here. The SDK still observes, selects and executes actions;
Lead checks current page ownership before observation and every native tool.
"""
from urllib.parse import urljoin, urlsplit, urlunsplit


class ScopeViolation(ValueError):
    pass


def origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ScopeViolation('invalid_page_origin')
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def scoped_session_class():
    # Browser-use is the existing separate, pinned worker runtime. Importing
    # service metadata must not require the SDK inside the API interpreter.
    import asyncio
    from dataclasses import replace
    from html import escape

    from browser_use import BrowserSession
    from browser_use.browser.session_manager import SessionManager
    from browser_use.browser.watchdogs.downloads_watchdog import DownloadsWatchdog
    from browser_use.dom.views import NodeType, SerializedDOMState
    from pydantic import PrivateAttr

    class ScopedDOMState(SerializedDOMState):
        """A model-only view of the SDK's already collected, filtered tree.

        The original nodes and selector map remain the native tools' source.
        No extra CDP/JS read, indexes, permission or completion proof is added.
        """
        def __init__(self, state, targets, page_url):
            super().__init__(state._root, state.selector_map)
            self.targets, self.page_url = frozenset(targets), page_url

        def llm_representation(self, include_attributes=None):
            rows, size, visited = [], 0, 0

            def safe_href(value):
                try:
                    parsed = urlsplit(urljoin(self.page_url, value))
                    if origin(parsed.geturl()) != origin(self.page_url):
                        return None
                    # Account/canonical links may contain tokens in queries.
                    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
                except ValueError:
                    return None

            def project(node, depth=0):
                nonlocal size, visited
                if node is None or depth > 64 or visited >= 5000:
                    return None
                visited += 1
                source = node.original_node
                attributes = source.attributes or {}
                tag = source.tag_name.lower()
                if (source.target_id not in self.targets or node.ignored_by_paint_order
                        or tag in ('script', 'style', 'noscript', 'template')
                        or 'hidden' in attributes or attributes.get('aria-hidden') == 'true'
                        or tag == 'input' and attributes.get('type', '').lower() == 'hidden'):
                    return None
                visible = source.is_visible is True and source.snapshot_node is not None
                # should_display may only fold a visible structural wrapper in
                # the SDK text view; it is not a visibility/paint-order proof.
                display = visible and not node.excluded_by_parent
                # Preserve native text/indices but remove form values from the
                # model-only copy, including the SDK's unconditional AX fallback.
                copied = {key: value for key, value in attributes.items() if key not in ('value', 'nonce')}
                if 'href' in copied:
                    href = safe_href(copied['href'])
                    if href is None:
                        copied.pop('href')
                    else:
                        copied['href'] = href
                form = tag in ('input', 'textarea', 'select')
                ax = source.ax_node
                if form and ax is not None:
                    ax = replace(ax, properties=[prop for prop in ax.properties or []
                                                if prop.name not in ('value', 'valuetext')])
                if display and source.node_type == NodeType.ELEMENT_NODE and len(rows) < 32:
                    facts = {}
                    for key in ('id', 'name', 'rel', 'role', 'href'):
                        value = copied.get(key)
                        if (isinstance(value, str) and value and len(value) <= 160
                                and not any(ord(char) < 32 for char in value)):
                            facts[key] = value
                    if facts:
                        line = ' ' * min(depth, 12) + '<' + tag + ''.join(
                            ' ' + key + '="' + escape(value, quote=True) + '"' for key, value in facts.items()) + ' />'
                        if size + len(line) + 1 <= 3000:
                            rows.append(line)
                            size += len(line) + 1
                children = [] if form else [child for item in node.children
                                            if (child := project(item, depth + 1)) is not None]
                original = replace(source, attributes=copied, ax_node=ax,
                    snapshot_node=replace(source.snapshot_node, input_value=None) if form and source.snapshot_node else source.snapshot_node,
                    children_nodes=None, shadow_roots=None, content_document=None, parent_node=None,
                    hidden_elements_info=[], has_hidden_content=False)
                return replace(node, original_node=original, children=children,
                               should_display=display and node.should_display if source.node_type == NodeType.ELEMENT_NODE else node.should_display)

            tree = project(self._root)
            native = SerializedDOMState(tree, self.selector_map).llm_representation(include_attributes)
            if not rows:
                return native
            # The SDK's existing 40k observation cap still applies to this view.
            return ('Visible selector attributes (untrusted page facts; not authorization or success):\n'
                    + '\n'.join(rows) + '\nNative page observation:\n' + native)

    class NoDownloadsWatchdog(DownloadsWatchdog):
        def attach_to_session(self):
            # File tools aren't enabled. The SDK's default download listener is
            # browser-wide, even when accept_downloads is false. Don't register
            # it or change another task's existing browser download behavior.
            return None

    class OwnedSessionManager(SessionManager):
        """Keep SDK startup/watchdog initialization inside the assigned scope.

        browser-use 0.13.14 otherwise attaches every existing tab before scope
        binding. Its page/network initialization can await another task's slow
        navigation even though later agent observations are correctly fenced.
        """
        async def _initialize_existing_targets(self):
            scope = await self.browser_session.refresh_assigned_scope()
            client = self.browser_session._cdp_client_root
            targets = (await client.send.Target.getTargets()).get('targetInfos', [])
            owned = [target for target in targets if target['targetId'] in scope['targets']
                     and target.get('type') in ('page', 'iframe', 'tab')]
            if not any(target['targetId'] == scope['target_id'] for target in owned):
                raise ScopeViolation('assigned_page_missing_at_start')
            owned.sort(key=lambda target: target['targetId'] != scope['target_id'])
            for target in owned:
                await client.send.Target.attachToTarget(params={'targetId': target['targetId'], 'flatten': True})
            # The SDK's attach callback still creates sessions and enables its
            # native monitoring. Wait only for these already-authorized targets.
            def ready(target):
                session = self._get_session_for_target(target['targetId'])
                return session is not None and (target['type'] not in ('page', 'tab')
                    or getattr(session, '_lifecycle_events', None) is not None)
            async with asyncio.timeout(2):
                while not all(ready(target) for target in owned):
                    await asyncio.sleep(.02)

        async def _handle_target_attached(self, event):
            target = event['targetInfo']
            if target.get('type') not in ('page', 'iframe', 'tab'):
                return
            # An event callback must not perform an RPC that itself needs CDP
            # while a request is paused. Observation/actions still recheck live.
            scope = self.browser_session._scope or self.browser_session._startup_scope
            if target['targetId'] not in scope['targets']:
                return
            return await super()._handle_target_attached(event)

        def get_all_page_targets(self):
            browser = self.browser_session
            scope = browser._scope or browser._startup_scope
            targets = [target for target in super().get_all_page_targets() if target.target_id in scope.get('targets', {})]
            return sorted(targets, key=lambda target: target.target_id != scope.get('target_id'))

    class ScopedSession(BrowserSession):
        _lease: dict = PrivateAttr(default_factory=dict)
        _startup_scope: dict = PrivateAttr(default_factory=dict)
        _scope_reader: object = PrivateAttr(default=None)
        _scope: dict = PrivateAttr(default_factory=dict)
        _observation: object = PrivateAttr(default=None)

        def __setattr__(self, name, value):
            # The pinned SDK assigns its manager inside connect(), overriding
            # constructor injection. Replace that empty instance before its
            # start_monitoring call; no installed SDK files/global patching.
            if (name == 'session_manager' and isinstance(value, SessionManager)
                    and not isinstance(value, OwnedSessionManager) and getattr(self, '_startup_scope', None)):
                value = OwnedSessionManager(self)
            if (name == '_downloads_watchdog' and isinstance(value, DownloadsWatchdog)
                    and not isinstance(value, NoDownloadsWatchdog)):
                value = NoDownloadsWatchdog(event_bus=value.event_bus, browser_session=self)
            return super().__setattr__(name, value)

        async def refresh_assigned_scope(self):
            if not self._scope_reader:
                raise ScopeViolation('page_lease_missing')
            scope = await self._scope_reader()
            expected = self._lease or self._startup_scope
            for key in ('task_id', 'generation', 'target_id', 'connection_ref', 'control_mode', 'challenge_version'):
                if scope.get(key) != expected.get(key):
                    raise ScopeViolation('page_lease_changed')
            self._scope = scope
            return scope

        async def start_scoped(self, lease, read_scope):
            self.browser_profile = self.browser_profile.model_copy(update={
                'permissions': [], 'accept_downloads': False, 'auto_download_pdfs': False})
            self._startup_scope = dict(lease)
            self._scope_reader = read_scope
            try:
                await self.refresh_assigned_scope()
                await self.start()
                await self.bind_scope(lease, read_scope)
            except BaseException:
                # This precedes Agent.run's normal finally. Without an
                # intentional stop, SDK reconnect/event tasks can outlive a
                # rejected startup and hang asyncio.run's cancellation drain.
                self._intentional_stop = True
                try:
                    await asyncio.wait_for(self.stop(), 2)
                except (Exception, asyncio.CancelledError):
                    pass
                raise

        async def bind_scope(self, lease, read_scope):
            self._scope_reader = read_scope
            self._lease = dict(lease)
            self._scope = await read_scope()
            target = self._lease['target_id']
            await BrowserSession.get_or_create_cdp_session(self, target_id=target, focus=True)
            await self.live_scope()

        def assert_scope(self):
            target = self._lease.get('target_id')
            if not target or not self.session_manager.get_target(target):
                raise ScopeViolation('assigned_page_missing')
            if self.agent_focus_target_id != target:
                raise ScopeViolation('assigned_page_focus_changed')
            if target not in self._scope.get('targets', {}):
                raise ScopeViolation('page_ownership_revoked')

        async def live_scope(self):
            if not self._scope_reader:
                raise ScopeViolation('page_lease_missing')
            scope = await self._scope_reader()
            for key in ('task_id', 'generation', 'target_id', 'connection_ref', 'control_mode', 'challenge_version'):
                if scope.get(key) != self._lease.get(key):
                    raise ScopeViolation('page_lease_changed')
            self._scope = scope
            self.assert_scope()
            allowed = {origin(url) for url in scope['origins']}
            facts = []
            for target_id in sorted(scope['targets']):
                session = await self.get_or_create_cdp_session(target_id=target_id, focus=False)
                info = await session.cdp_client.send.Target.getTargetInfo(params={'targetId': target_id}, session_id=session.session_id)
                target = info['targetInfo']
                if (not target.get('attached') or target.get('type') not in ('page', 'iframe')
                        or origin(target.get('url', '')) not in allowed):
                    raise ScopeViolation('observation_target_out_of_scope')
                tree=await session.cdp_client.send.Page.getFrameTree(session_id=session.session_id)
                def frame_facts(node):
                    frame=node['frame']
                    if origin(frame.get('url','')) not in allowed:
                        raise ScopeViolation('observation_frame_out_of_scope')
                    rows=[(frame['id'],frame.get('loaderId'),frame['url'])]
                    for child in node.get('childFrames',[]):
                        rows.extend(frame_facts(child))
                    return rows
                facts.append((target_id,target['url'],frame_facts(tree['frameTree'])))
            return scope, facts

        async def get_or_create_cdp_session(self, target_id=None, focus=True):
            if self._lease:
                self.assert_scope()
                target_id = target_id or self._lease['target_id']
                if target_id != self._lease['target_id'] and (focus or target_id not in self._scope['targets']):
                    # Native DOM collection skips foreign frames on ValueError.
                    raise ScopeViolation('foreign_frame_target')
                if self.session_manager.get_target(target_id) is None:
                    # The SDK event can precede the manager's frame ownership
                    # event. Reattach only after scope proves this exact target.
                    await self._cdp_client_root.send.Target.attachToTarget(
                        params={'targetId': target_id, 'flatten': True})
            return await super().get_or_create_cdp_session(target_id=target_id, focus=focus)

        async def get_tabs(self):
            tabs = await super().get_tabs()
            if not self._lease:
                return []
            self.assert_scope()
            return [tab for tab in tabs if tab.target_id == self._lease['target_id']]

        async def cdp_client_for_node(self, node):
            await self.live_scope()
            if not node.target_id or node.target_id not in self._scope['targets']:
                raise ScopeViolation('stale_or_foreign_node')
            session = await super().cdp_client_for_node(node)
            if session.target_id != node.target_id:
                raise ScopeViolation('node_target_changed')
            return session

        async def get_browser_state_summary(self, include_screenshot=True, cached=False, include_recent_events=False):
            before = await self.live_scope()
            state = await super().get_browser_state_summary(include_screenshot=include_screenshot,
                                                            cached=False, include_recent_events=False)
            if before != await self.live_scope():
                raise ScopeViolation('scope_changed_during_observation')
            if origin(state.url) not in {origin(url) for url in self._scope['origins']}:
                raise ScopeViolation('observation_out_of_scope')
            def display_url(url):
                parsed = urlsplit(url)
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
            # Keep the SDK's real URL/state for its native operations. Only the
            # model view omits transient account/authentication query material.
            state = replace(state, url=display_url(state.url),
                tabs=[tab.model_copy(update={'url': display_url(tab.url)}) for tab in state.tabs
                      if tab.target_id == self._lease['target_id']],
                dom_state=ScopedDOMState(state.dom_state, self._scope['targets'], state.url))
            self._observation=await self.live_scope()
            return state

        async def assert_fresh_observation(self):
            if self._observation is None or self._observation != await self.live_scope():
                raise ScopeViolation('document_changed_reobserve_before_action')

        async def on_NavigateToUrlEvent(self, event):
            if self._lease:
                await self.live_scope()
                if event.new_tab or origin(event.url) not in {origin(url) for url in self._scope['origins']}:
                    raise ScopeViolation('navigation_out_of_scope')
            return await super().on_NavigateToUrlEvent(event)

    return ScopedSession


def guarded_tools(session, authorize, *, sensitive_data=None):
    """Wrap only tested native tools, using distinct names to prevent SDK overwrite.

    authorize receives the actual node identity and parameters before native
    input/click/navigation. It must use current TaskRef/ConnectionRef/ActionRef;
    a model's claim that an action is read-only is never permission.
    """
    from browser_use import ActionResult, Tools

    native = Tools()
    exposed = Tools(exclude_actions=[name for name in native.registry.registry.actions if name != 'done'])

    async def execute(name, values):
        await session.live_scope()
        await session.assert_fresh_observation()
        node_ref = None
        if 'index' in values:
            node = await session.get_dom_element_by_index(values['index'])
            if not node:
                raise ScopeViolation('element_no_longer_observed')
            await session.cdp_client_for_node(node)
            node_ref = {'target_id': node.target_id, 'backend_node_id': node.backend_node_id,
                        'tag': node.tag_name, 'attributes': dict(node.attributes)}
        values=dict(values)
        credential_data={key:value for key,value in (sensitive_data or {}).items() if key in ('saved_username','saved_password')}
        if name=='input' and credential_data:
            # Agent's outer registry substitutes secrets before custom actions.
            # Restore one approved placeholder before the authorization RPC and
            # let the native input registry perform the actual sensitive write.
            text=values.get('text','')
            matched=[key for key,secret in credential_data.items() if isinstance(secret,str) and text==secret]
            if matched:
                preferred='saved_password' if (node_ref or {}).get('attributes',{}).get('type')=='password' else 'saved_username'
                values['text']=preferred if preferred in matched else matched[0]
            elif any(isinstance(secret,str) and secret and secret in text for secret in credential_data.values()):
                return ActionResult(error='Use exactly one approved saved credential placeholder.')
        decision = await authorize(name, values, node_ref)
        if not decision.get('allowed'):
            return ActionResult(error=decision.get('reason', 'Current task does not authorize this action'))
        result = await native.registry.execute_action(name, values, browser_session=session,
                                                      sensitive_data=sensitive_data)
        await session.live_scope()
        return result

    @exposed.action('Navigate the assigned page within the current task scope.')
    async def guarded_navigate(url: str):
        return await execute('navigate', {'url': url, 'new_tab': False})

    @exposed.action('Click a freshly observed element after the current task intent is checked.')
    async def guarded_click(index: int):
        return await execute('click', {'index': index})

    @exposed.action('Input authorized content or an approved sensitive placeholder into a fresh element.')
    async def guarded_input(index: int, text: str, clear: bool = True):
        return await execute('input', {'index': index, 'text': text, 'clear': clear})

    @exposed.action('Scroll the assigned page within the current task scope.')
    async def guarded_scroll(down: bool = True, pages: float = 1.0):
        return await execute('scroll', {'down': down, 'pages': max(.1, min(3, pages))})

    return exposed


CAPABILITY_VERSION = 'native-browser-v1'


def canonical_numeric_id(url):
    """One literal numeric component of an observed canonical URL, or a gap."""
    import re
    from urllib.parse import parse_qsl, unquote

    parsed=urlsplit(url)
    if parsed.fragment:
        return None
    values=[unquote(part) for part in parsed.path.split('/')]
    values += [value for _,value in parse_qsl(parsed.query,keep_blank_values=True)]
    numbers=[value for value in values if re.fullmatch(r'[0-9]+',value)]
    return numbers[0] if len(numbers)==1 else None


def safe_parent_field(name):
    import re

    if not isinstance(name,str) or not 1<=len(name)<=100:
        return False
    normalized=re.sub(r'([a-z0-9])([A-Z])',r'\1_\2',name).lower()
    if re.search(r'password|secret|credential|authorization|session|token|csrf|nonce|api[_-]?key|authenticity|author|user|account|owner|member|profile|identity|subject|payment|billing|charge|amount|price|cost|currency|purchase|subscribe|credit',normalized):
        return False
    return not re.search(r'(?:^|\W|_)(?:admin|moderator|role|privilege|permission|grant|delete|remove|destroy|ban|invite|join|follow|like|share|scope|action|op|operation|mode|method|command|cmd|intent|key|sig|signature|access)(?:$|\W|_)',normalized)


def validate_method(method, *, account_id, binding_version, target_url, operation):
    """Validate declarative, evidence-backed facts; never execute model code.

    A method is scoped to the actual binding and target. Public-page methods
    and another account's old permissions cannot satisfy this contract.
    """
    import copy
    from uuid import UUID
    value = copy.deepcopy(method)
    if (not isinstance(value, dict) or value.get('version') != CAPABILITY_VERSION
            or str(value.get('account_id')) != str(UUID(str(account_id)))
            or value.get('binding_version') != binding_version or value.get('target_url') != target_url
            or operation not in ('publish', 'reply') or operation not in value.get('operations', [])
            or not value.get('evidence_refs')):
        raise ValueError('browser_method_scope_changed')
    if (not isinstance(value.get('request'),dict) or not isinstance(value.get('selectors'),dict)
            or not isinstance(value.get('platform_permission'),dict)
            or not isinstance(value.get('operations'),list)
            or not value['operations'] or set(value['operations'])-{'publish','reply'}):
        raise ValueError('browser_method_contract_invalid')
    for refs in (value.get('evidence_refs'),value['platform_permission'].get('evidence_refs')):
        if not isinstance(refs,list) or not 1 <= len(refs) <= 20 or any(not isinstance(ref,str) or not 1 <= len(ref) <= 500 for ref in refs):
            raise ValueError('browser_method_evidence_invalid')
    expected_origin = origin(target_url)
    if any(origin(url) != expected_origin for url in (value.get('self_url', ''), value.get('request', {}).get('url', ''))):
        raise ValueError('browser_method_origin_changed')
    if value.get('role') not in ('owner', 'administrator', 'ordinary_member'):
        raise ValueError('browser_method_role_missing')
    selectors = value.get('selectors', {})
    required = ['self_subject', 'editor_target', 'editor_body', 'submit']
    if value.get('receipt_state','observed')=='observed':
        required += ['receipt_author','receipt_body','receipt_url']
    elif value.get('receipt_state')!='unobserved':
        raise ValueError('browser_receipt_contract_invalid')
    if operation == 'reply':
        required += ['editor_parent']
        if value.get('receipt_state','observed')=='observed':
            required += ['receipt_parent']
    if any(not isinstance(selectors.get(key), str) or not 1 <= len(selectors[key]) <= 300 for key in required):
        raise ValueError('browser_method_observation_missing')
    # The schema intentionally has no JS/expression/evaluate fields. Selectors
    # are read through Playwright, not interpolated into JavaScript source.
    allowed = {'version', 'account_id', 'binding_version', 'target_url', 'self_url', 'operations',
               'role', 'selectors', 'request', 'evidence_refs', 'checked_at', 'platform_permission',
               'receipt_state','self_subject_prefix','self_subject_suffix'}
    if set(value)-allowed:
        raise ValueError('browser_method_unknown_field')
    request = value['request']
    if (request.get('method') not in ('POST', 'PUT') or request.get('body_kind') not in ('json', 'form')
            or not isinstance(request.get('content_field'), str) or not request['content_field']
            or operation == 'reply' and not request.get('parent_field')
            or set(request)-{'method', 'url', 'body_kind', 'content_field', 'parent_field', 'dynamic_fields','fixed_fields','parent_binding'}):
        raise ValueError('browser_request_contract_missing')
    binding=request.get('parent_binding')
    if binding is not None and (operation!='reply' or request['body_kind']!='form'
            or not safe_parent_field(request.get('parent_field'))
            or not isinstance(binding,dict) or set(binding)!={'canonical_url','value'}
            or not isinstance(binding.get('canonical_url'),str) or len(binding['canonical_url'])>2000
            or not isinstance(binding.get('value'),str) or len(binding['value'])>100
            or origin(binding['canonical_url'])!=expected_origin
            or canonical_numeric_id(binding['canonical_url'])!=binding['value']):
        raise ValueError('browser_request_parent_binding_invalid')
    if not isinstance(request.get('dynamic_fields', []), list) or len(request.get('dynamic_fields', [])) > 12:
        raise ValueError('browser_request_contract_missing')
    if not all(isinstance(field, str) and 1 <= len(field) <= 100 for field in request.get('dynamic_fields', [])):
        raise ValueError('browser_request_contract_missing')
    fixed=request.get('fixed_fields',{})
    if (not isinstance(fixed,dict) or len(fixed)>20 or any(not isinstance(key,str) or not 1<=len(key)<=100
            or not isinstance(item,str) or len(item)>2000 for key,item in fixed.items())
            or set(fixed)&{request['content_field'],request.get('parent_field'),*request.get('dynamic_fields',[])}):
        raise ValueError('browser_request_fixed_fields_invalid')
    for key in ('self_subject_prefix','self_subject_suffix'):
        if key in value and (not isinstance(value[key],str) or len(value[key])>300):
            raise ValueError('browser_subject_contract_invalid')
    permission = value.get('platform_permission', {})
    if permission.get('state') != 'approved' or not permission.get('evidence_refs'):
        raise ValueError('platform_access_missing')
    return value


def match_request(method, request, intent):
    """Exact known request facts; unknown encoding/uploads are a local gap."""
    import hashlib
    import json
    from urllib.parse import parse_qs
    spec = method['request']
    if request.get('method') != spec['method'] or request.get('url') != spec['url']:
        raise ValueError('browser_request_does_not_match')
    if request.get('hasPostData') and 'postData' not in request:
        raise ValueError('browser_request_body_unavailable')
    body = request.get('postData', '')
    if spec['body_kind'] == 'json':
        values = json.loads(body)
    else:
        parsed = parse_qs(body, strict_parsing=True, keep_blank_values=True)
        if any(len(value) != 1 for value in parsed.values()):
            raise ValueError('browser_request_ambiguous_field')
        values = {key: value[0] for key, value in parsed.items()}
    fields = {spec['content_field'], *spec.get('dynamic_fields', []),*spec.get('fixed_fields',{})}
    if spec.get('parent_field'):
        fields.add(spec['parent_field'])
    parent=intent.get('parent_url')
    if spec.get('parent_binding') is not None:
        binding=spec['parent_binding']
        if (intent.get('operation')!='reply' or binding.get('canonical_url')!=parent
                or canonical_numeric_id(parent)!=binding.get('value')):
            raise ValueError('browser_request_parent_binding_changed')
        parent=binding['value']
    if (not isinstance(values, dict) or set(values)-fields or values.get(spec['content_field']) != intent['body']
            or any(values.get(key)!=item for key,item in spec.get('fixed_fields',{}).items())
            or intent.get('operation', 'publish') == 'reply' and values.get(spec['parent_field']) != parent):
        raise ValueError('browser_request_content_changed')
    semantic={key:item for key,item in values.items() if key not in spec.get('dynamic_fields',[])}
    return hashlib.sha256(json.dumps([request['method'],request['url'],semantic],sort_keys=True,separators=(',', ':')).encode()).hexdigest()
