"""An isolated caller must use its configured reader without direct fallback."""
import socket
import sys
from contextlib import contextmanager
from types import SimpleNamespace

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
