"""Unexpected browser failures retain code locations, never page or error text."""
import json

import httpx
import pytest
from lead_app import activities, browser, community_runtime
from lead_app.config import ROOT
from lead_app.db import one, tx
from lead_app.growth import runtime
from lead_app.scope import service_scope
from lead_app.security import internal_token
from playwright.async_api import Error as BrowserError
from test_growth_telegram_community import community as community
from test_growth_telegram_community import source, work


async def test_real_http_failure_survives_rpc_and_task_result_without_private_text(community, monkeypatch, capsys):
    ref = work(community, 'inspect', source(community))
    claim = runtime.advance(ref)['claim']
    calls = []

    async def failed(*args, **kwargs):
        calls.append('inspect')
        raise BrowserError('NEVER_EXPORT cookie=private-secret https://private.invalid/?token=secret')

    original_client = httpx.AsyncClient

    def client(**kwargs):
        return original_client(**kwargs, transport=httpx.ASGITransport(app=browser.app, raise_app_exceptions=False))

    monkeypatch.setattr(browser, 'execute_community_step', failed)
    monkeypatch.setattr(activities.httpx, 'AsyncClient', client)
    with service_scope():
        result = await runtime.execute(claim)
    assert result['outcome'] == 'failed' and result['failure_code'] == 'HTTPStatusError'
    diagnostic = result['output']['http_diagnostic']
    assert diagnostic['status_code'] == 500 and diagnostic['operation'] == 'inspect'
    assert diagnostic['browser_failure']['category'] == 'browser_error'
    assert diagnostic['browser_failure']['module'] == 'browser'
    assert type(diagnostic['browser_failure']['line']) is int
    runtime.finish(claim, result)
    with tx(community.aid) as c:
        saved = one(c, 'SELECT result FROM lead.tasks WHERE id=%s', (ref['task_id'],))['result']
        assert saved['output']['output']['http_diagnostic'] == diagnostic
        assert one(c, 'SELECT count(*) n FROM lead.actions')['n'] == 0
    assert calls == ['inspect']
    log = capsys.readouterr().out
    assert 'TG_RESULT' in log and 'browser_error' in log
    for value in (json.dumps(result), json.dumps(saved), log):
        assert 'NEVER_EXPORT' not in value and 'private.invalid' not in value and 'cookie=' not in value


def test_metadata_uses_real_core_location_without_serializing_exception():
    from lead_app.channel_contracts import (
        browser_failure_metadata,
        read_browser_failure,
    )

    try:
        community_runtime._growth_group({}, {})
    except KeyError as error:
        value = browser_failure_metadata(error, ROOT)
    assert value['category'] == 'key_error' and value['module'] == 'community_runtime'
    assert isinstance(value['line'], int) and value['line'] > 0
    assert read_browser_failure({'browser_failure': value}) == value
    assert set(value) == {'category', 'module', 'line'}


@pytest.mark.parametrize('value', [
    None, 'NEVER_EXPORT', {'category': 'NEVER_EXPORT', 'module': 'telegram', 'line': 2},
    {'category': 'browser_error', 'module': 'NEVER_EXPORT', 'line': 2},
    {'category': 'browser_error', 'module': 'telegram', 'line': True},
    {'category': 'browser_error', 'module': 'telegram', 'line': 0},
    {'category': 'browser_error', 'module': 'telegram', 'line': 2, 'message': 'NEVER_EXPORT'},
    {'category': 'browser_error', 'module': 'unknown', 'line': 2},
])
def test_rpc_metadata_rejects_unrecognized_or_extra_fields(value):
    from lead_app.channel_contracts import read_browser_failure

    assert read_browser_failure({'browser_failure': value}) is None


@pytest.mark.parametrize(('kind', 'status'), [('value', 409), ('http', 418), ('validation', 422), ('unauthenticated', 403)])
async def test_existing_domain_auth_and_http_boundaries_remain(kind, status, monkeypatch):
    from fastapi import HTTPException

    async def failed(*args, **kwargs):
        if kind == 'value':
            raise ValueError('community_discussion_binding_required')
        raise HTTPException(418, 'retained')

    monkeypatch.setattr(browser, 'execute_community_step', failed)
    headers = {} if kind == 'unauthenticated' else {'Authorization': 'Bearer ' + internal_token()}
    body = {'task_id': 'invalid' if kind == 'validation' else '00000000-0000-0000-0000-000000000001', 'operation': 'inspect'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser.app, raise_app_exceptions=False), base_url='http://fixture') as client:
        response = await client.post('/agents/00000000-0000-0000-0000-000000000002/community/step', json=body, headers=headers)
    assert response.status_code == status
    assert 'browser_failure' not in response.text


@pytest.mark.parametrize('authenticated', [True, False])
async def test_failure_before_business_middleware_is_diagnosed_without_bypassing_auth(authenticated, monkeypatch, capsys):
    async def failed(*args, **kwargs):
        raise RuntimeError('NEVER_EXPORT upstream-private-token')

    monkeypatch.setattr(browser.TelemetryMiddleware, '__call__', failed)
    headers = {'Authorization': 'Bearer ' + internal_token()} if authenticated else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=browser.app, raise_app_exceptions=False), base_url='http://fixture') as client:
        response = await client.get('/health', headers=headers)
    if authenticated:
        assert response.status_code == 500
        assert response.json()['browser_failure'] == {'category': 'runtime_error', 'module': 'unknown', 'line': None}
        assert 'TG_RESULT' in capsys.readouterr().out
    else:
        assert response.status_code == 403 and 'browser_failure' not in response.text
        assert 'TG_RESULT' not in capsys.readouterr().out
    assert 'NEVER_EXPORT' not in response.text
