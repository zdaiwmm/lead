"""Original unknown Actions retain only still-approved public-frame reads.

Action/attempt/Task/owner-source rows, pause/recovery, current_scope and frame
source helpers use the real isolated DB. DOM observation is an explicit seam;
these tests make no browser, model or upstream request.
"""
import copy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import domain
from lead_app.account_intents import set_intent
from lead_app.browser_runtime import PersistentRuntime
from lead_app.db import j, one, tx, uid
from lead_app.growth.contracts import utcnow
from lead_app.growth.runtime import advance
from lead_app.growth.tools import prepare_channel
from lead_app.native_frame_roles import canonical_roles, connection_sources, digest
from test_browser_action_transactions import native_work
from test_growth_account_connection import login_work
from test_native_readonly_admission import independent_work, unknown


def reconcile_case(aid, business=None):
    actor, action, claim, *_ = native_work(aid, provider='chromium')
    method = action['intent']['browser_method']
    _, connection, _ = login_work(aid, method['account_id'])
    source = {'state': 'approved', 'candidate_id': 'a'*64,
        'resource_url': 'https://public.fixture/reference', 'parent_url': method['target_url'],
        'expires_at': (utcnow()+timedelta(minutes=10)).isoformat(),
        'profile_id': str(actor.profile['id']), 'execution_ref': copy.deepcopy(action['intent']['execution_ref']),
        'bound': {'account_id': method['account_id'], 'binding_version': action['intent']['binding_version'],
            'intent_revision': action['intent']['intent_revision'], 'control_version': action['expected_control_version'],
            'browser_session_id': str(actor.session), 'browser_epoch': actor.epoch}}
    source['source_proof'] = digest(source)
    with tx(aid) as c:
        c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now(),completed_account_id=%s,connection_data=connection_data||%s WHERE id=%s",
            (method['account_id'], j({'read_dependencies': [source]}), connection['id']))
    ref = unknown(aid, actor, action, claim)
    if business == 'paused':
        set_intent(aid, method['account_id'], 'paused', source='owner_command', source_ref='frame-reconcile-pause')
        domain.set_control(aid, 'pause')
    elif business == 'revoked_grant':
        with tx(aid) as c:
            c.execute('UPDATE lead.authorization_grants SET revoked_at=now() WHERE id=%s', (action['grant_id'],))
    admitted = advance(ref, now=utcnow()+timedelta(minutes=6))
    assert admitted['state'] == 'ready', admitted
    with tx(aid) as c:
        task = one(c, 'SELECT t.*,a.control_version FROM lead.tasks t JOIN lead.agents a ON a.id=t.agent_id WHERE t.id=%s', (action['task_id'],))
    scope = {'task_id': str(action['task_id']), 'task_generation': task['input']['task_generation'],
        'control_version': task['control_version'], 'claim': {'attempt_token': admitted['claim']['attempt_token']},
        'origins': [method['target_url']], 'reconcile_action_id': str(action['id']),
        'read_urls': [method['target_url'], method['self_url']], 'target_id': 'original-root', 'generation': actor.epoch}
    runtime = PersistentRuntime(actor)
    runtime.gate = SimpleNamespace(token='original-instance',
        documents={'original-root': ['root-frame', 'current-loader', method['target_url']]},
        targets={'root-session': {'targetId': 'original-root', 'type': 'page'}},
        owners={'root-session': 'original-root'})
    runtime.pages['original-root'] = {'scope': scope, 'page': SimpleNamespace(frames=[object(), object()])}
    actor.native_runtime = runtime
    role = {key: source[key] for key in ('source_proof', 'candidate_id', 'resource_url', 'parent_url', 'expires_at')}
    role.update({key: copy.deepcopy(scope[key]) for key in ('task_id', 'task_generation', 'claim', 'generation')})
    role['document'] = runtime.gate.documents['original-root']
    return runtime, scope, source, role, connection, action


@pytest.mark.parametrize('business', [None, 'paused', 'revoked_grant'])
def test_unknown_source_uses_original_identity_without_reauthorizing_business(growth_agent, business):
    runtime, scope, source, role, _, action = reconcile_case(growth_agent, business)
    runtime.current_scope(scope)
    with tx(growth_agent) as c:
        assert connection_sources(c, runtime, scope) == [source]
        scope['frame_roles'] = [role]
        assert canonical_roles(c, runtime, scope) == [role]
        final = one(c, 'SELECT * FROM lead.actions WHERE id=%s', (action['id'],))
        attempt = one(c, 'SELECT * FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
        assert final['state'] == 'unknown' and final['intent'] == action['intent']
        assert attempt['attempt'] == 1 and attempt['observation']['browser_request']['claimed']
        if business == 'paused':
            assert one(c, 'SELECT desired FROM lead.account_operating_intents')['desired'] == 'paused'
            assert scope['control_version'] != source['bound']['control_version']
        elif business == 'revoked_grant':
            assert one(c, 'SELECT revoked_at FROM lead.authorization_grants WHERE id=%s', (action['grant_id'],))['revoked_at']
    assert 'account_id' not in scope and 'intent_revision' not in scope


@pytest.mark.parametrize('change', ['withdrawn', 'expired', 'wrong_action', 'missing_action', 'wrong_task', 'cross_account',
    'old_binding', 'wrong_intent', 'wrong_control', 'session', 'epoch', 'route', 'parent', 'resource', 'old_claim', 'old_generation'])
def test_unknown_frame_never_adopts_another_or_retired_owner_source(growth_agent, change):
    runtime, scope, source, role, connection, action = reconcile_case(growth_agent)
    if change == 'wrong_action':
        other_ref, data = independent_work(growth_agent, action, 99)
        other_claim = advance(other_ref)['claim']
        other_action = prepare_channel(growth_agent, other_ref['task_id'], data)
        unknown(growth_agent, runtime.actor, other_action, other_claim)
        scope['reconcile_action_id'] = str(other_action['id'])
    elif change == 'missing_action':
        scope['reconcile_action_id'] = uid()
    elif change == 'wrong_task':
        scope['task_id'] = uid()
    elif change == 'cross_account':
        source['bound']['account_id'] = uid()
    elif change in ('old_binding', 'wrong_intent', 'wrong_control'):
        source['bound'][{'old_binding': 'binding_version', 'wrong_intent': 'intent_revision', 'wrong_control': 'control_version'}[change]] -= 1
    elif change == 'withdrawn':
        source['state'] = 'refused'
    elif change == 'expired':
        source['expires_at'] = (utcnow()-timedelta(seconds=1)).isoformat()
    elif change == 'session':
        source['bound']['browser_session_id'] = uid()
    elif change == 'epoch':
        source['bound']['browser_epoch'] += 1
    elif change == 'route':
        source['execution_ref']['route_generation'] += 1
    elif change in ('parent', 'resource'):
        source['parent_url' if change == 'parent' else 'resource_url'] = 'https://other.fixture/reference'
    elif change == 'old_claim':
        role['claim'] = {'attempt_token': uid()}
    else:
        role['task_generation'] += 1
    with tx(growth_agent) as c:
        c.execute('UPDATE lead.takeover_sessions SET connection_data=connection_data||%s WHERE id=%s',
            (j({'read_dependencies': [source]}), connection['id']))
    scope['frame_roles'] = [role]
    with pytest.raises(ValueError):
        runtime.current_scope(scope)


@pytest.mark.asyncio
async def test_reconcile_descriptor_consumes_original_frame_observer(growth_agent, monkeypatch):
    from lead_app import native_frame_roles

    runtime, scope, source, _, _, _ = reconcile_case(growth_agent, 'paused')
    async def observed(current, lease, page):
        # Only the DOM boundary is synthetic. Re-enter the actual source proof
        # and original Task authority before returning an observation projection.
        current.current_scope(lease)
        with tx(growth_agent) as c:
            assert connection_sources(c, current, lease) == [source]
        return [scope['origins'][0], 'https://public.fixture']
    monkeypatch.setattr(native_frame_roles, 'observation', observed)
    descriptor = await runtime.scope_view(scope['target_id'])
    assert descriptor['origins'] == [scope['origins'][0], 'https://public.fixture']
    assert scope['origins'] == [source['parent_url']] and 'account_id' not in scope


@pytest.mark.asyncio
async def test_paused_reconcile_derives_fresh_readonly_frame_and_opaque_observation(growth_agent, monkeypatch):
    from lead_app import native_frame_roles as frames

    runtime, scope, source, _, _, _ = reconcile_case(growth_agent, 'paused')
    attrs = {'index': 0, 'resource_url': source['resource_url'], 'sandbox': None,
        'credentialless': False, 'referrerpolicy': None, 'name': '', 'title': ''}
    document = {'id': 'child-frame', 'loaderId': 'child-loader', 'url': source['resource_url']}
    class Handle:
        async def dispose(self):
            pass
    class Node(Handle):
        async def element_handle(self, **kwargs):
            return self
        async def content_frame(self):
            return child
        async def evaluate(self, script, args=None):
            if args is None:
                return {'src': attrs['resource_url'], **{key: attrs[key] for key in ('sandbox', 'credentialless', 'referrerpolicy')}}
            expected, root = args
            assert expected['url'] == attrs['resource_url'] and isinstance(root, Handle)
            attrs.update(sandbox='', credentialless=True, referrerpolicy='no-referrer')
    class Page:
        url = source['parent_url']
        main_frame = object()
        async def evaluate(self, script):
            return {'parent_url': self.url, 'supported': True, 'secure': True, 'frames': [copy.deepcopy(attrs)]}
        async def evaluate_handle(self, script):
            return Handle()
        def locator(self, selector):
            assert selector == 'iframe[src]'
            return self
        def nth(self, index):
            assert index == 0
            return Node()
    page = Page()
    class Child:
        url = source['resource_url']
        parent_frame = page.main_frame
        async def evaluate(self, script):
            return {'credentialless': True, 'origin': 'null', 'storage': True, 'password': False}
    child = Child()
    page.frames = [page.main_frame, child]
    class Session:
        async def send(self, method, params=None):
            if method == 'Target.getTargetInfo':
                return {'targetInfo': {'targetId': 'isolated-child'}}
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': document}}
            assert method == 'Storage.getStorageKeyForFrame'
            raise ValueError('opaque origin and its storage key cannot be serialized')
        async def detach(self):
            pass
    async def cookies(url):
        return []
    async def cdp(frame):
        assert frame is child
        return Session()
    async def safety(*args):
        return None
    async def send(method, params, session):
        assert session == 'root-session'
        if method == 'DOM.getFrameOwner':
            return {'backendNodeId': 1}
        if method == 'DOM.resolveNode':
            return {'object': {'objectId': 'actual-child-owner'}}
        if method == 'Runtime.callFunctionOn':
            return {'result': {'value': True}}
        assert method == 'Runtime.releaseObject'
    runtime.actor.context = SimpleNamespace(cookies=cookies, new_cdp_session=cdp)
    runtime.pages[scope['target_id']]['page'] = page
    runtime.gate.network_sequence = 1
    runtime.gate.send = send
    monkeypatch.setattr(frames, 'known_conflict', safety)
    await frames.prepare(runtime, scope, page)
    assert len(scope.get('frame_roles', [])) == 1
    role = scope['frame_roles'][0]
    assert role['claim'] == scope['claim'] and role['document'] == runtime.gate.documents[scope['target_id']]
    runtime.gate.network = {('request', 'child-session'): {'_lead_sequence': 2}}
    outgoing = {'url': source['resource_url'], 'method': 'GET',
        '_native_network': {'network_id': 'request', 'session': 'child-session', 'frame_id': 'child-frame'}}
    target = {'targetId': 'isolated-child', 'type': 'iframe'}
    decision = await frames.request(runtime, scope, target, outgoing)
    assert decision['public_frame'] is True and decision['cookie_header'] == ''
    assert await frames.request(runtime, scope, target, {**outgoing, 'method': 'POST'}) is None
    assert frames.readonly_node(scope, {'target_id': 'isolated-child'})
    assert await frames.observation(runtime, scope, page) == [source['parent_url'], 'https://public.fixture']
    assert scope['origins'] == [source['parent_url']] and 'account_id' not in scope
    # The exact response guard must persist refusal for this original account
    # even though the readonly page intentionally has no business account_id.
    await decision['refuse_response'](200)
    with tx(growth_agent) as c:
        assert connection_sources(c, runtime, scope) == []
        stored = one(c, 'SELECT connection_data FROM lead.takeover_sessions WHERE completed_account_id=%s',
            (source['bound']['account_id'],))['connection_data']['read_dependencies'][0]
        assert stored['state'] == 'refused' and stored['reason'] == 'isolation_unsupported'
    with pytest.raises(ValueError, match='frame_source_expired'):
        runtime.current_scope(scope)


@pytest.mark.parametrize('field,value', [('account_id', '00000000-0000-0000-0000-000000000002'),
    ('binding_version', -1), ('intent_revision', -1)])
def test_reconcile_source_identity_cannot_come_from_page_scope(growth_agent, field, value):
    runtime, scope, *_ = reconcile_case(growth_agent)
    scope[field] = value
    with tx(growth_agent) as c, pytest.raises(ValueError):
        connection_sources(c, runtime, scope)
