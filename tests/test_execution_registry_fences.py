"""Current server transport registrations fence existing execution references."""
import json

import pytest
from lead_app import execution_routes
from lead_app.browser_actions import claim_request, prepare_binding
from lead_app.channel_contracts import ExecutionRef
from lead_app.db import one, tx
from lead_app.execution_routes import (
    endpoint,
    register,
    request_route,
    resolve,
    transports,
)
from test_browser_action_transactions import native_work


def registry(monkeypatch, url='http://127.0.0.1:19001', **extra):
    monkeypatch.setenv('LEAD_BROWSER_TRANSPORTS', json.dumps({'shared': url, **extra}))


def test_new_server_references_freeze_only_the_selected_transport(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, action, _, _, _, _ = native_work(growth_agent)
    original = resolve(growth_agent, profile_id=actor.profile['id'])
    revision = getattr(original, 'registry_revision', None)
    assert isinstance(revision, str) and len(revision) == 64
    assert action['intent']['execution_ref']['registry_revision'] == revision
    assert register(growth_agent, actor.profile['id']) == original
    registry(monkeypatch, url='http://127.0.0.1:19001/', unrelated='http://127.0.0.1:19009')
    assert resolve(growth_agent, profile_id=actor.profile['id']) == original
    assert endpoint(original) == 'http://127.0.0.1:19001'


def test_same_name_endpoint_change_invalidates_frozen_reference(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, _, _, _, _, _ = native_work(growth_agent)
    original = resolve(growth_agent, profile_id=actor.profile['id'])
    registry(monkeypatch, url='http://127.0.0.1:19002')
    current = resolve(growth_agent, profile_id=actor.profile['id'])
    assert current.route_generation == original.route_generation
    with pytest.raises(ValueError, match='execution_registry_changed'):
        endpoint(original)
    assert endpoint(current) == 'http://127.0.0.1:19002'


def test_endpoint_validates_and_selects_from_one_registry_snapshot(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, _, _, _, _, _ = native_work(growth_agent)
    original = resolve(growth_agent, profile_id=actor.profile['id'])
    snapshots = iter([{'shared': 'http://127.0.0.1:19001'}, {'shared': 'http://127.0.0.1:19002'}])
    monkeypatch.setattr(execution_routes, 'transports', lambda: next(snapshots))
    assert endpoint(original) == 'http://127.0.0.1:19001'
    assert next(snapshots) == {'shared': 'http://127.0.0.1:19002'}


def test_prepared_action_cannot_claim_through_a_reused_transport_name(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, action, claim, request, observed, _ = native_work(growth_agent)
    prepare_binding(actor, action['id'], claim, actor.epoch)
    registry(monkeypatch, url='http://127.0.0.1:19002')
    with pytest.raises(ValueError, match='execution_route_changed'):
        claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(growth_agent) as c:
        assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action['id'],))['state'] == 'prepared'
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts')['n'] == 1
        attempt = one(c, 'SELECT observation FROM lead.action_attempts WHERE action_id=%s', (action['id'],))
        assert attempt['observation']['browser_request']['claimed'] is False


def test_unknown_action_keeps_original_hash_and_never_follows_new_endpoint(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, action, claim, request, observed, _ = native_work(growth_agent)
    original = resolve(growth_agent, profile_id=actor.profile['id'])
    prepare_binding(actor, action['id'], claim, actor.epoch)
    assert claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s", (action['id'],))
    registry(monkeypatch, url='http://127.0.0.1:19002')
    with pytest.raises(ValueError, match='execution_registry_changed'):
        with tx(growth_agent) as c:
            endpoint(request_route(c, growth_agent, '/reconcile', {'action_id': str(action['id'])}))
    assert not claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(growth_agent) as c:
        actual = one(c, 'SELECT state,intent FROM lead.actions WHERE id=%s', (action['id'],))
        assert actual['state'] == 'unknown' and actual['intent']['execution_ref'] == original.model_dump(mode='json')
        assert one(c, 'SELECT count(*) n FROM lead.actions')['n'] == 1
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts')['n'] == 1


def test_legacy_reference_remains_unstamped_and_uses_no_invented_history(growth_agent, monkeypatch):
    registry(monkeypatch)
    actor, _, _, _, _, _ = native_work(growth_agent)
    original = resolve(growth_agent, profile_id=actor.profile['id'])
    legacy = ExecutionRef.model_validate(original.model_dump(exclude={'registry_revision'}))
    assert getattr(legacy, 'registry_revision', None) is None
    registry(monkeypatch, url='http://127.0.0.1:19002')
    assert endpoint(legacy) == 'http://127.0.0.1:19002'
    assert getattr(legacy, 'registry_revision', None) is None


def test_legacy_unknown_action_is_not_upgraded_or_resubmitted(growth_agent, monkeypatch):
    registry(monkeypatch)
    current_resolve = execution_routes.resolve

    def legacy_resolve(*args, **kwargs):
        reference = current_resolve(*args, **kwargs)
        return ExecutionRef.model_validate(reference.model_dump(exclude={'registry_revision'}))

    # Emulate the server reference emitted before registry revisions existed.
    with monkeypatch.context() as old_server:
        old_server.setattr(execution_routes, 'resolve', legacy_resolve)
        actor, action, claim, request, observed, _ = native_work(growth_agent)
    assert action['intent']['execution_ref'].get('registry_revision') is None
    prepare_binding(actor, action['id'], claim, actor.epoch)
    assert claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s", (action['id'],))
    registry(monkeypatch, url='http://127.0.0.1:19002')
    with tx(growth_agent) as c:
        retained = request_route(c, growth_agent, '/reconcile', {'action_id': str(action['id'])})
        assert retained.registry_revision is None
        actual = one(c, 'SELECT intent FROM lead.actions WHERE id=%s', (action['id'],))
        assert actual['intent'] == action['intent']
    assert not claim_request(actor, action['id'], claim, actor.epoch, request, observed)
    with tx(growth_agent) as c:
        assert one(c, 'SELECT state FROM lead.actions WHERE id=%s', (action['id'],))['state'] == 'unknown'
        assert one(c, 'SELECT count(*) n FROM lead.action_attempts')['n'] == 1


@pytest.mark.parametrize('raw', [
    '[]', '{"shared":null}', '{"shared":12}', '{"shared":[]}',
    '{"shared":"http://127.0.0.1:bad"}', '{"shared":"http://127.0.0.1:99999"}',
    '{"shared":"http://user:password@127.0.0.1:8089"}',
    '{"shared":"http://example.com:8089"}', '{"shared":"http://127.0.0.1:8089/path"}',
])
def test_registry_rejects_invalid_shape_and_non_loopback_endpoints(monkeypatch, raw):
    monkeypatch.setenv('LEAD_BROWSER_TRANSPORTS', raw)
    with pytest.raises(ValueError, match='invalid_transport_registry'):
        transports()
