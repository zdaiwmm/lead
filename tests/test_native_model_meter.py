"""Real HTTPX hooks stop the next model transport when prior billing is unknown."""
import ast
import json
from pathlib import Path

import httpx
import pytest


def meter(**limits):
    from lead_app.native_model_meter import ModelCallMeter
    return ModelCallMeter(max_requests=limits.get('requests', 8), max_total_tokens=limits.get('tokens', 1000),
        max_output_tokens=limits.get('output', 10), sensitive_data=limits.get('secrets', {}))


@pytest.mark.parametrize('bad', ['missing', 'invalid_json', 'non_2xx', 'boolean', 'negative', 'inconsistent', 'timeout'])
async def test_unknown_prior_usage_prevents_another_actual_http_transport(bad):
    from lead_app.native_model_meter import ModelBillingUnknown
    observed = []
    async def upstream(request):
        observed.append(request.content)
        if bad == 'timeout':
            raise httpx.ReadTimeout('synthetic response loss', request=request)
        if bad == 'non_2xx':
            return httpx.Response(503, json={'error': 'synthetic unavailable'})
        if bad == 'invalid_json':
            return httpx.Response(200, content=b'not json')
        usage = {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}
        if bad == 'boolean':
            usage['total_tokens'] = True
        if bad == 'negative':
            usage['prompt_tokens'] = -2
        if bad == 'inconsistent':
            usage['total_tokens'] = 4
        return httpx.Response(200, json={} if bad == 'missing' else {'usage': usage})
    current = meter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [current.measure_request], 'response': [current.measure_response]}) as client:
        try:
            await client.post('https://model.fixture/messages', content=b'first')
        except httpx.ReadTimeout:
            assert bad == 'timeout'
        with pytest.raises(ModelBillingUnknown):
            await client.post('https://model.fixture/messages', content=b'must not be sent')
    assert observed == [b'first']
    assert current.billing_unknown
    assert not current.provider_usage


async def test_known_usage_can_continue_within_original_request_and_token_limits():
    observed = []
    async def upstream(request):
        observed.append(request.content)
        return httpx.Response(200, json={'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
    current = meter(requests=2)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [current.measure_request], 'response': [current.measure_response]}) as client:
        await client.post('https://model.fixture/messages', content=b'first')
        await client.post('https://model.fixture/messages', content=b'second')
        with pytest.raises(ValueError, match='request limit'):
            await client.post('https://model.fixture/messages', content=b'third')
    assert observed == [b'first', b'second'] and len(current.provider_usage) == 2
    assert not current.billing_unknown


async def test_conservative_token_envelope_stops_before_the_next_transport():
    observed = []
    async def upstream(request):
        observed.append(request.content)
        return httpx.Response(200, json={'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
    current = meter(tokens=20, output=5)
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [current.measure_request], 'response': [current.measure_response]}) as client:
        await client.post('https://model.fixture/messages', content=b'first')
        with pytest.raises(ValueError, match='token envelope'):
            await client.post('https://model.fixture/messages', content=b'long second request')
    assert observed == [b'first'] and not current.billing_unknown


@pytest.mark.parametrize('content', [b'known-secret', json.dumps({'value': 'known-secret'}).encode()])
async def test_credential_literal_rejection_occurs_before_any_model_transport(content):
    observed = []
    async def upstream(request):
        observed.append(request.content)
        return httpx.Response(200)
    current = meter(secrets={'saved_password': 'known-secret'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [current.measure_request], 'response': [current.measure_response]}) as client:
        with pytest.raises(ValueError, match='redaction'):
            await client.post('https://model.fixture/messages', content=content)
    assert observed == [] and current.secret_seen


def runner_usage(current):
    """Evaluate the real serializer without importing the SDK or running a model."""
    path = Path(__file__).resolve().parents[1] / 'scripts/browser_use_runner.py'
    tree = ast.parse(path.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == 'main')
    values = [value for node in ast.walk(main) if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
              for key, value in zip(node.value.keys, node.value.values)
              if isinstance(key, ast.Constant) and key.value == 'usage']
    assert len(values) == 1
    scope = {name: getattr(current, name) for name in (
        'provider_usage', 'unmeasured_requests', 'provider_requests', 'provider_responses', 'secret_seen')}
    scope['meter'] = current
    return eval(compile(ast.Expression(values[0]), str(path), 'eval'), scope)


def manager_known(function, usage):
    """Exercise both actual native settlement predicates, with no database IO."""
    path = Path(__file__).resolve().parents[1] / 'services/api/lead_app/native_runner.py'
    tree = ast.parse(path.read_text())
    caller = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == function)
    values = [node.value for node in ast.walk(caller) if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == 'known' for target in node.targets)]
    assert len(values) == 1
    from lead_app.native_model_meter import known_usage
    return eval(compile(ast.Expression(values[0]), str(path), 'eval'), {'usage': usage,'known_usage':known_usage})


@pytest.mark.parametrize('caller',['connect','run_task'])
@pytest.mark.parametrize('case',['complete_zero','missing_report','started_without_usage'])
def test_only_complete_zero_transport_receipt_can_settle_zero(caller,case):
    usage={'entry_count':0,'request_count':0,'total_tokens':0,'total_prompt_tokens':0,
        'total_completion_tokens':0,'unmeasured_requests':0,'billing_unknown':False,'measurement_complete':True}
    if case=='missing_report':
        usage.pop('measurement_complete')
    elif case=='started_without_usage':
        usage.update(request_count=1,billing_unknown=True,unmeasured_requests=1)
    known=manager_known(caller,usage)
    assert known is (case=='complete_zero')
    path=Path(__file__).resolve().parents[1]/'services/api/lead_app/native_runner.py'
    function=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name==caller)
    calls=[node for node in ast.walk(function) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='settle'
        and any(key.arg=='incomplete' and isinstance(key.value,ast.UnaryOp) and isinstance(key.value.operand,ast.Name)
            and key.value.operand.id=='known' for key in node.keywords)]
    assert len(calls)==1
    values={key.arg:eval(compile(ast.Expression(key.value),str(path),'eval'),{'usage':usage,'known':known})
        for key in calls[0].keywords if key.arg in ('actual','incomplete')}
    assert values=={'actual':0 if known else None,'incomplete':not known}


@pytest.mark.parametrize('failure', ['headers', 'body'])
async def test_partial_known_usage_then_response_loss_stays_unknown_in_actual_runner_and_manager(failure):
    from lead_app.native_model_meter import ModelBillingUnknown

    observed = []

    class PartialBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"usage":'
            raise httpx.ReadTimeout('synthetic body lost after 200 response headers')

    async def upstream(request):
        observed.append(request.content)
        if len(observed) == 1:
            return httpx.Response(200, json={'usage': {'prompt_tokens': 2, 'completion_tokens': 3, 'total_tokens': 5}})
        if failure == 'headers':
            raise httpx.ReadTimeout('synthetic loss before headers', request=request)
        return httpx.Response(200, stream=PartialBody())

    current = meter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream),
            event_hooks={'request': [current.measure_request], 'response': [current.measure_response]}) as client:
        await client.post('https://model.fixture/messages', content=b'known first response')
        with pytest.raises(httpx.ReadTimeout):
            await client.post('https://model.fixture/messages', content=b'unknown second response')
        with pytest.raises(ModelBillingUnknown):
            await client.post('https://model.fixture/messages', content=b'SDK retry must not reach transport')
    assert len(observed) == 2 and current.billing_unknown
    usage = runner_usage(current)
    assert usage['total_tokens'] == 5 and usage['entry_count'] == 1 and usage['request_count'] == 2
    assert usage['unmeasured_requests'] >= usage['request_count'] - usage['entry_count']
    assert usage['billing_unknown'] is True
    assert not manager_known('connect', usage)
    assert not manager_known('run_task', usage)


@pytest.mark.parametrize('caller', ['connect', 'run_task'])
@pytest.mark.parametrize('uncertain', ['entry_count_mismatch', 'explicit_unknown'])
def test_native_settlement_does_not_release_an_uncertain_fragment(caller, uncertain):
    usage = {'entry_count': 1, 'request_count': 2 if uncertain == 'entry_count_mismatch' else 1,
             'total_tokens': 5, 'unmeasured_requests': 0, 'billing_unknown': uncertain == 'explicit_unknown'}
    assert not manager_known(caller, usage)
