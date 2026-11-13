"""An internal request claim cannot start a new SDK/renderer observation.

Run the actual original verifier and worker proof; DB/current-scope and auxiliary
DOM checks are explicit seams. Public DOM output still uses the full descriptor.
"""
import pytest
from lead_app.native_task import NativeTask
from test_native_worker_submit_scope_review import worker_action


@pytest.mark.asyncio
@pytest.mark.parametrize('revoked', [False, True])
async def test_paused_request_claim_checks_current_authority_without_renderer_observation(monkeypatch, revoked):
    controller, gate, request, decision, claimed = worker_action(monkeypatch)
    events = []
    def current(lease):
        assert lease is controller.lease
        events.append('current_authority')
        if revoked:
            raise ValueError('browser_task_scope_changed')
    controller.actor.native_runtime.current_scope = current
    async def descriptor():
        raise AssertionError('Paused request verification must not evaluate the original sender renderer')
    controller.descriptor = descriptor
    identity = controller.verify_private_self
    async def private_self():
        events.append('fresh_private_self_aux')
        return await identity()
    controller.verify_private_self = private_self
    if revoked:
        with pytest.raises(ValueError, match='browser_task_scope_changed'):
            await controller.verify_request(request, decision)
        assert claimed == [] and events == ['current_authority']
    else:
        assert await controller.verify_request(request, decision)
        assert events[:2] == ['current_authority', 'fresh_private_self_aux']
        assert len(claimed) == 1
        assert claimed[0][1:5] == (controller.action_id, controller.claim, 7, request)


@pytest.mark.asyncio
async def test_sdk_descriptor_still_requires_live_public_frame_observation(monkeypatch):
    controller, gate, request, decision, claimed = worker_action(monkeypatch)
    entered = []
    async def scope_view(target):
        assert target == controller.lease['target_id']
        entered.append(True)
        raise ValueError('public_frame_live_observation_required')
    controller.actor.native_runtime.scope_view = scope_view
    with pytest.raises(ValueError, match='public_frame_live_observation_required'):
        await NativeTask.descriptor(controller)
    assert entered == [True] and claimed == []
