"""Pending owner approval cannot survive changes during async safety checks.

The actual decide/root_facts functions run; only the DB and browser boundaries
are synthetic. These tests do not access a real DB, browser or network.
"""
import copy
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'document', 'frame_src'])
async def test_approval_rechecks_original_dom_after_awaited_safety_checks(monkeypatch, change):
    from lead_app import native_frame_roles as roles
    from lead_app.growth.contracts import utcnow

    parent = 'https://parent.fixture/home'
    resource = 'https://public.fixture/reference'
    scope = {'target_id': 'parent', 'origins': [parent], 'control_version': 1}
    document = ['root-frame', 'original-loader', parent]
    gate = SimpleNamespace(documents={'parent': copy.deepcopy(document)})
    frame = {'index': 0, 'resource_url': resource, 'sandbox': None, 'credentialless': False,
             'name': '', 'title': 'Original public source'}
    writes, installed = [], []

    class Node:
        async def evaluate(self, script, url):
            installed.append((frame['resource_url'], url))
            frame['resource_url'] = url

    class Page:
        url = parent

        async def evaluate(self, script):
            return {'parent_url': parent, 'supported': True, 'secure': True, 'frames': [copy.deepcopy(frame)]}

        def locator(self, selector):
            assert selector == 'iframe[src]'
            return self

        def nth(self, index):
            assert index == 0
            return Node()

    def current(scope):
        pass

    async def protect(url):
        assert url == resource

    gate.protect_frame_responses = protect
    page = Page()
    runtime = SimpleNamespace(gate=gate, current_scope=current,
        actor=SimpleNamespace(aid='00000000-0000-0000-0000-000000000031'))
    source = {'candidate_id': 'a' * 64, 'parent_url': parent, 'resource_url': resource,
        'state': 'pending', 'reason': None, 'role': 'read_only_frame', 'document': copy.deepcopy(document),
        'expires_at': (utcnow() + timedelta(minutes=10)).isoformat(), 'challenge_version': 1}
    connection = {'id': '00000000-0000-0000-0000-000000000032', 'challenge_version': 1,
        'input_seq': 1, 'control_version': 1, 'connection_data': {'read_dependencies': [source]}}
    controller = SimpleNamespace(runtime=runtime, lease=scope, page=page, actor=runtime.actor,
        snapshot=lambda: ({}, connection, {}))

    async def candidates(controller):
        # The pending candidate and original first-party document were already
        # checked by the preceding owner-listing call. decide re-reads below.
        pass

    async def conflict(runtime, scope, url):
        # A real Storage/CDP await lets page/navigation events arrive without
        # changing the owner's challenge/input sequence in the DB.
        if change == 'document':
            gate.documents['parent'] = ['root-frame', 'replacement-loader', parent]
        elif change == 'frame_src':
            frame['resource_url'] = 'https://other-public.fixture/replacement'
        return None

    class DB:
        def execute(self, statement, values):
            writes.append(copy.deepcopy(values[0]))
            return SimpleNamespace(rowcount=1)

    @contextmanager
    def tx(*args):
        yield DB()

    monkeypatch.setattr(roles, 'candidates', candidates)
    monkeypatch.setattr(roles, 'known_conflict', conflict)
    monkeypatch.setattr(roles, 'tx', tx)
    monkeypatch.setattr(roles, 'require_agent', lambda *args: {'control_version': 1})
    monkeypatch.setattr(roles, 'j', lambda value: value)
    args = {'candidate_id': 'a' * 64, 'challenge_version': 1, 'decision': 'approve'}
    if change is None:
        result = await roles.decide(controller, args)
        assert result['candidates'][0]['state'] == 'approved'
        assert writes and installed == [(resource, resource)]
    else:
        with pytest.raises(ValueError, match='frame_source_changed'):
            await roles.decide(controller, args)
        assert not any(row['state'] == 'approved' for write in writes for row in write['read_dependencies'])
        assert installed == []
