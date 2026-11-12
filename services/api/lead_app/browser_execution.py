"""Native browser-use execution scoped to a current manager-issued page lease.

No model loop lives here. The SDK still observes, selects and executes actions;
Lead checks current page ownership before observation and every native tool.
"""
from urllib.parse import urlsplit


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
    from browser_use import BrowserSession
    from pydantic import PrivateAttr

    class ScopedSession(BrowserSession):
        _lease: dict = PrivateAttr(default_factory=dict)
        _scope_reader: object = PrivateAttr(default=None)
        _scope: dict = PrivateAttr(default_factory=dict)
        _observation: object = PrivateAttr(default=None)

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
                session = await BrowserSession.get_or_create_cdp_session(self, target_id=target_id, focus=False)
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
            state.tabs = [tab for tab in state.tabs if tab.target_id == self._lease['target_id']]
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
        if name=='input' and sensitive_data:
            # Agent's outer registry substitutes secrets before custom actions.
            # Restore one approved placeholder before the authorization RPC and
            # let the native input registry perform the actual sensitive write.
            text=values.get('text','')
            matched=[key for key,secret in sensitive_data.items() if isinstance(secret,str) and text==secret]
            if matched:
                preferred='saved_password' if (node_ref or {}).get('attributes',{}).get('type')=='password' else 'saved_username'
                values['text']=preferred if preferred in matched else matched[0]
            elif any(isinstance(secret,str) and secret and secret in text for secret in sensitive_data.values()):
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
            or set(request)-{'method', 'url', 'body_kind', 'content_field', 'parent_field', 'dynamic_fields','fixed_fields'}):
        raise ValueError('browser_request_contract_missing')
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
    if (not isinstance(values, dict) or set(values)-fields or values.get(spec['content_field']) != intent['body']
            or any(values.get(key)!=item for key,item in spec.get('fixed_fields',{}).items())
            or intent.get('operation', 'publish') == 'reply' and values.get(spec['parent_field']) != intent.get('parent_url')):
        raise ValueError('browser_request_content_changed')
    semantic={key:item for key,item in values.items() if key not in spec.get('dynamic_fields',[])}
    return hashlib.sha256(json.dumps([request['method'],request['url'],semantic],sort_keys=True,separators=(',', ':')).encode()).hexdigest()
