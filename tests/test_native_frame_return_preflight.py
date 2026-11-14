"""A completed SSO popup cannot hide a new owner-only public-frame decision.

Run the production Connection start/return, root facts, person preflight and
runner. Live DOM/CDP, Task/lease authority and final handoff persistence are
explicit seams. No SDK process, reservation, model request or browser runs here.
"""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from lead_app.native_connection import NativeConnection


@pytest.fixture
def returned_connection(monkeypatch):
    from lead_app import native_challenges, native_frame_roles, native_runner

    # This seam exercises the retained SDK preflight, not executor routing or SQL.
    monkeypatch.setattr(native_runner, 'local_executor_enabled', lambda *_args, **_kwargs: False)

    def build(*, sso, foreign):
        home = 'https://board.fixture/home'
        callback = 'https://board.fixture/session/complete?ticket=synthetic'
        scope = {'connection_ref': 'original-connection', 'target_id': 'original-root',
            'origins': ['https://board.fixture'], 'challenge_version': 4, 'input_seq': 7,
            'control_mode': 'automatic', 'task_generation': 1, 'claim': {'attempt_token': 'older-claim'}}
        events = []
        class Page:
            url = home
            def is_closed(self):
                return False
            async def goto(self, url, **kwargs):
                self.url = url
                events.append('return_home')
            async def evaluate(self, script):
                events.append('root_facts')
                return {'parent_url': self.url, 'supported': True, 'secure': True,
                    'frames': [{'index': 0, 'resource_url': 'https://public.fixture/reference'}] if foreign else []}
        class Popup:
            url = callback
            closed = False
            def locator(self, selector):
                assert selector == 'a[href]'
                return self
            async def evaluate_all(self, script):
                return [home]
            async def close(self):
                self.closed = True
                events.append('close_popup')
        page, popup = Page(), Popup()
        if sso:
            scope['auth_flow'] = {'popup_target': 'original-popup', 'callback': True,
                'urls': [callback], 'completed': False}
        async def scope_view(target):
            assert target == scope['target_id']
            events.append('scope_descriptor')
            return {'target_id': target, 'origins': list(scope['origins'])}
        runtime = SimpleNamespace(pages={scope['target_id']: {'scope': scope, 'page': page, 'slot_held': True}},
            slots=asyncio.Semaphore(2), scope_view=scope_view,
            bridge=SimpleNamespace(url='http://127.0.0.1:1', headers={}),
            gate=SimpleNamespace(documents={scope['target_id']: ['original-frame', 'home-loader', home]}),
            current_scope=lambda value: events.append('current_scope'))
        actor = SimpleNamespace(aid='00000000-0000-0000-0000-000000000021', native_runtime=runtime)
        runtime.actor = actor
        controller = NativeConnection(actor, 'original-task', 'original-connection')
        controller.page, controller.lease = page, scope
        task = {'input': {'task_generation': 1}, 'result': {'runtime': {'attempt_token': 'new-current-claim'}}}
        connection = {'control_mode': 'automatic', 'challenge_version': 4, 'input_seq': 7}
        controller.snapshot = lambda: (task, connection, {})
        async def owner_page():
            return popup
        controller.owner_page = owner_page
        async def no_person_challenge(current):
            assert current is page
            return None
        monkeypatch.setattr(native_challenges, 'person_challenge', no_person_challenge)
        class Result:
            def fetchone(self):
                return None
        class Database:
            def execute(self, sql, params):
                return Result()
        @contextmanager
        def transaction(*args):
            yield Database()
        monkeypatch.setattr(native_frame_roles, 'tx', transaction)
        async def handoff(reason):
            events.append('owner_handoff')
            return {'control_mode': 'owner', 'challenge_version': 5}
        controller.handoff = handoff
        async def observe():
            events.append('identity_observed')
            assert not foreign, 'Unapproved return-page frame must stop before identity/SDK work'
            return {'status': 'authenticated'}
        controller.observe_current = observe
        async def credentials():
            raise AssertionError('No credentials or SDK should be needed for this preflight')
        controller.credentials = credentials
        def reserve(*args, **kwargs):
            raise AssertionError('No model reservation before owner-only frame approval')
        monkeypatch.setattr(native_runner, 'reserve', reserve)
        monkeypatch.setenv('LEAD_DEPLOYMENT_MODE', 'development')
        return controller, scope, popup, events
    return build


@pytest.mark.asyncio
@pytest.mark.parametrize('completed,foreign,expected', [(True, True, True), (False, True, False), (True, False, False)])
async def test_completed_popup_no_longer_suppresses_current_frame_review(returned_connection, completed, foreign, expected):
    from lead_app.native_frame_roles import needs_review

    controller, scope, popup, events = returned_connection(sso=True, foreign=foreign)
    scope['auth_flow']['completed'] = completed
    assert bool(await needs_review(controller)) is expected
    assert scope['origins'] == ['https://board.fixture']


@pytest.mark.asyncio
@pytest.mark.parametrize('sso,foreign', [(True, True), (False, True), (True, False)],
    ids=['sso_return_frame', 'password_frame', 'sso_return_without_frame'])
async def test_real_return_start_and_runner_stop_before_sdk_for_new_frame(returned_connection, sso, foreign):
    from lead_app.native_runner import connect

    controller, scope, popup, events = returned_connection(sso=sso, foreign=foreign)
    result = await connect(controller, 'http://127.0.0.1:1')
    if sso:
        assert popup.closed and scope['auth_flow']['completed'] is True
        assert events.index('return_home') < events.index('close_popup')
    assert scope['claim'] == {'attempt_token': 'new-current-claim'}
    assert scope['origins'] == ['https://board.fixture']
    if foreign:
        assert result['control_mode'] == 'owner' and events[-1] == 'owner_handoff'
        assert 'identity_observed' not in events
    else:
        assert result == {'status': 'authenticated'} and events[-1] == 'identity_observed'
