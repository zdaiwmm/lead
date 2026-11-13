"""Explicit SDK routes stay on UDS/TLS and retain the original billing hooks."""
import ast
import asyncio
import json
import socket
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
AID = '00000000-0000-0000-0000-000000000011'


def payload(path):
    return {'agent_id': AID, 'model_socket': path,
            'model': {'provider': 'openai', 'base_url': 'https://api.openai.com/v1', 'key': 'synthetic-only', 'model': 'synthetic-only'}}


def model_client(data, meter):
    # Execute the actual factory without importing browser-use into the API
    # interpreter. On the old source, execute its real provider-client expression.
    tree = ast.parse((ROOT / 'scripts/browser_use_runner.py').read_text())
    function = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == 'provider_http_client'), None)
    namespace = {'httpx': httpx, 'data': data, 'model_timeout': 1, 'meter': meter}
    if function:
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'actual-sdk-model-client', 'exec'), namespace)
        return namespace['provider_http_client'](data, 1, meter)
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main')
    expression = next(item.context_expr for node in ast.walk(main) if isinstance(node, ast.AsyncWith)
                      for item in node.items if isinstance(item.optional_vars, ast.Name) and item.optional_vars.id == 'provider_http')
    return eval(compile(ast.Expression(expression), 'original-sdk-model-client', 'eval'), namespace)


def meter():
    from lead_app.native_model_meter import ModelCallMeter
    return ModelCallMeter(max_requests=8, max_total_tokens=40000, max_output_tokens=1000)


def test_route_validator_is_stdlib_only_and_preserves_scoped_selection():
    program = "import sys;sys.path.insert(0," + repr(str(ROOT / 'services/api')) + ");import lead_app.model_transport;assert 'httpx2' not in sys.modules"
    checked = subprocess.run([sys.executable, '-IB', '-c', program], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr
    from lead_app import model_transport
    assert hasattr(model_transport, 'validated_socket_path')
    validate = model_transport.validated_socket_path
    config = payload(None)['model']
    values = {f'LEAD_MODEL_SOCKET_{UUID(AID).hex.upper()}_OPENAI': '/synthetic/route.sock'}
    assert validate(AID, config, values) == '/synthetic/route.sock'
    assert validate('00000000-0000-0000-0000-000000000012', config, values) is None
    assert validate(AID, dict(config, provider='deepseek'), values) is None
    assert validate(AID, config, {}) is None


@pytest.mark.parametrize('path', ['', 'relative.sock', '/tmp/../route.sock', '/tmp/bad\0.sock', False])
def test_invalid_explicit_sdk_route_does_not_construct_default_transport(path):
    with pytest.raises(ValueError):
        model_client(payload(path), meter())


def test_missing_route_selection_is_not_silently_treated_as_unconfigured():
    data = payload(None)
    data.pop('model_socket')
    with pytest.raises((ValueError, KeyError)):
        model_client(data, meter())


@pytest.mark.parametrize('url', ['http://api.openai.com/v1', 'https://other.fixture/v1',
    'https://api.openai.com:8443/v1', 'https://api.openai.com/v1?token=synthetic'])
def test_socket_cannot_change_the_official_tls_destination(url):
    data = payload('/synthetic/route.sock')
    data['model']['base_url'] = url
    with pytest.raises(ValueError):
        model_client(data, meter())


@pytest.mark.asyncio
async def test_missing_uds_has_no_dns_or_proxy_fallback_and_unknown_usage_blocks_next_request(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    dns = []
    def no_dns(*args, **kwargs):
        dns.append(True)
        raise AssertionError('A dedicated route tried direct DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    usage = meter()
    async with model_client(payload('/tmp/lead-nonexistent-synthetic-model.sock'), usage) as client:
        assert client.follow_redirects is False
        assert client._transport._pool._ssl_context.check_hostname
        assert client._transport._pool._ssl_context.verify_mode == ssl.CERT_REQUIRED
        with pytest.raises(httpx.ConnectError):
            await client.post('https://api.openai.com/v1/chat/completions', json={'model': 'synthetic-only'})
        assert usage.billing_unknown and len(usage.provider_requests) == 1
        from lead_app.native_model_meter import ModelBillingUnknown
        with pytest.raises(ModelBillingUnknown):
            await client.post('https://api.openai.com/v1/chat/completions', json={'model': 'synthetic-only'})
    assert dns == []


@pytest.mark.asyncio
async def test_sdk_uds_peer_only_receives_tls_not_credentials_or_body(monkeypatch):
    def no_dns(*args, **kwargs):
        raise AssertionError('A dedicated route tried direct DNS')
    monkeypatch.setattr(socket, 'getaddrinfo', no_dns)
    with tempfile.TemporaryDirectory(prefix='lms-', dir='/tmp') as directory:
        route = directory + '/tls.sock'
        received = asyncio.get_running_loop().create_future()
        async def peer(reader, writer):
            raw = await reader.read(4096)
            if not received.done():
                received.set_result(raw)
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_unix_server(peer, route)
        async with server:
            async with model_client(payload(route), meter()) as client:
                with pytest.raises(httpx.ConnectError):
                    await client.post('https://api.openai.com/v1/chat/completions',
                        headers={'Authorization': 'Bearer synthetic-secret'}, json={'prompt': 'synthetic-private-body'})
            raw = await asyncio.wait_for(received, 2)
            assert raw.startswith(b'\x16\x03')
            assert b'api.openai.com' in raw  # HTTPS hostname/SNI survives UDS dialing.
            assert b'synthetic-secret' not in raw and b'synthetic-private-body' not in raw
            assert b'POST /' not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', ['route', None, 'invalid'])
async def test_hybrid_selects_before_reservation_and_sends_only_stdin_transport(monkeypatch, tmp_path, selected):
    from lead_app import admin_settings, config, hybrid_research

    route = str(tmp_path / 'synthetic.sock') if selected == 'route' else '' if selected == 'invalid' else None
    key = f'LEAD_MODEL_SOCKET_{UUID(AID).hex.upper()}_OPENAI'
    values = {key: route} if selected is not None else {}
    settings = {'tools': {'public_web': True, 'advanced_research': True}, 'max_tool_calls': 2,
        'task_models': {'research': None}, 'default_model': 'openai:synthetic',
        'max_model_requests': 2, 'max_output_tokens': 1000, 'timeout_seconds': 20}
    monkeypatch.setattr(admin_settings, 'for_agent', lambda *args: (settings, 'synthetic-version'))
    monkeypatch.setattr(config, 'resolve_model', lambda *args: payload(None)['model'])
    monkeypatch.setattr(hybrid_research, 'available', lambda: True)
    monkeypatch.setattr(hybrid_research, 'env', lambda: values)
    monkeypatch.setattr(hybrid_research, 'STATE', tmp_path)
    monkeypatch.setattr(hybrid_research, 'public_url', lambda url: url)
    monkeypatch.setattr(hybrid_research, 'selected_effort', lambda *args: None)
    monkeypatch.setattr(hybrid_research, 'research_endpoint', lambda *args: 'http://127.0.0.1:1')
    monkeypatch.setattr(hybrid_research, 'internal_token', lambda: 'synthetic-internal-token')
    reserved, inputs, started, launches = [], [], [], []
    monkeypatch.setattr(hybrid_research, 'reserve', lambda *args: reserved.append(args) or 'synthetic-reservation')
    monkeypatch.setattr(hybrid_research, 'settle', lambda *args, **kwargs: None)
    monkeypatch.setattr(hybrid_research, 'begin_run', lambda *args, **kwargs: started.append(args) or 'synthetic-run')
    monkeypatch.setattr(hybrid_research, 'finish_run', lambda *args: [])
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-parent-only-secret')
    monkeypatch.setenv('BROWSER_USE_API_KEY', 'synthetic-parent-only-cloud-key')
    monkeypatch.setenv(key, route or 'synthetic-parent-only-route')

    class GatewayClient:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return httpx.Response(200, json={'native_scope': {'target_id': 'synthetic-A'}},
                                  request=httpx.Request('POST', 'http://127.0.0.1:1'))

    class Child:
        returncode = 0
        async def communicate(self, raw):
            inputs.append(json.loads(raw))
            result = {'success': True, 'text': 'Synthetic only', 'usage': {'entry_count': 1,
                'total_tokens': 2, 'total_prompt_tokens': 1, 'total_completion_tokens': 1,
                'total_prompt_cached_tokens': 0, 'unmeasured_requests': 0}}
            return json.dumps(result).encode(), b''

    async def launch(*args, **kwargs):
        launches.append((args, kwargs))
        return Child()

    monkeypatch.setattr(hybrid_research.httpx, 'AsyncClient', GatewayClient)
    monkeypatch.setattr(hybrid_research.asyncio, 'create_subprocess_exec', launch)
    if selected == 'invalid':
        with pytest.raises(ValueError):
            await hybrid_research.explore(AID, 'https://public.fixture/', 'Synthetic goal')
        assert reserved == [] and started == [] and launches == []
    else:
        assert (await hybrid_research.explore(AID, 'https://public.fixture/', 'Synthetic goal'))['success']
        assert len(reserved) == len(inputs) == len(launches) == 1
        assert inputs[0]['model_socket'] == route
        args, kwargs = launches[0]
        assert 'OPENAI_API_KEY' not in kwargs['env'] and key not in kwargs['env']
        assert 'BROWSER_USE_API_KEY' not in kwargs['env']
        assert all('synthetic-parent-only-secret' not in str(value) for value in args)
        if route:
            assert all(route not in str(value) for value in args)
