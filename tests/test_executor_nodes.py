"""Shared compute carries a machine grant, never a borrowed human/device token."""
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from lead_app import admin_config, domain
from lead_app import executor_nodes as nodes
from lead_app import executor_studio as studio
from lead_app import local_executor as core
from lead_app.config import database_kwargs
from lead_app.db import all_rows, one, tx
from lead_app.executor_nodes_api import (
    ScopeEvents,
    ScopePoll,
    _scope_events,
    _scope_poll,
    router,
)
from lead_app.scope import current_tenant, service_scope, tenant_scope

CAPS = {'ready': True, 'auth_type': 'chatgpt', 'models': ['gpt-6.1-sol'], 'reasoning_efforts': ['medium']}
PAYLOAD = {'input': 'Synthetic public research', 'instructions': 'Observe only', 'tools': []}


def new_user(tenant=None, role='owner'):
    tenant = tenant or str(uuid4())
    who = {'tenant_id': tenant, 'issuer': 'https://synthetic.example/' + uuid4().hex, 'subject': uuid4().hex,
           'role': role, 'agent_ids': None, 'capabilities': ['agents.create', 'agents.control', 'agents.chat']}
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute('INSERT INTO lead.tenants(id,name) VALUES(%s,%s) ON CONFLICT DO NOTHING', (tenant, 'Synthetic workspace'))
        c.execute("INSERT INTO lead.memberships(tenant_id,oidc_issuer,oidc_subject,role,status) VALUES(%s,%s,%s,%s,'active')",
                  (tenant, who['issuer'], who['subject'], role))
    return who


@pytest.fixture
def owner():
    who = new_user()
    with tenant_scope(who['tenant_id'], who):
        yield who


def publish(source, node_id=None):
    with tx() as c:
        old = admin_config.read(c, 'execution/default')
        return admin_config.write(c, 'execution/default', {'source': source, 'node_id': node_id}, old['version'])


def connected(*, tenants=None):
    if tenants:
        deploy = {'tenant_id': current_tenant(), 'issuer': 'service:deployment', 'subject': 'synthetic-deployer',
                  'role': 'service', 'actor_type': 'service', 'capabilities': ['executor.manage', 'executor.enroll'], 'agent_ids': []}
        with tenant_scope(current_tenant(), deploy), service_scope():
            invitation = nodes.create_enrollment(allowed_tenants=tenants, deployment_ref='synthetic-deployment')
    else:
        invitation = nodes.create_enrollment()
    node = nodes.register(invitation['enrollment_code'], 'node-instance', 'server', 'linux', CAPS)
    publish('managed_node', node['node_id'])
    return node, invitation


def catalog(node):
    return nodes.poll_node(node['node_token'], node['node_epoch'], 'node-instance', CAPS)['scopes']


def scope_for(node, aid=None):
    return next(item for item in catalog(node) if item['scope_kind'] == ('project' if aid else 'studio')
                and (not aid or item['agent_id'] == str(aid)) and item['tenant_id'] == current_tenant())


def fence(node, scope, cls=ScopePoll, **values):
    return cls(node_epoch=node['node_epoch'], epoch=scope['epoch'], grant_generation=scope['grant_generation'],
               instance_id='scope-instance', **values)


def poll(node, scope):
    return _scope_poll(node['node_token'], scope['scope_ref'], fence(node, scope))


def event(node, scope, request, method, params, event_id=None):
    body = fence(node, scope, ScopeEvents, events=[{'id': event_id or str(uuid4()), 'request_id': str(request['id']),
                                                  'method': method, 'params': params}])
    return _scope_events(node['node_token'], scope['scope_ref'], body)


def test_default_is_inactive_and_enrollment_is_single_use_hashed(owner):
    assert not core.enabled(None)
    assert nodes.server_status()['state'] == 'not_configured'
    node, invitation = connected()
    with pytest.raises(ValueError, match='executor_node_unavailable'):
        nodes.register(invitation['enrollment_code'], 'other', 'other', 'linux', CAPS)
    with nodes.node_transaction(node['node_id']) as c:
        row = one(c, 'SELECT * FROM lead.executor_nodes')
        assert row['credential_hash'] != node['node_token'] and node['node_token'] not in str(row)
        assert row['enrollment_hash'] is None
        assert one(c, 'SELECT current_setting(\'app.tenant_id\') t,current_setting(\'app.allowed_agent_ids\') a') == {'t': '', 'a': '[]'}
        assert all_rows(c, 'SELECT * FROM lead.agents') == []
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE lead.executor_nodes SET allowed_tenants=ARRAY[%s]::uuid[]", (str(uuid4()),))


def test_new_project_inherits_without_pair_and_v1_cannot_claim_it(owner):
    node, _ = connected()
    aid = domain.create_agent(runtime_engine='growth')
    assert core.enabled(aid) and core.configured(aid)
    request = core.submit(aid, 'new-agent', PAYLOAD, session_key='new-agent')
    scope = scope_for(node, aid)
    assert str(request['device_id']) == scope['device_id']
    assert request['scope']['execution_route']['source'] == 'managed_node'
    assert [c['request_id'] for c in poll(node, scope)['commands']] == [str(request['id'])]
    with tx(aid) as c:
        device = one(c, 'SELECT * FROM lead.local_executor_devices WHERE id=%s', (scope['device_id'],))
        assert device['token_hash'] is None
        assert core.poll_device(c, device)['commands'] == []
    assert core.status(aid)['devices'] == []
    with pytest.raises(ValueError, match='executor_device_managed'):
        core.revoke(aid, scope['device_id'])


def test_scopes_fence_different_project_studio_and_epoch(owner):
    node, _ = connected()
    a, b = [domain.create_agent(runtime_engine='growth') for _ in range(2)]
    request = core.submit(a, 'only-a', PAYLOAD, session_key='a')
    sa, sb, ss = scope_for(node, a), scope_for(node, b), scope_for(node)
    for foreign in (sb, ss):
        with pytest.raises((ValueError, LookupError), match='executor_request_unavailable|executor_scope_forbidden'):
            event(node, foreign, request, 'request/started', {'thread_id': 'foreign'})
    with pytest.raises(ValueError, match='executor_epoch_changed'):
        _scope_poll(node['node_token'], sa['scope_ref'], fence(node, {**sa, 'epoch': sa['epoch']+1}))
    with pytest.raises(ValueError, match='executor_scope_generation_changed'):
        _scope_poll(node['node_token'], sa['scope_ref'], fence(node, {**sa, 'grant_generation': sa['grant_generation']+1}))
    with nodes.authorized_scope(node['node_token'], sa['scope_ref'], 1, 1, 1) as (c, _, _):
        assert all_rows(c, 'SELECT * FROM lead.executor_studio_requests') == []
        assert all_rows(c, 'SELECT * FROM lead.local_executor_requests WHERE agent_id=%s', (b,)) == []


def test_instance_poll_never_steals_generation_and_scope_descriptor_is_stable(owner):
    node, _ = connected()
    aid = domain.create_agent(runtime_engine='growth')
    scope = scope_for(node, aid)
    assert poll(node, scope)['epoch'] == 1
    assert poll(node, scope)['epoch'] == 1
    with pytest.raises(ValueError, match='executor_node_epoch_changed'):
        nodes.poll_node(node['node_token'], 1, 'different-node', CAPS)
    with pytest.raises(ValueError, match='executor_epoch_changed'):
        _scope_poll(node['node_token'], scope['scope_ref'], ScopePoll(node_epoch=1, grant_generation=1, epoch=1, instance_id='different-scope'))
    publish('managed_node', node['node_id'])
    assert scope_for(node, aid) == scope


@pytest.mark.parametrize('caps', [{**CAPS, 'ready': False}, {**CAPS, 'auth_type': 'api_key'}])
def test_signed_out_compute_waits_and_keeps_controls_without_api_fallback(owner, caps):
    node, _ = connected()
    aid = domain.create_agent(runtime_engine='growth')
    request = core.submit(aid, 'waiting', PAYLOAD, session_key='waiting')
    scope = scope_for(node, aid)
    nodes.poll_node(node['node_token'], 1, 'node-instance', caps)
    assert core.enabled(aid) and not core.configured(aid)
    assert poll(node, scope)['commands'] == []
    user_interrupt = core.command(aid, request['id'], 'interrupt', {}, str(uuid4()))
    controls = poll(node, scope)['commands']
    assert {c['kind'] for c in controls} == {'interrupt'}
    assert user_interrupt['id'] in {c['id'] for c in controls}


def test_revocation_disables_scope_and_node_without_rebinding(owner):
    node, _ = connected()
    aid = domain.create_agent(runtime_engine='growth')
    request = core.submit(aid, 'original', PAYLOAD, session_key='original')
    scope = scope_for(node, aid)
    nodes.revoke_grant(scope['scope_ref'])
    with pytest.raises(ValueError, match='executor_scope_forbidden'):
        poll(node, scope)
    assert core.request_status(aid, request['id'])['device_id'] == request['device_id']
    nodes.control_node(node['node_id'], 'revoke')
    with pytest.raises(ValueError, match='executor_node_unavailable'):
        catalog(node)
    assert core.enabled(aid) and nodes.server_status()['state'] == 'revoked'


def test_cross_tenant_node_requires_explicit_deployment_allowlist(owner):
    other = new_user()
    with pytest.raises(ValueError, match='executor_scope_forbidden'):
        nodes.create_enrollment(allowed_tenants=[owner['tenant_id'], other['tenant_id']])
    node, _ = connected(tenants=[owner['tenant_id'], other['tenant_id']])
    a = domain.create_agent(runtime_engine='growth')
    first = core.submit(a, 'same', PAYLOAD, session_key='same')
    sa = scope_for(node, a)
    with tenant_scope(other['tenant_id'], other):
        publish('managed_node', node['node_id'])
        b = domain.create_agent(runtime_engine='growth')
        core.submit(b, 'same', PAYLOAD, session_key='same')
        sb = scope_for(node, b)
        with pytest.raises((ValueError, LookupError)):
            event(node, sb, first, 'request/started', {'thread_id': 'other'})
    assert sa['tenant_id'] != sb['tenant_id']
    stranger = new_user()
    with tenant_scope(stranger['tenant_id'], stranger):
        publish('managed_node', node['node_id'])
        c = domain.create_agent(runtime_engine='growth')
        with pytest.raises(ValueError, match='executor_not_ready|executor_scope_forbidden'):
            core.submit(c, 'not-allowed', PAYLOAD, session_key='not-allowed')
        assert nodes.status()['node'] is None


def test_old_null_personal_request_is_not_moved_to_managed_scope(owner):
    aid = domain.create_agent(runtime_engine='growth')
    with tx(aid) as c:
        c.execute("INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode) VALUES(lead.ctx_tenant(),%s,'codex')", (aid,))
    old = core.submit(aid, 'old', PAYLOAD, session_key='old')
    assert old['device_id'] is None
    node, _ = connected()
    scope = scope_for(node, aid)
    assert poll(node, scope)['commands'] == []
    assert core.request_status(aid, old['id'])['device_id'] is None
    fresh = core.submit(aid, 'fresh', PAYLOAD, session_key='fresh')
    assert str(fresh['device_id']) == scope['device_id']
    publish('legacy')
    assert core.request_by_key(aid, 'fresh')['device_id'] == fresh['device_id']


def test_studio_works_without_agents_and_users_cannot_reuse_each_others_thread(owner):
    node, _ = connected()
    first = studio.submit('same-key', PAYLOAD, session_key='same-session')
    assert studio.submit('same-key', PAYLOAD, session_key='same-session')['id'] == first['id']
    scope = scope_for(node)
    assert scope['studio_id'] == owner['tenant_id'] and 'agent_id' not in scope and 'device_id' not in scope
    assert poll(node, scope)['commands'][0]['request_id'] == str(first['id'])
    other = new_user(owner['tenant_id'])
    with tenant_scope(other['tenant_id'], other):
        assert studio.request_by_key('same-key') is None
        with pytest.raises(ValueError, match='executor_scope_forbidden'):
            studio.request_status(first['id'])
        second = studio.submit('same-key', PAYLOAD, session_key='same-session')
        assert (first['id'], first['request_key'], first['session_key']) != (second['id'], second['request_key'], second['session_key'])
    with nodes.authorized_scope(node['node_token'], scope['scope_ref'], 1, 1, 1) as (c, _, _):
        assert all_rows(c, 'SELECT * FROM lead.local_executor_requests') == []
        with pytest.raises(psycopg.errors.RaiseException, match='executor_scope_forbidden'):
            c.execute('INSERT INTO lead.executor_studio_requests SELECT * FROM lead.executor_studio_requests LIMIT 1')


def test_studio_actor_revocation_fences_new_run_but_admits_original_receipt(owner):
    node, _ = connected()
    request = studio.submit('actor', PAYLOAD, session_key='actor')
    scope = scope_for(node)
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute("UPDATE lead.memberships SET status='revoked' WHERE tenant_id=%s", (owner['tenant_id'],))
    assert [c['kind'] for c in poll(node, scope)['commands']] == ['interrupt']
    event(node, scope, request, 'result', {'state': 'completed', 'output': {'receipt': 'original'}, 'thread_id': 'original'})
    with nodes.authorized_scope(node['node_token'], scope['scope_ref'], 1, 1, 1) as (c, _, grant):
        with pytest.raises(ValueError, match='executor_scope_changed|executor_studio_actor_unavailable'):
            studio.tool_request(c, grant, request['id'], 'lead_public_read')
    with pytest.raises(ValueError, match='executor_studio_actor_unavailable'):
        studio.submit('new', PAYLOAD, session_key='new')


@pytest.mark.parametrize('method', [None, 'item/commandExecution/requestApproval', 'item/fileChange/requestApproval', 'item/permissions/requestApproval'])
def test_studio_cannot_escalate_permissions_through_respond(owner, method):
    node, _ = connected()
    request = studio.submit('approval', PAYLOAD, session_key='approval')
    scope = scope_for(node)
    event(node, scope, request, 'server/request', {'id': 7, **({'method': method} if method else {})})
    with pytest.raises(ValueError, match='executor_permission_escalation_forbidden'):
        studio.command(request['id'], 'respond', {'id': 7, 'result': {'decision': 'accept'}}, str(uuid4()))


def test_studio_declared_user_input_still_responds_on_original_request(owner):
    node, _ = connected()
    request = studio.submit('input', PAYLOAD, session_key='input')
    scope = scope_for(node)
    event(node, scope, request, 'server/request', {'id': 8, 'method': 'item/tool/requestUserInput'})
    answer = studio.command(request['id'], 'respond', {'id': 8, 'result': {'answers': {}}}, str(uuid4()))
    assert answer['request_id'] == str(request['id'])
    assert any(c['kind'] == 'respond' and c['payload']['id'] == 8 for c in poll(node, scope)['commands'])


def test_studio_result_thread_fencing_and_durable_replay(owner):
    node, _ = connected()
    request = studio.submit('result', PAYLOAD, session_key='result')
    scope = scope_for(node)
    event(node, scope, request, 'request/started', {'thread_id': 'root', 'turn_id': 'turn'})
    event(node, scope, request, 'thread/tokenUsage/updated', {'threadId': 'child', 'tokenUsage': {'total': {'totalTokens': 99}}})
    assert not studio.request_status(request['id'])['usage']
    with pytest.raises(ValueError, match='executor_result_conflict'):
        event(node, scope, request, 'result', {'state': 'completed', 'thread_id': 'other', 'turn_id': 'turn'})
    result = {'state': 'completed', 'thread_id': 'root', 'turn_id': 'turn', 'output': {'done': True}}
    event(node, scope, request, 'result', result, 'stable-event')
    event(node, scope, request, 'result', result, 'stable-event')
    with pytest.raises(ValueError, match='executor_event_conflict'):
        event(node, scope, request, 'result', {**result, 'output': {'changed': True}}, 'stable-event')
    assert poll(node, scope)['commands'] == []
    assert studio.request_status(request['id'])['output'] == {'done': True}


def test_http_control_requires_owner_and_studio_never_accepts_client_actor(owner):
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    assert client.get('/api/executor/server/status').status_code == 200
    for identity in (None, {**owner, 'role': 'viewer'}, {**owner, 'role': 'operator'}):
        with tenant_scope(owner['tenant_id'], identity):
            assert client.post('/api/executor/server/enrollment', json={}).status_code in {401, 403}
    with tenant_scope(owner['tenant_id'], None), service_scope():
        with pytest.raises(ValueError, match='executor_studio_actor_unavailable'):
            studio.submit('fake-system', PAYLOAD, session_key='fake')
    assert client.post('/executor/v2/nodes/register', json={'actor': {'role': 'owner'}}).status_code == 422


def test_node_audit_is_real_service_not_oidc_or_fake_owner(owner):
    node, _ = connected()
    with tx() as c:
        row = one(c, "SELECT actor_type,actor_ref FROM lead.audit_events WHERE type='executor.node_registered'")
        assert row == {'actor_type': 'service', 'actor_ref': 'executor-node:' + node['node_id']}


def test_original_composite_fk_and_immutable_source_survive_new_grants(owner):
    node, _ = connected()
    a, b = [domain.create_agent(runtime_engine='growth') for _ in range(2)]
    request = core.submit(a, 'immutable', PAYLOAD, session_key='immutable')
    foreign = scope_for(node, b)
    with tx(a) as c, pytest.raises(psycopg.errors.RaiseException, match='executor_request_conflict'):
        c.execute("UPDATE lead.local_executor_requests SET scope=jsonb_set(scope,'{execution_route,source}','\"personal_v1\"') WHERE id=%s", (request['id'],))
    with tx(a) as c, pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute("INSERT INTO lead.local_executor_projects(tenant_id,agent_id,mode,device_id) VALUES(lead.ctx_tenant(),%s,'codex',%s)",
                  (a, foreign['device_id']))


def test_provision_repeat_does_not_restore_machine_control_writes(owner):
    from lead_app.provision import provision_security
    node, _ = connected()
    scope = scope_for(node)
    with psycopg.connect(**database_kwargs(True)) as c:
        provision_security(c)
    with nodes.node_transaction(node['node_id']) as c, pytest.raises(psycopg.errors.InsufficientPrivilege):
        c.execute("UPDATE lead.executor_nodes SET credential_hash='fake' WHERE id=%s", (node['node_id'],))
    with nodes.scope_transaction(node['node_id'], current_tenant(), grant_id=scope['scope_ref'], studio=True) as c:
        assert one(c, "SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user") == {'rolsuper': False, 'rolbypassrls': False}
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE lead.executor_node_grants SET tenant_id=%s WHERE id=%s", (str(uuid4()), scope['scope_ref']))


def test_real_v2_http_transport_rejects_spoofed_scope_and_undeclared_tool(owner):
    node, _ = connected()
    aid = domain.create_agent(runtime_engine='growth')
    request = core.submit(aid, 'http', PAYLOAD, session_key='http')
    scope = scope_for(node, aid)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ' + node['node_token']}
    body = fence(node, scope).model_dump(mode='json')
    path = '/executor/v2/scopes/' + scope['scope_ref']
    assert client.post(path + '/poll', json=body).status_code == 401
    reply = client.post(path + '/poll', json=body, headers=headers)
    assert reply.status_code == 200 and reply.json()['commands'][0]['request_id'] == str(request['id'])
    assert client.post(path + '/poll', json={**body, 'tenant_id': str(uuid4())}, headers=headers).status_code == 422
    stale = client.post(path + '/poll', json={**body, 'node_epoch': 2}, headers=headers)
    assert stale.status_code == 409 and stale.json()['detail'] == 'executor_node_epoch_changed'
    denied = client.post(path + '/tools', headers=headers, json={**body, 'request_id': str(request['id']),
                                                               'name': 'lead_public_read', 'arguments': {'url': 'https://invalid.example/'}})
    assert denied.status_code == 403 and denied.json()['detail'] == 'executor_scope_forbidden'
