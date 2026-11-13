"""Run the real worker main with offline HTTPX and SDK boundary doubles."""
import ast
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import httpx
import pytest
from lead_app.native_model_meter import ModelCallMeter


def worker(monkeypatch, tmp_path, *, run_error=None, stop_error=None, missing_usage=False, default_hints=False):
    """Replace only SDK/browser I/O; keep main, HTTP hooks and meter real."""
    actions = {}
    calls = []

    class Tools:
        def __init__(self):
            self.registry = SimpleNamespace(registry=SimpleNamespace(actions={'done': object()}))

        def action(self, description):
            def register(function):
                actions[function.__name__] = function
                return function
            return register

    class Browser:
        _intentional_stop = False

        def __init__(self, **kwargs):
            pass

        async def start_scoped(self, scope, reader, **kwargs):
            pass

        async def assert_fresh_observation(self):
            pass

        async def stop(self):
            if stop_error == 'wait':
                await asyncio.Event().wait()
            if stop_error:
                raise stop_error

    class History:
        history = [object()]

        def is_successful(self):
            return True

        def final_result(self):
            return 'Synthetic finished task.'

        def errors(self):
            return []

    class Agent:
        def __init__(self, *, llm, **kwargs):
            self.client = llm

        async def run(self, **kwargs):
            if default_hints:
                await actions['inspect_self']()
            await self.client.post('https://model.fixture/messages', content=b'synthetic prompt')
            if run_error:
                raise run_error
            return History()

    async def upstream(request):
        calls.append(request.content)
        return httpx.Response(200, json={} if missing_usage else {
            'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}})

    def provider_client(data, timeout, meter):
        return httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [meter.measure_request], 'response': [meter.measure_response]})

    async def manager(request):
        body = json.loads(request.content)
        assert body['operation'] == 'inspect_self'
        assert body['args'] == {'self_link_selector': '', 'acting_subject_selector': '',
            'self_subject_prefix': '', 'self_subject_suffix': ''}
        return httpx.Response(200, json={'state': 'self_surface_opened'})

    execution = ModuleType('lead_app.browser_execution')
    execution.scoped_session_class = lambda: Browser
    execution.guarded_tools = lambda *args, **kwargs: Tools()
    monkeypatch.setitem(sys.modules, execution.__name__, execution)
    views = ModuleType('browser_use.dom.views')
    views.DEFAULT_INCLUDE_ATTRIBUTES = ['id', 'value']
    monkeypatch.setitem(sys.modules, views.__name__, views)
    path = Path(__file__).resolve().parents[1] / 'scripts/browser_use_runner.py'
    tree = ast.parse(path.read_text())
    main = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'main')
    namespace = {'asyncio': asyncio, 'json': json, 'logging': logging, 'Path': Path,
        'httpx': SimpleNamespace(AsyncClient=lambda **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(manager), **kwargs)),
        'Tools': Tools, 'Agent': Agent, 'ActionResult': SimpleNamespace, 'ModelCallMeter': ModelCallMeter,
        'ModelBillingUnknown': __import__('lead_app.native_model_meter', fromlist=['ModelBillingUnknown']).ModelBillingUnknown,
        'research_llm': lambda data, timeout, client: client, 'provider_http_client': provider_client}
    exec(compile(ast.Module(body=[main], type_ignores=[]), str(path), 'exec'), namespace)
    data = {'workdir': str(tmp_path), 'native_scope': {'target_id': 'owned', 'cdp_url': 'ws://unused', 'headers': {}},
        'native_kind': 'connection', 'native_goal': 'Synthetic bounded task', 'max_steps': 1,
        'gateway_endpoint': 'http://manager.fixture', 'agent_id': 'synthetic', 'gateway_token': 'synthetic', 'run_id': 'synthetic',
        'max_requests': 8, 'max_total_tokens': 40000, 'max_output_tokens': 1800}
    return namespace['main'], data, calls, actions


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['agent', 'stop', 'timeout'])
async def test_failed_fragment_preserves_known_meter_without_exposing_exception(monkeypatch, tmp_path, failure):
    private = 'synthetic-private-detail-must-not-reach-output'
    main, data, calls, _ = worker(monkeypatch, tmp_path,
        run_error=RuntimeError(private) if failure == 'agent' else TimeoutError(private) if failure == 'timeout' else None,
        stop_error=RuntimeError(private) if failure == 'stop' else None)
    result = await main(data)
    assert result['success'] is False
    assert result['errors']
    assert private not in json.dumps(result)
    assert result['usage']['request_count'] == result['usage']['entry_count'] == 1
    assert result['usage']['total_tokens'] == 10
    assert result['usage']['billing_unknown'] is False
    assert result['usage']['unmeasured_requests'] == 0
    assert calls == [b'synthetic prompt']


@pytest.mark.asyncio
async def test_failure_does_not_turn_missing_response_usage_into_known(monkeypatch, tmp_path):
    main, data, calls, _ = worker(monkeypatch, tmp_path, run_error=RuntimeError('synthetic'), missing_usage=True)
    result = await main(data)
    assert result['success'] is False
    assert result['usage']['billing_unknown'] is True
    assert result['usage']['request_count'] == 1 and result['usage']['entry_count'] == 0
    assert result['usage']['unmeasured_requests'] == 1
    assert calls == [b'synthetic prompt']


@pytest.mark.asyncio
async def test_cleanup_is_bounded_and_cannot_report_success(monkeypatch, tmp_path):
    main, data, _, _ = worker(monkeypatch, tmp_path, stop_error='wait')
    result = await asyncio.wait_for(main(data), 3)
    assert result['success'] is False
    assert result['errors']
    assert result['usage']['total_tokens'] == 10


@pytest.mark.asyncio
async def test_parent_cancellation_still_propagates(monkeypatch, tmp_path):
    main, data, _, _ = worker(monkeypatch, tmp_path, run_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await main(data)


@pytest.mark.asyncio
async def test_cancellation_during_cleanup_still_propagates(monkeypatch, tmp_path):
    main, data, _, _ = worker(monkeypatch, tmp_path, stop_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await main(data)


@pytest.mark.asyncio
async def test_unchanged_success_retains_known_meter(monkeypatch, tmp_path):
    main, data, _, _ = worker(monkeypatch, tmp_path, default_hints=True)
    result = await main(data)
    assert result['success'] is True and result['errors'] == []
    assert result['usage']['total_tokens'] == 10
