"""Real HTTPX hooks stop the next model transport when prior billing is unknown."""
import json

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
