"""Pre-project model routing with synthetic identity/SQL boundaries only."""
import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from lead_app import executor_nodes, executor_studio, local_executor
from lead_app import local_executor_client as client
from lead_app import local_executor_identity as identity
from lead_app.scope import tenant_scope
from pydantic import BaseModel

TENANT = '00000000-0000-4000-8000-000000000102'
GRANT = '00000000-0000-4000-8000-000000000103'
USER = {'tenant_id': TENANT, 'role': 'owner', 'issuer': 'https://identity.invalid', 'subject': 'user-one'}


class Answer(BaseModel):
    text: str


def forbidden(*_args, **_kwargs):
    raise AssertionError('Unexpected project, database, model or network boundary')


@pytest.fixture
def studio(monkeypatch):
    state = SimpleNamespace(inserted=[], bindings=[], commands=[], lookups=[], writes=[])
    connection = SimpleNamespace(execute=lambda *args: state.writes.append(args))

    def one(_connection, query, params=()):
        state.lookups.append((query, params))
        if 'INSERT INTO lead.executor_studio_requests' in query:
            keys = ('tenant_id', 'studio_id', 'grant_id', 'request_key', 'request_hash',
                    'session_key', 'actor_ref', 'actor', 'purpose', 'payload', 'scope')
            row = {'id': 'synthetic-request', **dict(zip(keys, params, strict=True))}
            state.inserted.append(row)
            return row
        if 'FROM lead.executor_studio_requests' in query:
            return None
        raise AssertionError('Unexpected synthetic read: ' + query)

    def binding(_connection, aid, policy):
        state.bindings.append((aid, policy))
        return {'device_id': None, 'execution_route': {'source': 'managed_node',
            'grant_id': GRANT, 'grant_generation': 1, 'policy_revision': 'execution/default@3'}}

    monkeypatch.setattr(executor_studio, 'tx', lambda: nullcontext(connection))
    monkeypatch.setattr(executor_studio, 'one', one)
    monkeypatch.setattr(executor_studio, 'j', copy.deepcopy)
    monkeypatch.setattr(executor_studio, 'audit', lambda *_a, **_k: None)
    monkeypatch.setattr(executor_studio, '_enqueue', lambda *args: state.commands.append(args))
    monkeypatch.setattr(identity, 'one', lambda *_a, **_k: {'role': 'owner', 'status': 'active'})
    monkeypatch.setattr(identity, 'resolve', lambda *_a, **_k:
        {'capabilities': ['agents.create'], 'agent_ids': None})
    monkeypatch.setattr(executor_nodes, 'policy', lambda _c:
        {'source': 'managed_node', 'node_id': GRANT, 'revision': 'execution/default@3'})
    monkeypatch.setattr(executor_nodes, 'project_binding', binding)
    monkeypatch.setattr(local_executor, 'default_agent', forbidden)
    monkeypatch.setattr(local_executor, 'submit', forbidden)
    monkeypatch.setattr(local_executor, 'request_by_key', forbidden)

    async def completed(aid, request_id):
        assert aid is None and request_id == 'synthetic-request'
        return {'text': 'synthetic result'}

    monkeypatch.setattr(client, 'await_request', completed)
    return state


@pytest.mark.parametrize('context', [{}, {'purpose': 'website_brief'},
    {'studio_purpose': 'other'}, {'actor': {'actor_type': 'system'}}])
async def test_studio_model_requires_explicit_supported_context(monkeypatch, context):
    monkeypatch.setattr(executor_studio, 'request_by_key', forbidden)
    handle = client.execution_context.set(context)
    try:
        with pytest.raises(client.ExecutorUnavailable, match='executor_studio_actor_unavailable'):
            await client.run_model(Answer, {}, 'instructions', purpose='website_brief')
    finally:
        client.execution_context.reset(handle)


@pytest.mark.parametrize('who', [None, {'actor_type': 'system'},
    {**USER, 'role': 'service'}, {**USER, 'role': 'viewer'}, {**USER, 'subject': None}])
async def test_explicit_studio_context_cannot_promote_absent_or_invalid_actor(studio, who):
    handle = client.execution_context.set({'studio_purpose': 'website_brief'})
    try:
        with tenant_scope(TENANT, who), pytest.raises(ValueError, match='executor_studio_actor_unavailable'):
            await client.run_model(Answer, {}, 'instructions')
    finally:
        client.execution_context.reset(handle)
    assert studio.inserted == [] and studio.bindings == []


@pytest.mark.parametrize('public_web', [True, False])
async def test_empty_studio_submits_actor_namespaced_public_only_payload(studio, public_web):
    tools = SimpleNamespace(local_executor_context={'kind': 'project', 'task_id': 'forged-task',
        'agent_id': 'forged-project', 'connection_ref': 'forged-connection'})
    context = {'studio_purpose': 'website_brief', 'key': 'client-operation', 'session_key': 'client-session'}
    handle = client.execution_context.set(context)
    try:
        with tenant_scope(TENANT, USER):
            result = await client.run_model(Answer, {'managed_tools': {'public_web': public_web}},
                'instructions', tools=tools)
            with executor_studio.actor_transaction() as (_, actor):
                expected_key = executor_studio.namespace(actor,
                    client.request_identity(Answer, {'managed_tools': {'public_web': public_web}}, None, 'primary'))
                expected_session = executor_studio.namespace(actor, 'client-session')
    finally:
        client.execution_context.reset(handle)
    assert result.text == 'synthetic result'
    row, = studio.inserted
    assert row['request_key'] == expected_key and row['session_key'] == expected_session
    assert row['studio_id'] == row['tenant_id'] == TENANT
    assert row['payload']['session_key'] == expected_session
    assert row['payload']['tool_context'] == {'kind': 'studio', 'purpose': 'website_brief'}
    assert [tool['name'] for tool in row['payload']['tools']] == (['lead_public_read'] if public_web else [])
    assert row['payload']['web_search'] == ('live' if public_web else 'disabled')
    assert studio.bindings[0][0] is None


@pytest.mark.parametrize('tool', ['lead_document_read', 'lead_task_read', 'shell'])
def test_studio_rejects_project_or_arbitrary_tools_before_allocation(studio, tool):
    with tenant_scope(TENANT, USER), pytest.raises(ValueError, match='executor_tool_not_declared'):
        executor_studio.submit('client-key', {'tools': [{'name': tool}]}, session_key='client-session')
    assert studio.inserted == [] and studio.bindings == []


async def test_same_client_keys_from_two_users_get_different_request_and_session_namespaces(studio):
    handle = client.execution_context.set({'studio_purpose': 'website_brief',
        'key': 'same-client-key', 'session_key': 'same-client-session'})
    try:
        for who in (USER, {**USER, 'subject': 'user-two'}):
            with tenant_scope(TENANT, who):
                await client.run_model(Answer, {'same': 'input'}, 'instructions')
    finally:
        client.execution_context.reset(handle)
    first, second = studio.inserted
    assert first['actor_ref'] != second['actor_ref']
    assert first['request_key'] != second['request_key']
    assert first['session_key'] != second['session_key']


async def test_managed_policy_remains_native_while_node_offline(monkeypatch):
    from lead_app import admin_settings, db, model

    connection = object()
    monkeypatch.setattr(local_executor, 'tx', lambda *_a, **_k: nullcontext(connection))
    monkeypatch.setattr(local_executor, 'one', forbidden)
    monkeypatch.setattr(local_executor, 'default_agent', forbidden)
    monkeypatch.setattr(executor_nodes, 'policy', lambda _c:
        {'source': 'managed_node', 'node_id': GRANT, 'revision': 'execution/default@3'})
    monkeypatch.setattr(executor_nodes, 'server_status', lambda *_a:
        {'ready': False, 'state': 'offline', 'configured': True})
    monkeypatch.setattr(db, 'tx', lambda: nullcontext(connection))
    monkeypatch.setattr(admin_settings, 'effective', lambda *_a:
        ({'tools': {'public_web': True}}, 'models/builtin@0'))
    monkeypatch.setattr(model, 'model_config', forbidden)
    monkeypatch.setattr(model, 'Agent', forbidden)
    monkeypatch.setattr(model, 'reserve', forbidden)

    async def offline(*args, **kwargs):
        assert kwargs['agent_id'] is None
        raise client.ExecutorUnavailable('local_executor_offline')

    monkeypatch.setattr(client, 'run_model', offline)
    assert client.uses_native() is True
    assert local_executor.configured() is False
    with pytest.raises(client.ExecutorUnavailable, match='local_executor_offline'):
        await model.run_model(Answer, {'website': 'https://product.invalid'})


async def test_preproject_api_delegates_actor_namespace_and_explicit_studio_purpose(studio, monkeypatch):
    from lead_app import api

    observed = {}

    def native(aid, *, key):
        observed.update(aid=aid, key=key)
        return True

    async def step(key, factory):
        assert key == observed['key']
        return await factory()

    async def analyze(url, aid):
        observed['context'] = copy.deepcopy(client.execution_context.get())
        assert aid is None
        return {'url': url}

    monkeypatch.setattr(client, 'uses_native', native)
    monkeypatch.setattr(client.activity_jobs, 'step', step)
    monkeypatch.setattr(api.website_brief, 'analyze', analyze)
    monkeypatch.setattr(api, 'require_agent', forbidden)
    request = SimpleNamespace(receive=forbidden)
    body = api.website_brief.Website(product_url='https://product.invalid', request_id=GRANT)
    with tenant_scope(TENANT, USER):
        result = await api.analyze_website(body, request)
        with executor_studio.actor_transaction() as (_, actor):
            expected = executor_studio.namespace(actor, 'website:' + TENANT + ':' + GRANT)
    assert result == {'url': 'https://product.invalid'}
    assert observed['aid'] is None and observed['key'] == expected
    assert observed['context'] == {'agent_id': None, 'key': expected, 'session_key': expected,
                                   'studio_purpose': 'website_brief'}
    assert client.execution_context.get() is None


async def test_preproject_api_refuses_missing_actor_before_scheduling(studio, monkeypatch):
    from lead_app import api

    monkeypatch.setattr(client, 'uses_native', forbidden)
    monkeypatch.setattr(client.activity_jobs, 'step', forbidden)
    body = api.website_brief.Website(product_url='https://product.invalid')
    with tenant_scope(TENANT, None), pytest.raises(ValueError, match='executor_studio_actor_unavailable'):
        await api.analyze_website(body, SimpleNamespace(receive=forbidden))
