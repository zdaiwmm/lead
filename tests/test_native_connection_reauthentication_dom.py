"""A bound account reuses only a fresh current/private identity, never cached facts."""
import os
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from lead_app import native_connection
from lead_app.native_connection import NativeConnection
from test_native_member_automation_dom import observed_b_rules  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.environ.get('LEAD_BROWSER_TESTS') != '1',
    reason='Explicit isolated reauthentication DOM verification required')]


def controller(page, board, monkeypatch):
    connection = {'challenge_version': 1, 'input_seq': 0,
                  'connection_data': {'site_url': board.origin + '/'}}
    account = {'id': str(uuid4()), 'external_account_id': board.state['username'],
               'binding_version': 2, 'connection_origin': board.origin, 'platform': 'web'}
    writes = []

    @contextmanager
    def transaction(_aid):
        yield SimpleNamespace(execute=lambda *args: writes.append(args))

    monkeypatch.setattr(native_connection, 'tx', transaction)
    monkeypatch.setattr(native_connection, 'one', lambda *_args: {
        'selector': 'dd:nth-of-type(2)', 'self_url': board.origin + '/desk/account',
        'link_selector': 'a[rel~=me]'})
    runtime = SimpleNamespace(pages={})

    async def acquire(_scope, url, **_kwargs):
        fresh = await page.context.new_page()
        await fresh.goto(url)
        target = str(uuid4())
        runtime.pages[target] = {'page': fresh}
        return {'target_id': target}

    async def release(target):
        await runtime.pages.pop(target)['page'].close()

    runtime.acquire_page = acquire
    runtime.release_page = release
    actor = SimpleNamespace(aid=str(uuid4()), session=str(uuid4()), native_runtime=runtime)
    value = NativeConnection(actor, str(uuid4()), str(uuid4()))
    value.page = page
    value.lease = {'target_id': str(uuid4()), 'challenge_version': 1}
    value.snapshot = lambda: ({}, connection, account)
    value.descriptor = AsyncMock()
    value.advance_auth_flow = AsyncMock(return_value=False)
    value.handoff = AsyncMock(return_value={'control_mode': 'owner'})
    return value, writes


async def test_cold_bound_account_stays_on_real_signin_without_cached_private_probe(observed_b_rules, monkeypatch):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    await page.context.clear_cookies()
    await page.goto(board.origin + '/access')
    value, writes = controller(page, board, monkeypatch)
    before = len(board.oracle()['requests'])
    assert await value.observe_current() is None
    assert page.url == board.origin + '/access'
    assert not writes and not value.handoff.called and value.self_entry is None
    assert not any(row['path'] == '/desk/account' for row in board.oracle()['requests'][before:])


async def test_warm_bound_account_proves_current_then_independent_private_subject(observed_b_rules, monkeypatch):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    value, writes = controller(page, board, monkeypatch)
    before = len(board.oracle()['requests'])
    observed = await value.observe_current()
    assert observed['status'] == 'authenticated'
    assert value.self_entry['acting_subject'] == board.state['username']
    assert observed['operating_subject']['external_id'] == board.state['username']
    assert len([row for row in board.oracle()['requests'][before:] if row['path'] == '/desk/account']) == 2
    assert writes and not value.handoff.called


async def test_warm_current_subject_different_from_frozen_account_hands_to_owner(observed_b_rules, monkeypatch):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    value, writes = controller(page, board, monkeypatch)
    await page.locator('nav span').evaluate("node=>node.innerText='Signed in as another.member'")
    result = await value.observe_current()
    assert result['control_mode'] == 'owner'
    assert value.handoff.called and not writes
    assert value.observation is None


async def test_private_only_cached_entry_cannot_authenticate_without_current_subject(observed_b_rules, monkeypatch):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    await page.goto(board.origin + '/desk/account')
    value, writes = controller(page, board, monkeypatch)
    value.self_entry = {'url': page.url, 'selector': 'a[rel~=me]'}
    with pytest.raises(ValueError) as caught:
        await value.report_identity({'subject_selector': 'dd:nth-of-type(2)'})
    assert getattr(caught.value, 'code', None) == 'identity_current_self_unestablished'
    assert not writes and not value.actor.native_runtime.pages


@pytest.mark.parametrize('mode', ['dispatched', 'permission_denied'])
async def test_cold_retry_cannot_resubmit_after_dispatch_or_permission_denial(observed_b_rules, monkeypatch, mode):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    await page.context.clear_cookies()
    await page.goto(board.origin + '/access')
    value, writes = controller(page, board, monkeypatch)
    if mode == 'dispatched':
        value.lease['authentication_dispatched'] = True
    else:
        await page.locator('main').evaluate("node=>node.prepend(Object.assign(document.createElement('h1'),{textContent:'Access denied. You do not have permission.'}))")
    result = await value.observe_current()
    assert result['control_mode'] == 'owner'
    assert value.handoff.called and not writes
    assert value.observation is None


@pytest.mark.parametrize('dispatched', [False, True])
async def test_cached_current_document_redirects_to_actual_signin_without_replaying_credentials(observed_b_rules, monkeypatch, dispatched):  # noqa: F811
    page, _, _, _, board = observed_b_rules
    # Keep the old rendered member desk, while its real server session is gone.
    await page.context.clear_cookies()
    value, writes = controller(page, board, monkeypatch)
    if dispatched:
        value.lease['authentication_dispatched'] = True
    before = len(board.oracle()['requests'])
    result = await value.observe_current()
    assert page.url == board.origin + '/access'
    if dispatched:
        assert result['control_mode'] == 'owner' and value.handoff.called
    else:
        assert result is None and not value.handoff.called
    assert not writes and value.observation is None
    assert not any(row['method'] == 'POST' for row in board.oracle()['requests'][before:])
