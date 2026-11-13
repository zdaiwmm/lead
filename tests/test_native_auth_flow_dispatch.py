"""Original reserved SSO roles precede the independent public-frame fallback.

The production request dispatcher and finite auth flow run unchanged. Database
and live Task/lease checks are explicit seams; frame isolation has its separate
real Chromium/product coverage. No model, browser or network runs here.
"""
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lead_app.browser_runtime import PersistentRuntime
from lead_app.native_auth_flow import attach_popup, note_redirect, reserve


@pytest.fixture
def authentication_dispatch(monkeypatch):
    from lead_app import db

    checked, writes = [], []
    class Result:
        def fetchall(self):
            return []
    class Connection:
        def execute(self, sql, params):
            if sql.startswith('UPDATE'):
                writes.append(sql)
            return Result()
    @contextmanager
    def transaction(*args):
        yield Connection()
    monkeypatch.setattr(db, 'tx', transaction)
    scope = {'connection_ref': 'original-connection', 'target_id': 'original-root',
        'origins': ['https://board.fixture'], 'challenge_version': 3,
        'control_mode': 'owner', 'authentication_endpoints': []}
    target = {'targetId': 'actual-popup', 'openerId': scope['target_id'], 'type': 'page'}
    reserve(scope, 'https://board.fixture/sso/start')
    attach_popup(scope, target)
    document = ['actual-popup-frame', 'actual-loader', 'https://identity.fixture/authorize?state=observed']
    runtime = SimpleNamespace(actor=SimpleNamespace(aid='synthetic-agent', profile={'id': 'synthetic-profile'}),
        forward_token=lambda: 'live-managed-token', current_scope=lambda value: checked.append(value),
        gate=SimpleNamespace(documents={target['targetId']: document}), pages={})
    request = {'method': 'GET', 'url': document[2]}
    return runtime, scope, target, request, checked, writes


async def observed_redirect(runtime, scope, target, request):
    start = scope['auth_flow']['start_url']
    decision = await PersistentRuntime.authorize_request(runtime, scope, target, {'method': 'GET', 'url': start})
    assert decision == {'kind': 'authentication'}
    # A CDP redirectResponse binds the IdP destination; a model-supplied URL
    # cannot append it to this finite authentication role.
    note_redirect(scope, target, {'request': request, 'redirectResponse': {
        'url': start, 'status': 303, 'headers': {'Location': request['url']}}})
    assert scope['auth_flow']['urls'] == [start, request['url']]


@pytest.mark.asyncio
async def test_reserved_foreign_popup_authentication_uses_original_role(authentication_dispatch):
    runtime, scope, target, request, checked, writes = authentication_dispatch
    await observed_redirect(runtime, scope, target, request)
    result = await PersistentRuntime.authorize_request(runtime, scope, target, request)
    assert result == {'kind': 'authentication'}
    assert scope['origins'] == ['https://board.fixture']
    assert scope['authentication_endpoints'] == ['https://board.fixture/sso/start', 'https://identity.fixture/authorize']
    assert scope['auth_flow']['foreign_seen'] is True
    assert len(checked) == 2 and len(writes) == 2
    # The exact first-hop read is one use, including the same valid popup.
    assert await PersistentRuntime.authorize_request(runtime, scope, target, request) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['wrong_opener', 'wrong_target', 'service_worker', 'unreserved_url', 'expired', 'completed'])
async def test_foreign_authentication_never_falls_back_to_unscoped_read(authentication_dispatch, change):
    runtime, scope, target, request, checked, writes = authentication_dispatch
    await observed_redirect(runtime, scope, target, request)
    if change == 'wrong_opener':
        target = {**target, 'openerId': 'another-page'}
    elif change == 'wrong_target':
        target = {**target, 'targetId': 'another-popup'}
    elif change == 'service_worker':
        target = {**target, 'type': 'service_worker'}
    elif change == 'unreserved_url':
        request = {**request, 'url': 'https://identity.fixture/unreserved'}
    elif change == 'expired':
        scope['auth_flow']['expires'] = time.monotonic() - 1
    else:
        scope['auth_flow']['completed'] = True
    assert await PersistentRuntime.authorize_request(runtime, scope, target, request) is None
    assert scope['origins'] == ['https://board.fixture']
    assert scope['authentication_endpoints'] == ['https://board.fixture/sso/start']
    assert len(checked) == 2 and len(writes) == 1


@pytest.mark.asyncio
async def test_approved_public_frame_keeps_its_separate_dispatch(authentication_dispatch, monkeypatch):
    from lead_app import native_frame_roles

    runtime, scope, target, request, checked, writes = authentication_dispatch
    await observed_redirect(runtime, scope, target, request)
    resource = {'method': 'GET', 'url': 'https://public.fixture/reference'}
    child = {'targetId': 'isolated-frame', 'type': 'iframe'}
    delegated = []
    decision = {'kind': 'read', 'public_frame': True, 'frame_role': {'source_proof': 'approved-server-proof'}}
    async def isolated_frame(current, lease, actual, outgoing):
        assert current is runtime and lease is scope and actual is child and outgoing is resource
        delegated.append(True)
        return decision
    monkeypatch.setattr(native_frame_roles, 'request', isolated_frame)
    assert await PersistentRuntime.authorize_request(runtime, scope, child, resource) is decision
    assert delegated == [True]
    assert scope['origins'] == ['https://board.fixture']
    assert len(checked) == 2 and len(writes) == 1
