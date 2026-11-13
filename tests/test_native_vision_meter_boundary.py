"""Image transport accounting with real HTTPX hooks and no provider calls."""
import base64
import io
import json
import random

import httpx
import pytest
from lead_app.native_model_meter import ModelBillingUnknown, ModelCallMeter, known_usage
from lead_app.native_vision import IMAGE_MODEL, MAX_MODEL_BODY_BYTES, supported_model
from PIL import Image


def picture():
    image = Image.frombytes('RGB', (64, 32), random.Random(13).randbytes(64 * 32 * 3))
    stream = io.BytesIO()
    image.save(stream, format='PNG')
    return base64.b64encode(stream.getvalue()).decode()


def payload(image, *, text='ordinary page', role='user'):
    return {'model': IMAGE_MODEL, 'messages': [{'role': role, 'content': [
        {'type': 'text', 'text': text},
        {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image}},
    ]}]}


def measured(*, image_model=IMAGE_MODEL, secrets=None):
    return ModelCallMeter(max_requests=8, max_total_tokens=40000, max_output_tokens=1800,
        image_model=image_model, sensitive_data=secrets)


async def fresh():
    return True


def transport(current, calls, *, missing_usage=False):
    async def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={} if missing_usage else {
            'usage': {'prompt_tokens': 1024, 'completion_tokens': 3, 'total_tokens': 1027}})
    return httpx.AsyncClient(transport=httpx.MockTransport(upstream), event_hooks={
        'request': [current.measure_request], 'response': [current.measure_response]})


@pytest.mark.asyncio
async def test_approved_image_is_sent_once_and_short_subject_is_not_scanned_as_pixels():
    image = picture()
    assert '7' in image
    current, calls = measured(secrets={'bound_account_subject': '7'}), []
    current.approve_image(image, fresh)
    async with transport(current, calls) as client:
        await client.post('https://api.deepseek.com/chat/completions', json=payload(image))
    assert len(calls) == 1 and current.vision_images_sent == 1
    assert len(current.provider_usage) == 1 and not current.billing_unknown


@pytest.mark.asyncio
async def test_secret_in_text_is_still_blocked_after_the_inline_image_is_excluded():
    image = picture()
    current, calls = measured(secrets={'bound_account_subject': '7'}), []
    current.approve_image(image, fresh)
    async with transport(current, calls) as client:
        with pytest.raises(ValueError):
            await client.post('https://api.deepseek.com/chat/completions', json=payload(image, text='subject 7'))
    assert not calls and not current.provider_requests


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['remaining_budget', 'raw_body', 'unapproved', 'revoked', 'provider', 'external', 'wrong_role', 'duplicate_json'])
async def test_image_rejection_happens_before_the_actual_http_transport(bad):
    image = picture()
    current, calls = measured(image_model=None if bad == 'provider' else IMAGE_MODEL), []
    if bad != 'unapproved':
        async def verify():
            return bad != 'revoked'
        current.approve_image(image, verify)
    data = payload(image, role='assistant' if bad == 'wrong_role' else 'user')
    if bad == 'remaining_budget':
        current.provider_requests.append(1)
        current.provider_usage.append({'prompt_tokens': 38999, 'completion_tokens': 1, 'total_tokens': 39000})
    if bad == 'external':
        data['messages'][0]['content'][1]['image_url']['url'] = 'https://external.invalid/private.png'
    body = json.dumps(data).encode()
    if bad == 'raw_body':
        body = b' ' * (MAX_MODEL_BODY_BYTES + 1)
    if bad == 'duplicate_json':
        # Encoded spelling must not hide an image from a parse-error fallback.
        body = body.replace(b'"model":', b'"model":"untrusted","model":').replace(b'image', b'\\u0069mage')
        current.image_model = None
    before = len(current.provider_requests)
    async with transport(current, calls) as client:
        with pytest.raises(ValueError):
            await client.post('https://api.deepseek.com/chat/completions', content=body)
    assert not calls and len(current.provider_requests) == before


@pytest.mark.asyncio
async def test_unknown_image_response_blocks_next_transport_and_cannot_be_known_zero():
    image = picture()
    current, calls = measured(), []
    current.approve_image(image, fresh)
    async with transport(current, calls, missing_usage=True) as client:
        await client.post('https://api.deepseek.com/chat/completions', json=payload(image))
        with pytest.raises(ModelBillingUnknown):
            await client.post('https://api.deepseek.com/chat/completions', json=payload(image))
    assert len(calls) == 1 and current.billing_unknown
    report = {'measurement_complete': True, 'request_count': len(current.provider_requests),
        'entry_count': len(current.provider_usage), 'billing_unknown': current.billing_unknown,
        'unmeasured_requests': len(current.unmeasured_requests), 'total_tokens': 0,
        'total_prompt_tokens': 0, 'total_completion_tokens': 0}
    assert not known_usage(report)


def test_complete_zero_transport_report_never_includes_missing_or_unknown_usage():
    report = {'measurement_complete': True, 'request_count': 0, 'entry_count': 0,
        'billing_unknown': False, 'unmeasured_requests': 0, 'total_tokens': 0,
        'total_prompt_tokens': 0, 'total_completion_tokens': 0}
    assert known_usage(report)
    assert not known_usage(None) and not known_usage({})
    assert not known_usage({**report, 'measurement_complete': False})
    assert not known_usage({**report, 'request_count': 1})
    assert not known_usage({**report, 'billing_unknown': True})


def test_vision_capability_keeps_the_true_official_model_and_endpoint():
    model = {'provider': 'deepseek', 'model': IMAGE_MODEL, 'base_url': 'https://api.deepseek.com'}
    assert supported_model(model)
    assert not supported_model({**model, 'provider': 'other'})
    assert not supported_model({**model, 'model': 'pretend-vision-model'})
    assert not supported_model({**model, 'base_url': 'http://127.0.0.1:1'})
