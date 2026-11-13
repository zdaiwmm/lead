"""A newer worker update cannot lend its status to stale page-control proof.

These protocol-boundary tests use the real NativeAction verifier; DB claim and
DOM I/O are explicit synthetic seams. No browser, model, database or send occurs.
"""
import copy
import time
from types import SimpleNamespace

import pytest


def worker_action(monkeypatch, change=None):
    from lead_app import native_action

    version = {'versionId': 'v1', 'targetId': 'sw', 'status': 'activated',
               'runningStatus': 'running', 'controlledClients': ['page']}
    gate = SimpleNamespace(context_id='managed', targets={
        'root-session': {'targetId': 'page', 'type': 'page'},
        'sw-session': {'targetId': 'sw', 'type': 'service_worker', 'browserContextId': 'managed'}},
        network={('request', 'sw-session'): {'request': {'url': 'https://site.test/posts', 'method': 'POST'}}},
        documents={'page': ['root-frame', 'loader', 'https://site.test/home']},
        worker_versions_by_page={'root-session': {'v1': copy.deepcopy(version)}},
        worker_versions={'v1': copy.deepcopy(version)})
    claimed = []

    async def cookies(*args):
        return [{'name': 'synthetic-session', 'value': 'not-a-real-cookie'}]

    async def release(*args):
        pass

    controller = native_action.NativeAction.__new__(native_action.NativeAction)
    controller.actor = SimpleNamespace(aid='00000000-0000-0000-0000-000000000001', epoch=7,
        context=SimpleNamespace(cookies=cookies), native_runtime=SimpleNamespace(gate=gate, release_page=release,
            current_scope=lambda scope: None))
    controller.lease = {'target_id': 'page', 'document_revision': 3}
    controller.reconcile = False
    controller.action_id = '00000000-0000-0000-0000-000000000002'
    controller.claim = {'attempt_token': 'original-current-claim'}
    spec = {'url': 'https://site.test/posts', 'method': 'POST', 'body_kind': 'form',
            'content_field': 'body', 'dynamic_fields': ['nonce']}
    method = {'target_url': 'https://site.test/home', 'self_url': 'https://site.test/account',
              'request': spec, 'selectors': {}, 'platform_permission': {'selector': '#rules', 'quote': 'Synthetic permission'}}
    controller.action = {'type': 'publish', 'intent': {'body': 'original', 'browser_method': method}}
    controller.submit_proof = {'frame_id': 'root-frame', 'expires': time.monotonic() + 15,
        'claimed': False, 'revision': 3, 'editor_url': method['target_url'],
        'dynamic': {'nonce': 'original-nonce'}, 'observed': {'body': 'original'}}
    controller.phase = lambda name: None

    async def descriptor():
        return {}

    async def identity():
        # A later page's ServiceWorker.workerVersionUpdated can arrive during
        # the verifier's awaited current-identity/editor reads. No navigation,
        # TaskRef, nonce, ActionRef or document revision changes in this cut.
        if change == 'other_observer_partial':
            gate.worker_versions['v1'].pop('controlledClients', None)
        elif change == 'worker_target':
            gate.worker_versions['v1']['targetId'] = 'replacement-worker'
        return {'id': 'account', 'binding_version': 1, 'external_account_id': 'member'}

    async def temporary(*args):
        return SimpleNamespace(url=method['target_url']), {'target_id': 'aux'}

    async def baseline():
        pass

    async def inspect(*args, **kwargs):
        return {'request': spec}

    def claim(*args):
        claimed.append(args)
        return True

    controller.descriptor = descriptor
    controller.verify_private_self = identity
    controller.temporary_page = temporary
    controller.baseline = baseline
    monkeypatch.setattr('lead_app.native_method_facts.inspect_editor', inspect)
    monkeypatch.setattr(native_action, 'claim_request', claim)
    request = {'url': spec['url'], 'method': 'POST', 'postData': 'body=original&nonce=original-nonce',
        '_native_network': {'session': 'sw-session', 'network_id': 'request', 'worker_body_verified': True}}
    decision = {'sender_frame': 'real-worker-fetch-frame'}
    return controller, gate, request, decision, claimed


@pytest.mark.asyncio
async def test_newer_worker_target_invalidates_old_page_proof_before_claim(monkeypatch):
    controller, gate, request, decision, claimed = worker_action(monkeypatch, 'worker_target')
    assert controller.worker_submit_source(request)
    with pytest.raises(ValueError, match='native_submit_worker_changed'):
        await controller.verify_request(request, decision)
    assert claimed == []


@pytest.mark.asyncio
async def test_other_observer_omitted_clients_do_not_erase_current_page_proof(monkeypatch):
    # This other-observer update omits controlledClients. Do not mechanically
    # require that optional global field when this page still has its proof.
    controller, gate, request, decision, claimed = worker_action(monkeypatch, 'other_observer_partial')
    assert await controller.verify_request(request, decision)
    assert len(claimed) == 1


@pytest.mark.asyncio
async def test_current_worker_still_claims_only_the_original_action_and_request(monkeypatch):
    controller, gate, request, decision, claimed = worker_action(monkeypatch)
    assert await controller.verify_request(request, decision)
    assert len(claimed) == 1
    assert claimed[0][1:5] == (controller.action_id, controller.claim, 7, request)


@pytest.mark.asyncio
async def test_unknown_worker_action_does_not_enter_a_new_claim(monkeypatch):
    controller, gate, request, decision, claimed = worker_action(monkeypatch)
    controller.reconcile = True
    assert await controller.verify_request(request, decision) is False
    assert claimed == []


@pytest.mark.asyncio
async def test_worker_body_cannot_replace_the_original_dynamic_nonce(monkeypatch):
    controller, gate, request, decision, claimed = worker_action(monkeypatch)
    request['postData'] = 'body=original&nonce=other-nonce'
    with pytest.raises(ValueError, match='browser_dynamic_field_changed'):
        await controller.verify_request(request, decision)
    assert claimed == []
