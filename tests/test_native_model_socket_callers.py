"""Actual NativeRunner callers select routes before spending an attempt/budget.

DB/ledger/child are explicit synthetic seams; no model, browser or DB is used.
"""
import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from uuid import UUID

import pytest

AID = '00000000-0000-0000-0000-000000000021'
TASK = '00000000-0000-0000-0000-000000000022'
CONNECTION = '00000000-0000-0000-0000-000000000023'
MODEL_KEY = 'synthetic-model-stdin-only'
CLOUD_KEY = 'synthetic-cloud-parent-only'
LOGIN = {'username': 'synthetic-login-user', 'password': 'synthetic-login-password'}


@pytest.fixture
def caller(monkeypatch, tmp_path):
    from lead_app import admin_settings, native_runner

    events, launches, inputs, settlements = [], [], [], []
    settings = {'default_model': 'openai:synthetic', 'max_model_requests': 8, 'max_output_tokens': 1800}
    model = {'provider': 'openai', 'model': 'synthetic', 'base_url': 'https://api.openai.com/v1', 'key': MODEL_KEY}
    values = {'LEAD_DEPLOYMENT_MODE': 'development'}
    route_key = f'LEAD_MODEL_SOCKET_{UUID(AID).hex.upper()}_OPENAI'
    other_key = 'LEAD_MODEL_SOCKET_00000000000000000000000000000099_OPENAI'
    # An unrelated route is present in the parent; it cannot select this Agent
    # and still must not be inherited by the SDK process environment.
    values[other_key] = '/synthetic/other-agent.sock'
    monkeypatch.setenv(other_key, values[other_key])
    monkeypatch.setenv('OPENAI_API_KEY', 'synthetic-openai-parent-only')
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'synthetic-deepseek-parent-only')
    monkeypatch.setenv('BROWSER_USE_API_KEY', CLOUD_KEY)
    monkeypatch.setattr(admin_settings, 'for_agent', lambda *args: (settings, 'synthetic/models'))
    monkeypatch.setattr(native_runner, 'env', lambda: dict(values))
    monkeypatch.setattr(native_runner, 'resolve_model', lambda *args: dict(model))
    monkeypatch.setattr(native_runner, 'STATE', tmp_path)
    monkeypatch.setattr(native_runner, 'internal_token', lambda: 'synthetic-gateway-stdin-only')

    class Connection:
        def execute(self, statement, parameters):
            assert 'automatic_attempts=automatic_attempts+1' in statement
            assert parameters == (CONNECTION, 1)
            events.append('attempt_update')
            return SimpleNamespace(rowcount=1)

    @contextmanager
    def tx(*args):
        yield Connection()

    def reserve(*args):
        events.append('reserve')
        return 'synthetic-reservation'

    def settle(*args, **kwargs):
        settlements.append((args, kwargs))
        events.append('settle')

    monkeypatch.setattr(native_runner, 'tx', tx)
    monkeypatch.setattr(native_runner, 'reserve', reserve)
    monkeypatch.setattr(native_runner, 'settle', settle)
    scope = {'target_id': 'synthetic-owned-page', 'challenge_version': 1}
    controller = SimpleNamespace(actor=SimpleNamespace(aid=AID, profile={'id': 'synthetic-profile'}),
        task_id=TASK, connection_id=CONNECTION, page=SimpleNamespace(url='https://account.fixture/home'),
        lease=scope, observation=None)

    async def start():
        events.append('start')
        return scope

    async def no_challenge():
        return False

    async def not_yet_observed():
        return None

    async def credentials():
        return dict(LOGIN)

    async def handoff(*args):
        events.append('handoff')
        return {'status': 'waiting_user'}

    def snapshot():
        return {'experiment_id': None}, {'external_account_id': 'synthetic-bound-account'}, {}

    def start_fragment():
        events.append('fragment')

    controller.start = start
    controller.person_only_challenge = no_challenge
    controller.observe_current = not_yet_observed
    controller.credentials = credentials
    controller.handoff = handoff
    controller.snapshot = snapshot
    controller.start_fragment = start_fragment
    controller.remaining_seconds = lambda: 165

    class Child:
        returncode = None

        async def communicate(self, raw=None):
            if raw is not None:
                inputs.append(json.loads(raw))
            self.returncode = 0
            controller.observation = {'status': 'authenticated'}
            result = {'outcome': 'completed', 'usage': {'entry_count': 1, 'request_count': 1,
                'total_tokens': 2, 'total_prompt_tokens': 1, 'total_completion_tokens': 1,
                'unmeasured_requests': 0, 'billing_unknown': False}}
            return json.dumps(result).encode(), b''

        def kill(self):
            self.returncode = -9
            events.append('kill')

    async def spawn(*args, **kwargs):
        events.append('child')
        launches.append((args, kwargs))
        return Child()

    monkeypatch.setattr(native_runner.asyncio, 'create_subprocess_exec', spawn)

    async def invoke(kind):
        if kind == 'connect':
            return await native_runner.connect(controller, 'http://127.0.0.1:1')
        return await native_runner.run_task(controller, 'http://127.0.0.1:1', kind='discovery', goal='Synthetic only')

    return SimpleNamespace(invoke=invoke, values=values, route_key=route_key, events=events,
        launches=launches, inputs=inputs, settlements=settlements, root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['connect', 'run_task'])
@pytest.mark.parametrize('configured', [False, True])
async def test_actual_native_caller_passes_explicit_route_selection_only_on_stdin(caller, configured, kind, capsys, caplog):
    route = str(caller.root / 'synthetic-model.sock') if configured else None
    if configured:
        caller.values[caller.route_key] = route
    await caller.invoke(kind)
    assert len(caller.inputs) == len(caller.launches) == 1
    payload = caller.inputs[0]
    assert 'model_socket' in payload and payload['model_socket'] == route
    assert payload['agent_id'] == AID and payload['model']['key'] == MODEL_KEY
    assert caller.events.count('reserve') == caller.events.count('settle') == 1
    assert caller.events.count('attempt_update') == (1 if kind == 'connect' else 0)
    assert caller.events.count('fragment') == (1 if kind == 'run_task' else 0)
    args, kwargs = caller.launches[0]
    assert kwargs['stdin'] == asyncio.subprocess.PIPE
    environment = kwargs['env']
    assert not any(key in environment for key in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'BROWSER_USE_API_KEY'))
    assert not any(key.startswith('LEAD_MODEL_SOCKET_') for key in environment)
    capture = capsys.readouterr()
    public = str(args) + str(environment) + caplog.text + capture.out + capture.err
    forbidden = [MODEL_KEY, CLOUD_KEY, *LOGIN.values(), 'synthetic-bound-account',
                 'synthetic-gateway-stdin-only', 'synthetic-openai-parent-only', 'synthetic-deepseek-parent-only']
    if route:
        forbidden.append(route)
    assert all(value not in public for value in forbidden)
    assert caller.settlements[0][1]['actual'] == 2 and caller.settlements[0][1]['incomplete'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['connect', 'run_task'])
@pytest.mark.parametrize('route', ['', 'relative.sock', None])
async def test_invalid_route_precedes_actual_attempt_fragment_budget_and_child(caller, route, kind):
    # Present-but-null differs from the absent/unconfigured positive case.
    caller.values[caller.route_key] = route
    with pytest.raises(ValueError):
        await caller.invoke(kind)
    assert not set(caller.events) & {'attempt_update', 'fragment', 'reserve', 'child', 'settle', 'handoff'}
    assert caller.inputs == caller.launches == caller.settlements == []
