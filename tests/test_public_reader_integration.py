"""An isolated caller must use its configured reader without direct fallback."""
import asyncio
import io
import json
import socket
import sys
import tempfile
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from lead_app import fetch

SOURCE = {'url': 'https://source.example/page', 'title': 'Original page',
          'text': 'An observed public source.', 'links': [], 'requires_javascript': False}
SOCKET = '/run/lead-public-reader/reader.sock'


async def forbidden_direct(*args, **kwargs):
    raise AssertionError('The isolated caller attempted a direct network read')


async def test_configured_reader_avoids_isolated_network_resolution(monkeypatch):
    async def read(path, url, *, discover_links, external_links):
        assert (path, url, discover_links, external_links) == (SOCKET, SOURCE['url'], False, True)
        return dict(SOURCE)
    monkeypatch.setenv('LEAD_PUBLIC_READER_SOCKET', SOCKET)
    monkeypatch.setitem(sys.modules, 'lead_app.public_reader', SimpleNamespace(read_via_reader=read))
    monkeypatch.setattr(fetch, 'pinned_get', forbidden_direct)
    assert await fetch.fetch_product(SOURCE['url'], external_links=True) == SOURCE


async def test_configured_reader_failure_never_falls_back_to_direct(monkeypatch):
    async def unavailable(*args, **kwargs):
        raise OSError('reader unavailable')
    monkeypatch.setenv('LEAD_PUBLIC_READER_SOCKET', SOCKET)
    monkeypatch.setitem(sys.modules, 'lead_app.public_reader', SimpleNamespace(read_via_reader=unavailable))
    monkeypatch.setattr(fetch, 'pinned_get', forbidden_direct)
    with pytest.raises(OSError, match='reader unavailable'):
        await fetch.fetch_product(SOURCE['url'])


async def test_explicit_empty_reader_configuration_is_refused(monkeypatch):
    monkeypatch.setenv('LEAD_PUBLIC_READER_SOCKET', '')
    monkeypatch.setattr(fetch, 'pinned_get', forbidden_direct)
    with pytest.raises(ValueError, match='public_reader'):
        await fetch.fetch_product(SOURCE['url'])


async def test_unconfigured_development_fetch_keeps_public_reader_contract(monkeypatch):
    monkeypatch.delenv('LEAD_PUBLIC_READER_SOCKET', raising=False)
    async def observed(client, url):
        return httpx.Response(200, headers={'content-type': 'text/html'},
            content=b'<title>Original page</title><p>An observed public source.</p>',
            request=httpx.Request('GET', url))
    monkeypatch.setattr(fetch, 'pinned_get', observed)
    result = await fetch.fetch_product(SOURCE['url'])
    assert result['url'] == SOURCE['url'] and result['title'] == SOURCE['title']
    assert SOURCE['text'] in result['text'] and not result['requires_javascript']


def native_request(monkeypatch):
    from lead_app import admin_settings
    monkeypatch.setattr(admin_settings, 'for_agent', lambda *args: ({'tools': {'public_web': True}}, None))
    return ({'id': 'device', 'agent_id': 'agent', 'tenant_id': 'tenant'},
        {'device_id': 'device', 'agent_id': 'agent', 'tenant_id': 'tenant',
         'payload': {'tool_context': {'kind': 'model'}}})


async def test_native_dns_failure_returns_limited_source_result(monkeypatch):
    from lead_app import local_executor_tools
    device, request = native_request(monkeypatch)
    async def unavailable(*args, **kwargs):
        raise socket.gaierror(-3, 'private diagnostic details must not escape')
    monkeypatch.setattr(fetch, 'fetch_product', unavailable)
    result = await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']})
    assert result['isError'] is True
    assert result['structuredContent'] == {'code': 'source_unavailable', 'reason': 'source_dns_unavailable',
        'failure_scope': 'source', 'source_ref': SOURCE['url'], 'knowledge_progress': False, 'business_progress': False}
    assert 'private diagnostic' not in str(result)


async def test_late_permission_change_is_not_hidden_by_source_error(monkeypatch):
    from lead_app import local_executor_tools
    device, request = native_request(monkeypatch)
    async def unavailable(*args, **kwargs):
        raise socket.gaierror(-3, 'DNS failed')
    @contextmanager
    def changed():
        raise ValueError('executor_scope_changed')
        yield
    monkeypatch.setattr(fetch, 'fetch_product', unavailable)
    with pytest.raises(ValueError, match='executor_scope_changed'):
        await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']}, revalidate=changed)


@pytest.mark.parametrize('status', [403, 404, 429, 503])
async def test_native_http_failure_is_limited_to_observed_source(monkeypatch, status):
    from lead_app import local_executor_tools
    device, request = native_request(monkeypatch)
    async def unavailable(*args, **kwargs):
        response = httpx.Response(status, request=httpx.Request('GET', SOURCE['url']))
        response.raise_for_status()
    monkeypatch.setattr(fetch, 'fetch_product', unavailable)
    result = await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']})
    assert result['isError'] and result['structuredContent']['failure_scope'] == 'source'
    assert result['structuredContent']['http_status'] == status
    assert result['structuredContent']['reason'] == 'source_http_error'


@pytest.mark.parametrize('error', [PermissionError('denied'), ValueError('private destination')])
async def test_native_policy_failure_is_never_reclassified_as_transport(monkeypatch, error):
    from lead_app import local_executor_tools
    device, request = native_request(monkeypatch)
    async def denied(*args, **kwargs):
        raise error
    monkeypatch.setattr(fetch, 'fetch_product', denied)
    with pytest.raises(type(error), match=str(error)):
        await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']})


@pytest.mark.parametrize('flag,read_fails', [('public_web', False), ('community_research', False),
    ('advanced_research', False), ('public_web', True)])
async def test_revoked_research_setting_is_checked_after_network(monkeypatch, flag, read_fails):
    from lead_app import admin_settings, local_executor_tools
    device, request = native_request(monkeypatch)
    settings = {'tools': {'public_web': True, 'community_research': True, 'advanced_research': True}}
    monkeypatch.setattr(admin_settings, 'for_agent', lambda *args: (settings, None))
    context = request['payload']['tool_context']
    if flag == 'community_research':
        context['mode'] = 'community_discovery'
    if flag == 'advanced_research':
        context['requires_advanced_research'] = True
    async def observed(*args, **kwargs):
        settings['tools'][flag] = False
        if read_fails:
            raise socket.gaierror(-3, 'diagnostic')
        return dict(SOURCE)
    monkeypatch.setattr(fetch, 'fetch_product', observed)
    with pytest.raises(ValueError, match='executor_public_research_disabled'):
        await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']})


@pytest.mark.parametrize('read_fails', [False, True])
async def test_scope_barrier_follows_final_async_settings_read(monkeypatch, read_fails):
    from lead_app import admin_settings, local_executor_tools
    device, request = native_request(monkeypatch)
    state = {'reads': 0, 'live': True}
    def settings(*args):
        state['reads'] += 1
        if state['reads'] == 2:
            state['live'] = False
        return {'tools': {'public_web': True}}, None
    async def observed(*args, **kwargs):
        if read_fails:
            raise socket.gaierror(-3, 'diagnostic')
        return dict(SOURCE)
    @contextmanager
    def revalidate():
        if not state['live']:
            raise ValueError('executor_scope_changed')
        yield
    monkeypatch.setattr(admin_settings, 'for_agent', settings)
    monkeypatch.setattr(fetch, 'fetch_product', observed)
    with pytest.raises(ValueError, match='executor_scope_changed'):
        await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']}, revalidate=revalidate)


def studio_request(monkeypatch):
    from lead_app import admin_settings, executor_studio
    @contextmanager
    def transaction():
        yield object()
    monkeypatch.setattr(executor_studio, 'tx', transaction)
    monkeypatch.setattr(executor_studio, 'require_studio_actor', lambda *args: None)
    monkeypatch.setattr(admin_settings, 'effective', lambda *args: ({'tools': {'public_web': True}}, None))
    return executor_studio, {'actor': {'actor_ref': 'test-only'}}


async def test_studio_dns_failure_uses_same_limited_result(monkeypatch):
    studio, request = studio_request(monkeypatch)
    async def unavailable(*args, **kwargs):
        raise socket.gaierror(-3, 'diagnostic')
    monkeypatch.setattr(fetch, 'fetch_product', unavailable)
    result = await studio.call_public_tool(request, 'lead_public_read', {'url': SOURCE['url']})
    assert result['isError'] and result['structuredContent']['reason'] == 'source_dns_unavailable'


@pytest.mark.parametrize('read_fails', [False, True])
async def test_studio_revalidates_original_scope_after_network(monkeypatch, read_fails):
    studio, request = studio_request(monkeypatch)
    async def observed(*args, **kwargs):
        if read_fails:
            raise socket.gaierror(-3, 'diagnostic')
        return dict(SOURCE)
    @contextmanager
    def changed():
        raise ValueError('executor_scope_changed')
        yield
    monkeypatch.setattr(fetch, 'fetch_product', observed)
    with pytest.raises(ValueError, match='executor_scope_changed'):
        await studio.call_public_tool(request, 'lead_public_read', {'url': SOURCE['url']}, revalidate=changed)


@pytest.mark.parametrize('studio_mode', [False, True])
async def test_typed_reader_policy_is_a_finite_nonretryable_result(monkeypatch, studio_mode):
    from lead_app import local_executor_tools, public_reader
    async def denied(*args, **kwargs):
        raise public_reader.PublicReaderPolicyError()
    monkeypatch.setattr(fetch, 'fetch_product', denied)
    if studio_mode:
        studio, request = studio_request(monkeypatch)
        result = await studio.call_public_tool(request, 'lead_public_read', {'url': SOURCE['url']})
    else:
        device, request = native_request(monkeypatch)
        result = await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']})
    assert result['isError'] is True
    assert result['structuredContent'] == {
        'code': 'source_policy_denied', 'category': 'policy', 'failure_scope': 'source',
        'retryable': False, 'knowledge_progress': False, 'business_progress': False}
    assert SOURCE['url'] not in str(result)


@pytest.mark.parametrize('studio_mode', [False, True])
@pytest.mark.parametrize('revocation', ['scope', 'settings'])
async def test_policy_result_cannot_hide_late_authority_revocation(monkeypatch, revocation, studio_mode):
    from lead_app import admin_settings, local_executor_tools, public_reader
    device, request = native_request(monkeypatch)
    settings = {'tools': {'public_web': True}}
    if studio_mode:
        studio, request = studio_request(monkeypatch)
        monkeypatch.setattr(admin_settings, 'effective', lambda *args: (settings, None))
    monkeypatch.setattr(admin_settings, 'for_agent', lambda *args: (settings, None))
    async def denied(*args, **kwargs):
        if revocation == 'settings':
            settings['tools']['public_web'] = False
        raise public_reader.PublicReaderPolicyError()
    @contextmanager
    def revalidate():
        if revocation == 'scope':
            raise ValueError('executor_scope_changed')
        yield
    monkeypatch.setattr(fetch, 'fetch_product', denied)
    expected = 'executor_scope_changed' if revocation == 'scope' else 'executor_public_research_disabled'
    with pytest.raises(ValueError, match=expected):
        if studio_mode:
            await studio.call_public_tool(request, 'lead_public_read', {'url': SOURCE['url']}, revalidate=revalidate)
        else:
            await local_executor_tools.call(device, request, 'lead_public_read', {'url': SOURCE['url']}, revalidate=revalidate)


@pytest.mark.parametrize('message', ['executor_public_research_disabled', 'executor_tool_scope_changed'])
def test_native_authority_error_stays_forbidden_in_api(message):
    from lead_app.executor_nodes_api import _error
    assert _error(ValueError(message)).status_code == 403
    assert _error(ValueError(message)).detail == 'executor_scope_forbidden'


def through_broker(monkeypatch, response):
    from test_codex_node_broker import broker
    node_http = broker.NodeHTTP({'base_url': 'https://example.invalid', 'node_id': str(uuid4()),
        'node_token': 'SYNTHETIC_NODE_CREDENTIAL_0123456789', 'node_epoch': 1,
        'instance_id': str(uuid4()), 'credential_generation': 1})
    def receive(request, timeout):
        if response.status_code >= 400:
            raise urllib.error.HTTPError(request.full_url, response.status_code, 'fixture', {}, io.BytesIO(response.content))
        return io.BytesIO(response.content)
    monkeypatch.setattr(node_http.opener, 'open', receive)
    path = '/executor/v2/scopes/' + str(uuid4()) + '/tools'
    return node_http(path, {})


@pytest.fixture
def native_api(monkeypatch):
    from fastapi import FastAPI
    from lead_app import executor_nodes_api as api
    from lead_app import local_executor_identity
    device, request = native_request(monkeypatch)
    node = {'id': str(uuid4())}
    grant = {'id': str(uuid4()), 'generation': 1, 'scope_kind': 'project',
             'tenant_id': device['tenant_id'], 'agent_id': device['agent_id']}
    @contextmanager
    def authorized(*args, **kwargs):
        yield object(), node, grant
    monkeypatch.setattr(api.nodes, 'authorized_scope', authorized)
    monkeypatch.setattr(local_executor_identity, 'service_device', lambda *args: device)
    monkeypatch.setattr(api.executor, 'tool_request', lambda *args, **kwargs: request)
    monkeypatch.setattr(api.executor, 'require_agent', lambda *args: None)
    app = FastAPI()
    app.include_router(api.router)
    return app


@pytest.mark.parametrize('reader_error,code,scope,extra', [
    ({'code': 'source_policy_denied', 'category': 'policy'}, 'source_policy_denied', 'source',
        {'category': 'policy', 'retryable': False}),
    ({'code': 'source_dns_unavailable', 'category': 'transport'}, 'source_unavailable', 'source',
        {'reason': 'source_dns_unavailable', 'source_ref': SOURCE['url']}),
    ({'code': 'reader_busy', 'category': 'transport'}, 'source_unavailable', 'platform',
        {'reason': 'reader_busy', 'source_ref': SOURCE['url']}),
    ({'code': 'source_http_error', 'category': 'http', 'status': 403}, 'source_unavailable', 'source',
        {'reason': 'source_http_error', 'source_ref': SOURCE['url'], 'http_status': 403}),
    ({'code': 'PRIVATE_NODE_MARKER', 'category': 'policy'}, 'source_unavailable', 'platform',
        {'reason': 'reader_invalid_response', 'source_ref': SOURCE['url']}),
])
async def test_synthetic_reader_to_native_api_preserves_finite_result(monkeypatch, native_api,
        reader_error, code, scope, extra):
    from lead_app import public_reader
    async def serve(reader, writer):
        await reader.readline()
        writer.write(json.dumps({'v': 1, 'ok': False, 'error': reader_error}).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    # Parent ownership is covered by the protocol suite. This fixture substitutes
    # only the root-owned socket location; decoding and native/API handlers run.
    with tempfile.TemporaryDirectory(prefix='lead-err-', dir='/tmp') as directory:
        path = str(Path(directory).resolve() / 'r.sock')
        monkeypatch.setattr(public_reader, 'PUBLIC_READER_SOCKET', path)
        monkeypatch.setattr(public_reader, '_trusted_socket', lambda value: None)
        monkeypatch.setenv('LEAD_PUBLIC_READER_SOCKET', path)
        server = await asyncio.start_unix_server(serve, path=path)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=native_api), base_url='http://fixture') as client:
                response = await client.post('/executor/v2/scopes/' + str(uuid4()) + '/tools',
                    headers={'Authorization': 'Bearer fixture-only'}, json={
                        'node_epoch': 1, 'grant_generation': 1, 'epoch': 1,
                        'request_id': str(uuid4()), 'name': 'lead_public_read',
                        'arguments': {'url': SOURCE['url']}})
        finally:
            server.close()
            await server.wait_closed()
    assert response.status_code == 200
    result = response.json()
    assert result['isError'] is True
    assert result['structuredContent'] == {'code': code, 'failure_scope': scope,
        'knowledge_progress': False, 'business_progress': False, **extra}
    assert json.loads(result['content'][0]['text']) == result['structuredContent']
    assert 'PRIVATE_NODE_MARKER' not in response.text
    assert through_broker(monkeypatch, response) == result


@pytest.mark.parametrize('error,status,detail', [
    (ValueError('PRIVATE_NODE_MARKER'), 422, 'executor_payload_invalid'),
    (ValueError('source_policy_denied'), 422, 'executor_payload_invalid'),
    (ValueError('executor_public_research_disabled'), 403, 'executor_scope_forbidden'),
    (PermissionError('PRIVATE_NODE_MARKER'), 403, 'executor_scope_forbidden'),
])
async def test_native_api_does_not_turn_ordinary_or_permission_errors_into_source_results(
        monkeypatch, native_api, error, status, detail):
    async def failed(*args, **kwargs):
        raise error
    monkeypatch.setattr(fetch, 'fetch_product', failed)
    transport = httpx.ASGITransport(app=native_api, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url='http://fixture') as client:
        response = await client.post('/executor/v2/scopes/' + str(uuid4()) + '/tools',
            headers={'Authorization': 'Bearer fixture-only'}, json={
                'node_epoch': 1, 'grant_generation': 1, 'epoch': 1,
                'request_id': str(uuid4()), 'name': 'lead_public_read', 'arguments': {'url': SOURCE['url']}})
    assert response.status_code == status
    if detail:
        assert response.json() == {'detail': detail}
    assert 'PRIVATE_NODE_MARKER' not in response.text
    from test_codex_node_broker import broker
    with pytest.raises(broker.BrokerError) as rejected:
        through_broker(monkeypatch, response)
    assert rejected.value.code == detail
    assert rejected.value.retryable is False
