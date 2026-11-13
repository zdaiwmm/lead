"""An owner identity handoff is terminal for this automatic auth invocation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app import local_executor, local_executor_browser, native_runner


@pytest.mark.asyncio
async def test_codex_owner_observation_stops_before_credentials_or_request(monkeypatch):
    submitted=[]
    monkeypatch.setattr(local_executor,'submit',lambda *args,**kwargs:submitted.append(args))
    task={'input':{'task_generation':1},'result':{'runtime':{'attempt_token':'attempt'}}}
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a',profile={'id':'p'}),task_id='task',
        lease={'control_version':1},connection_id='connection',start=AsyncMock(),
        snapshot=lambda:(task,{},{}),person_only_challenge=AsyncMock(return_value=False),
        observe_current=AsyncMock(return_value={'control_mode':'owner','challenge_version':2}),
        credentials=AsyncMock())
    result=await local_executor_browser.run(controller,'http://gateway','connection','Authenticate')
    assert result=={'control_mode':'owner','challenge_version':2}
    assert not submitted
    controller.credentials.assert_not_called()


@pytest.mark.asyncio
async def test_connect_does_not_handoff_twice_after_codex_current_identity_handoff(monkeypatch):
    result={'control_mode':'owner','challenge_version':2}
    monkeypatch.setattr(native_runner,'local_executor_enabled',lambda *_args,**_kwargs:True)
    monkeypatch.setattr(local_executor_browser,'run',AsyncMock(return_value=result))
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a'),task_id='task',observation=None,
        handoff=AsyncMock())
    assert await native_runner.connect(controller,'http://gateway')==result
    controller.handoff.assert_not_called()


@pytest.mark.asyncio
async def test_legacy_auth_also_preserves_current_identity_owner_handoff(monkeypatch):
    monkeypatch.setattr(native_runner,'local_executor_enabled',lambda *_args,**_kwargs:False)
    result={'control_mode':'owner','challenge_version':2}
    controller=SimpleNamespace(actor=SimpleNamespace(aid='a'),task_id='task',
        start=AsyncMock(),person_only_challenge=AsyncMock(return_value=False),
        observe_current=AsyncMock(return_value=result),credentials=AsyncMock())
    assert await native_runner.connect(controller,'http://gateway')==result
    controller.credentials.assert_not_called()
