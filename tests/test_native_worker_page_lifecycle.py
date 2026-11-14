"""SW authorization keeps live page authority across awaited lease checks.

The real dispatcher runs unchanged. Only its database read and lease checker
are seams; a real thread barrier reproduces page creation/closure while the
dispatcher is suspended. No browser, model, database or outbound send occurs.
"""
import asyncio
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest


@pytest.fixture
def worker_pages(monkeypatch):
    from lead_app import db
    from lead_app.browser_runtime import PersistentRuntime

    class Rows:
        def fetchall(self):
            return []

    class Connection:
        def execute(self, query, params):
            assert 'authentication_endpoints' in query
            assert params == ('synthetic-profile',)
            return Rows()

    @contextmanager
    def transaction(aid):
        assert aid == 'synthetic-agent'
        yield Connection()

    monkeypatch.setattr(db, 'tx', transaction)
    scope = {'target_id': 'original-page', 'origins': ['https://board.test'],
             'account_id': 'original-account', 'binding_version': 3,
             'task_id': 'original-task', 'task_generation': 1, 'control_version': 7,
             'claim': {'attempt_token': 'original-claim'},
             'control_mode': 'automatic', 'challenge_version': 2, 'input_seq': 1,
             'authentication_endpoints': []}
    entry = {'scope': scope}
    runtime = PersistentRuntime.__new__(PersistentRuntime)
    runtime.actor = SimpleNamespace(aid='synthetic-agent', profile={'id': 'synthetic-profile'})
    runtime.forward_token = lambda: 'current-control'
    runtime.pages = {'original-page': entry}
    target = {'targetId': 'current-worker', 'type': 'service_worker',
              'browserContextId': 'current-context', 'url': 'https://board.test/sw.js'}
    return runtime, scope, entry, target


@pytest.mark.asyncio
@pytest.mark.parametrize('change,allowed', [
    ('close_unrelated', True),
    ('close_original', False),
    ('replace_entry', False),
    ('replace_scope', False),
    ('new_ambiguous_account', False),
    ('account_in_place', False),
    ('binding_in_place', False),
    ('claim_in_place', False),
    ('control_in_place', False),
    ('challenge_in_place', False),
    ('runtime_metadata', True),
    ('revoked_scope', False),
    ('page_in_place', False),
])
async def test_worker_read_survives_unrelated_page_close_but_never_reuses_removed_authority(
        worker_pages, change, allowed):
    runtime, scope, entry, target = worker_pages
    runtime.pages['unrelated-page'] = {'scope': {
        **scope, 'target_id': 'unrelated-page', 'origins': ['https://other.test']}}
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    blocked = False
    revoked = False

    def current(lease):
        nonlocal blocked
        if lease is scope and not blocked:
            blocked = True
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(2), 'synthetic lease check was not released'
        if revoked:
            raise ValueError('browser_profile_lease_expired')

    runtime.current_scope = current
    request = {'method': 'GET', 'url': 'https://board.test/sw.js'}
    checking = asyncio.create_task(runtime.authorize_request(None, target, request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == 'close_unrelated':
            runtime.pages.pop('unrelated-page')
        elif change == 'close_original':
            runtime.pages.pop('original-page')
        elif change == 'replace_entry':
            runtime.pages['original-page'] = {'scope': dict(scope)}
        elif change == 'replace_scope':
            entry['scope'] = dict(scope)
        elif change == 'account_in_place':
            scope['account_id'] = 'replacement-account'
        elif change == 'binding_in_place':
            scope['binding_version'] += 1
        elif change == 'claim_in_place':
            scope['claim']['attempt_token'] = 'replacement-claim'
        elif change == 'control_in_place':
            scope.update(control_mode='owner', input_seq=2)
        elif change == 'challenge_in_place':
            scope.update(challenge_version=3, task_generation=2)
        elif change == 'runtime_metadata':
            scope.update(document_revision=3, document_token=['frame', 'loader', 'https://board.test/home'],
                         frame_roles=[], auth_flow={'current_document': ['new-frame', 'new-loader']})
            scope['claim']['lease_until'] = 'synthetic-heartbeat-metadata'
            entry['slot_held'] = False
        elif change == 'revoked_scope':
            revoked = True
        elif change == 'page_in_place':
            entry['page'] = object()
        else:
            runtime.pages['new-page'] = {'scope': {
                **scope, 'target_id': 'new-page', 'account_id': 'another-account'}}
        release.set()
        decision = await checking
    finally:
        release.set()
        if not checking.done():
            checking.cancel()
            await asyncio.gather(checking, return_exceptions=True)
    assert decision == ({'kind': 'read'} if allowed else None)


@pytest.mark.asyncio
@pytest.mark.parametrize('change,allowed', [
    ('close_unrelated', True),
    ('close_original', False),
    ('replace_entry', False),
    ('replace_scope', False),
    ('new_ambiguous_action', False),
])
async def test_worker_action_keeps_only_the_original_live_candidate_after_second_scope_check(
        worker_pages, monkeypatch, change, allowed):
    from lead_app import native_read_roles

    runtime, scope, entry, target = worker_pages
    scope['action'] = {'id': 'original-action', 'intent': {
        'body': 'Original body', 'browser_method': {'request': {
            'url': 'https://board.test/posts', 'method': 'POST',
            'body_kind': 'form', 'content_field': 'body'}}}}
    runtime.pages['unrelated-page'] = {'scope': {
        **scope, 'target_id': 'unrelated-page', 'origins': ['https://other.test']}}

    async def no_maintenance(*args):
        return False

    # DOM-based maintenance has separate coverage. This cut starts only after
    # it declined and the real dispatcher scans exact publication candidates.
    monkeypatch.setattr(native_read_roles, 'authorize_maintenance', no_maintenance)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    checks = 0

    def current(lease):
        nonlocal checks
        if lease is scope:
            checks += 1
            if checks == 2:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(2), 'synthetic action check was not released'

    runtime.current_scope = current
    request = {'method': 'POST', 'url': 'https://board.test/posts', 'postData': 'body=Original+body'}
    checking = asyncio.create_task(runtime.authorize_request(None, target, request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        if change == 'close_unrelated':
            runtime.pages.pop('unrelated-page')
        elif change == 'close_original':
            runtime.pages.pop('original-page')
        elif change == 'replace_entry':
            runtime.pages['original-page'] = {'scope': dict(scope)}
        elif change == 'replace_scope':
            entry['scope'] = dict(scope)
        else:
            runtime.pages['new-page'] = {'scope': {
                **scope, 'target_id': 'new-page',
                'action': {**scope['action'], 'id': 'another-action'}}}
        release.set()
        decision = await checking
    finally:
        release.set()
        if not checking.done():
            checking.cancel()
            await asyncio.gather(checking, return_exceptions=True)
    if allowed:
        assert decision == {'kind': 'action', 'action_id': 'original-action', 'scope': scope}
        assert decision['scope'] is scope
    else:
        assert decision is None


@pytest.mark.asyncio
async def test_action_added_in_place_to_already_scanned_page_cannot_hide_a_second_candidate(
        worker_pages, monkeypatch):
    from lead_app import native_read_roles

    runtime, first, _, target = worker_pages
    action = {'id': 'original-action', 'intent': {
        'body': 'Original body', 'browser_method': {'request': {
            'url': 'https://board.test/posts', 'method': 'POST',
            'body_kind': 'form', 'content_field': 'body'}}}}
    second = {**first, 'target_id': 'second-page', 'action': action}
    runtime.pages['second-page'] = {'scope': second}

    async def no_maintenance(*args):
        return False

    monkeypatch.setattr(native_read_roles, 'authorize_maintenance', no_maintenance)
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    checks = 0

    def current(lease):
        nonlocal checks
        if lease is second:
            checks += 1
            if checks == 2:
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(2), 'synthetic second candidate check was not released'

    runtime.current_scope = current
    request = {'method': 'POST', 'url': 'https://board.test/posts', 'postData': 'body=Original+body'}
    checking = asyncio.create_task(runtime.authorize_request(None, target, request))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        # NativeAction.start installs its frozen Action through this real in-
        # place lease update after baseline observation (native_action.py:81).
        first.update(action={**action, 'id': 'newly-bound-action'})
        release.set()
        decision = await checking
    finally:
        release.set()
        if not checking.done():
            checking.cancel()
            await asyncio.gather(checking, return_exceptions=True)
    assert decision is None
