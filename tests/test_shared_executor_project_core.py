"""Pure project transport contracts; no database, model or account traffic."""
import copy
import sys
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4

import pytest
from lead_app import local_executor as executor

AID = '00000000-0000-4000-8000-000000000001'
TENANT = '00000000-0000-4000-8000-000000000002'
DEVICE = '00000000-0000-4000-8000-000000000003'
GRANT = '00000000-0000-4000-8000-000000000004'
OTHER = '00000000-0000-4000-8000-000000000005'
ROUTE = {'source': 'managed_node', 'grant_id': GRANT, 'grant_generation': 3,
         'policy_revision': 'execution/default@2'}


@pytest.fixture
def store(monkeypatch):
    state = SimpleNamespace(project=None, previous=None, session=None, task_binding=None,
        request=None, pending=None, writes=[], enqueued=[], bindings=[], active=[], commands=[])
    state.task = {'state': 'running', 'input': {'task_generation': 1},
        'result': {'runtime': {'attempt_token': 'original-attempt'}}}
    c = SimpleNamespace(execute=lambda sql, params=(): state.writes.append((sql, params)))

    def one(_connection, sql, params=()):
        if 'INSERT INTO lead.local_executor_requests' in sql:
            names = ('tenant_id', 'agent_id', 'device_id', 'task_id', 'request_key',
                     'request_hash', 'session_key', 'payload', 'scope')
            return {'id': str(uuid4()), **dict(zip(names, params, strict=True))}
        if 'FROM lead.local_executor_requests' in sql:
            if 'request_key=' in sql:
                return state.previous
            if 'session_key=' in sql:
                if state.session and 'device_id IS NOT NULL' in sql and state.session['device_id'] is None:
                    return None
                return state.session
            if 'task_id=' in sql:
                return state.task_binding
            return state.request
        if 'FROM lead.local_executor_projects' in sql:
            return state.project
        if 'FROM lead.tasks' in sql:
            return state.task
        if 'FROM lead.local_executor_events' in sql:
            return state.pending
        raise AssertionError('Unexpected synthetic read: ' + sql)

    def all_rows(_connection, sql, params=()):
        if 'FROM lead.local_executor_requests' in sql:
            return state.active
        if 'FROM lead.local_executor_commands' in sql:
            return state.commands
        raise AssertionError('Unexpected synthetic list: ' + sql)

    def binding(_connection, aid, policy):
        state.bindings.append((aid, policy))
        return {'device_id': DEVICE, 'execution_route': copy.deepcopy(ROUTE)}

    def enqueue(_connection, request, kind, payload, key):
        state.enqueued.append((request, kind, payload, key))
        return {'id': 'command'}

    nodes = SimpleNamespace(policy=lambda _c: {'source': 'managed_node', 'node_id': OTHER,
        'revision': 'execution/default@2'}, project_binding=binding,
        server_status=lambda *_a, **_k: {'source': 'managed_node', 'configured': True,
            'ready': False, 'state': 'offline'},
        is_managed_device=lambda _c, value: str(value) == DEVICE)
    monkeypatch.setitem(sys.modules, 'lead_app.executor_nodes', nodes)
    import lead_app
    monkeypatch.setattr(lead_app, 'executor_nodes', nodes, raising=False)
    monkeypatch.setattr(executor, 'tx', lambda *_a, **_k: nullcontext(c))
    monkeypatch.setattr(executor, 'current_tenant', lambda: TENANT)
    monkeypatch.setattr(executor, 'one', one)
    monkeypatch.setattr(executor, 'all_rows', all_rows)
    monkeypatch.setattr(executor, 'j', copy.deepcopy)
    monkeypatch.setattr(executor, 'require_agent', lambda *_a, **_k:
        {'id': AID, 'control_version': 7, 'desired_state': 'running'})
    monkeypatch.setattr(executor, 'audit', lambda *_a, **_k: None)
    monkeypatch.setattr(executor, '_wake_existing', lambda *_a: None)
    monkeypatch.setattr(executor, '_enqueue', enqueue)
    state.connection, state.nodes = c, nodes
    return state


def request(scope=None, **changes):
    return {'id': 'request', 'tenant_id': TENANT, 'agent_id': AID, 'device_id': DEVICE,
        'scope': {'control_version': 7, **(scope or {})}, 'payload': {'tools': [{'name': 'allowed'}]},
        'task_id': None, 'state': 'queued', 'thread_id': None, 'turn_id': None, **changes}


def test_missing_source_stays_personal_and_malformed_route_is_refused():
    decode = getattr(executor, 'execution_source', None)
    assert callable(decode), 'Immutable source decoder is required'
    assert decode({}) == 'personal_v1'
    assert decode({'execution_route': ROUTE}) == 'managed_node'
    for route in ({'source': 'managed_node'}, {'source': 'legacy'}, 'managed_node'):
        with pytest.raises(ValueError):
            decode({'execution_route': route})


def test_new_project_inherits_published_native_policy_even_offline(store):
    assert executor.enabled(AID)
    assert not executor.configured(AID)


@pytest.mark.parametrize('override,mode,want', [('legacy', 'codex', False),
    ('local_device', 'legacy', False), ('local_device', 'codex', True), ('inherit', 'legacy', True)])
def test_explicit_project_override_precedes_default_without_health_fallback(store, override, mode, want):
    store.project = {'execution_override': override, 'mode': mode, 'device_id': OTHER}
    assert executor.enabled(AID) is want


def test_fresh_managed_request_gets_fixed_server_route_in_creation_transaction(store):
    row = executor.submit(AID, 'new', {'input': 'Read', 'execution_route': {'source': 'personal_v1'}},
                          session_key='new-session')
    assert row['device_id'] == DEVICE
    assert row['scope']['execution_route'] == ROUTE
    assert row['scope']['control_version'] == 7
    assert len(store.bindings) == 1
    assert store.enqueued[0][1] == 'run'


@pytest.mark.parametrize('kind', ['session', 'task_binding'])
def test_null_historical_binding_never_moves_to_managed_default(store, kind):
    store.project = {'mode': 'codex', 'device_id': OTHER, 'execution_override': 'inherit'}
    setattr(store, kind, {'device_id': None, 'scope': {}})
    row = executor.submit(AID, 'old-follow', {'input': 'Continue'}, session_key='old-session',
        task_id=OTHER if kind == 'task_binding' else None)
    assert row['device_id'] is None
    assert row['scope'].get('execution_route', {}).get('source', 'personal_v1') == 'personal_v1'
    assert store.bindings == []


def test_existing_exact_request_returns_before_changed_default_allocation(store):
    payload = {'input': 'Read', 'session_key': 's', 'model': executor.MODEL, 'effort': executor.EFFORT}
    store.previous = {'id': 'original', 'request_hash': executor._digest(executor._encoded(
        {'payload': payload, 'task_id': None})), 'device_id': None, 'scope': {}}
    assert executor.submit(AID, 'same-key', {'input': 'Read'}, session_key='s')['id'] == 'original'
    assert not store.bindings and not store.enqueued


@pytest.mark.parametrize('source,grant,generation,scope,allowed', [
    ('personal_v1', None, None, {}, True),
    ('personal_v1', None, None, {'execution_route': ROUTE}, False),
    ('managed_node', GRANT, 3, {}, False),
    ('managed_node', GRANT, 3, {'execution_route': ROUTE}, True),
    ('managed_node', OTHER, 3, {'execution_route': ROUTE}, False),
    ('managed_node', GRANT, 4, {'execution_route': ROUTE}, False),
])
def test_tool_core_keeps_device_source_grant_and_generation(store, source, grant, generation, scope, allowed):
    core = getattr(executor, 'tool_request', None)
    assert callable(core), 'Typed core must be reusable after real service authorization'
    store.request = request(scope)
    device = {'id': DEVICE, 'agent_id': AID, 'tenant_id': TENANT}
    if allowed:
        assert core(store.connection, device, 'request', 'allowed', source=source,
            grant_id=grant, grant_generation=generation) is store.request
    else:
        with pytest.raises(ValueError):
            core(store.connection, device, 'request', 'allowed', source=source,
                grant_id=grant, grant_generation=generation)
    assert not store.writes


@pytest.mark.parametrize('method', ['item/commandExecution/requestApproval',
    'item/fileChange/requestApproval', 'item/permissions/requestApproval'])
def test_managed_response_cannot_expand_native_permissions(store, method):
    store.request = request({'execution_route': ROUTE})
    store.pending = {'event_id': 'pending', 'params': {'method': method}}
    with pytest.raises(ValueError, match='permission'):
        executor.command(AID, 'request', 'respond', {'id': 'approval', 'result': {'decision': 'accept'}})
    assert not store.enqueued


def test_managed_user_input_keeps_original_response_channel(store):
    store.request = request({'execution_route': ROUTE})
    store.pending = {'event_id': 'pending', 'params': {'method': 'item/tool/requestUserInput'}}
    result = executor.command(AID, 'request', 'respond', {'id': 'question', 'result': {'answer': 'yes'}})
    assert result['id'] == 'command'
    assert store.enqueued[0][1] == 'respond'


@pytest.mark.parametrize('mode,device,expected', [('legacy', OTHER, 'legacy'),
    ('codex', OTHER, 'local_device'), ('codex', DEVICE, 'inherit'), ('codex', None, 'local_device')])
def test_mode_changes_are_explicit_and_never_choose_an_unselected_personal_device(store, mode, device, expected):
    store.project = {'mode': 'legacy', 'device_id': device, 'execution_override': 'inherit'}
    assert executor.set_mode(AID, mode)['execution_override'] == expected
    assert not store.bindings


def test_restore_inheritance_does_not_allocate_or_rebind_a_device(store):
    assert executor.set_inherit(AID) == {'execution_override': 'inherit'}
    assert not store.bindings and not store.enqueued


def test_personal_revoke_endpoint_cannot_revoke_a_managed_scope_device(store):
    with pytest.raises(ValueError, match='device_managed'):
        executor.revoke(AID, DEVICE)
    assert not store.writes


@pytest.mark.parametrize('field', ['id', 'agent_id', 'tenant_id'])
def test_tool_core_requires_exact_device_project_and_tenant_even_before_business_checks(store, field):
    store.request = request()
    device = {'id': DEVICE, 'agent_id': AID, 'tenant_id': TENANT, field: OTHER}
    with pytest.raises(ValueError, match='request_unavailable'):
        executor.tool_request(store.connection, device, 'request', 'allowed')
    assert not store.writes


@pytest.mark.parametrize('source,grant,generation,scope', [
    ('personal_v1', None, None, {'execution_route': ROUTE}),
    ('managed_node', GRANT, 3, {}),
    ('managed_node', OTHER, 3, {'execution_route': ROUTE}),
    ('managed_node', GRANT, 4, {'execution_route': ROUTE}),
])
def test_event_core_rejects_other_source_or_grant_before_any_ack_or_event_write(store, source, grant, generation, scope):
    store.request = request(scope)
    with pytest.raises(ValueError, match='request_unavailable'):
        executor.device_events(store.connection, {'id': DEVICE, 'agent_id': AID, 'tenant_id': TENANT, 'epoch': 1}, 1,
            [{'id': 'event', 'request_id': 'request', 'method': 'command/ack', 'params': {'command_id': 'command'}}],
            source=source, grant_id=grant, grant_generation=generation)
    assert not store.writes


@pytest.mark.parametrize('aid', [None, AID])
def test_malformed_default_never_silently_selects_legacy_api(store, aid):
    store.nodes.policy = lambda _c: {'source': 'unknown'}
    with pytest.raises(ValueError, match='not_ready'):
        executor.enabled(aid)


def test_managed_request_uses_one_policy_observation_for_its_allocation(store):
    seen = []
    first = {'source': 'managed_node', 'node_id': OTHER, 'revision': 'execution/default@2'}
    def policy(_c):
        seen.append(True)
        return first if len(seen) == 1 else {'source': 'legacy', 'node_id': None, 'revision': 'execution/default@3'}
    store.nodes.policy = policy
    row = executor.submit(AID, 'one-policy', {'input': 'Read'}, session_key='one-policy')
    assert row['device_id'] == DEVICE
    assert len(seen) == 1 and store.bindings[0][1] is first


def test_event_helper_cannot_ack_under_a_different_device_epoch(store):
    with pytest.raises(ValueError, match='epoch_changed'):
        executor.device_events(store.connection,
            {'id': DEVICE, 'agent_id': AID, 'tenant_id': TENANT, 'epoch': 2}, 1, [])
    assert not store.writes


@pytest.mark.parametrize('source,grant,generation,expected', [
    ('personal_v1', None, None, ['personal']), ('managed_node', GRANT, 3, ['managed'])])
def test_poll_core_fences_and_returns_only_its_immutable_source(store, monkeypatch, source, grant, generation, expected):
    from lead_app import admin_settings
    monkeypatch.setattr(admin_settings, 'effective', lambda *_a: ({'tools': {'public_web': True}}, 'models/default@0'))
    store.active = [request({'control_version': 6}, id='personal'),
        request({'control_version': 6, 'execution_route': ROUTE}, id='managed')]
    store.commands = [{'id': row['id'], 'kind': 'run', 'request_id': row['id'], 'payload': {},
        'request_scope': row['scope']} for row in store.active]
    device = {'id': DEVICE, 'agent_id': AID, 'tenant_id': TENANT, 'epoch': 7, 'instance_id': 'fixed'}
    batch = executor.poll_device(store.connection, device, source=source, grant_id=grant,
        grant_generation=generation)
    assert batch['epoch'] == 7 and device['instance_id'] == 'fixed'
    assert [row['request_id'] for row in batch['commands']] == expected
    assert [row[0]['id'] for row in store.enqueued] == expected
    assert all(set(row) == {'id', 'kind', 'request_id', 'payload'} for row in batch['commands'])


def test_pure_event_transition_keeps_primary_ids_and_root_usage():
    transition = getattr(executor, 'event_transition', None)
    assert callable(transition), 'Studio and project require one recovery codec'
    row = request(thread_id='root', turn_id='turn', state='waiting_input')
    child = transition(row, 'thread/started', {'threadId': 'child', 'turnId': 'child-turn'})
    assert (child['thread_id'], child['turn_id']) == ('root', 'turn')
    assert child['ack_run'] is True and child['state'] == 'running'
    usage = transition(row, 'thread/tokenUsage/updated', {'threadId': 'root', 'tokenUsage': {'tokens': 4}})
    assert usage['usage'] == {'tokens': 4}
    assert not usage['ack_run'] and 'state' not in usage
    with pytest.raises(ValueError, match='result_conflict'):
        transition(row, 'result', {'thread_id': 'other', 'state': 'completed'})


def test_unknown_result_reconciles_only_same_original_primary_identity():
    transition = getattr(executor, 'event_transition', None)
    assert callable(transition), 'Shared event codec is required'
    row = request(thread_id='root', turn_id='turn', state='execution_unknown', result={'state': 'execution_unknown'})
    params = {'state': 'completed', 'reconciled': True, 'thread_id': 'root', 'turn_id': 'turn', 'output': 'proof'}
    result = transition(row, 'result', params)
    assert result['result'] == params and result['state'] == 'completed' and result['ack_all']
    with pytest.raises(ValueError, match='result_conflict'):
        transition(row, 'result', {**params, 'reconciled': False})


@pytest.mark.parametrize('method,params,expected', [
    ('request/started', {'thread_id': 'root', 'turn_id': 'turn'},
        {'thread_id': 'root', 'turn_id': 'turn', 'state': 'running', 'ack_run': True, 'ack_all': False}),
    ('thread/started', {'threadId': 'child'},
        {'thread_id': None, 'turn_id': None, 'state': 'running', 'ack_run': True, 'ack_all': False}),
    ('thread/tokenUsage/updated', {'threadId': 'unbound', 'tokenUsage': {'tokens': 9}},
        {'thread_id': None, 'turn_id': None, 'state': 'running', 'ack_run': True, 'ack_all': False}),
    ('command/ack', {'command_id': 'specific-command'},
        {'thread_id': None, 'turn_id': None, 'command_id': 'specific-command', 'ack_run': False, 'ack_all': False}),
    ('server/request', {'method': 'item/tool/requestUserInput'},
        {'thread_id': None, 'turn_id': None, 'state': 'waiting_input', 'ack_run': True, 'ack_all': False}),
])
def test_recovery_codec_has_the_original_initial_identity_and_ack_rules(method, params, expected):
    assert executor.event_transition(request(), method, params) == expected


@pytest.mark.parametrize('state', ['completed', 'failed', 'interrupted', 'canceled'])
def test_terminal_request_is_not_reopened_by_later_notifications(state):
    result = executor.event_transition(request(state=state, thread_id='original', turn_id='turn'),
        'server/request', {'id': 'later', 'method': 'item/tool/requestUserInput'})
    assert result == {'thread_id': 'original', 'turn_id': 'turn', 'ack_run': False, 'ack_all': False}


@pytest.mark.parametrize('params', [{'thread': 'not-an-object'}, {'turn': []}, {'threadId': 7},
    {'turn_id': True}])
def test_malformed_native_identity_is_refused_before_persistence(params):
    with pytest.raises(ValueError, match='payload_invalid'):
        executor.event_transition(request(), 'request/started', params)


@pytest.mark.parametrize('generation', [True, 0, -1, 3.0, '3'])
def test_managed_generation_snapshot_is_typed_and_positive(generation):
    with pytest.raises(ValueError, match='request_conflict'):
        executor.execution_source({'execution_route': {**ROUTE, 'grant_generation': generation}})
