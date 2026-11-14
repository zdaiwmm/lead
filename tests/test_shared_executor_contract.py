"""Pure shared-executor boundary contracts; no model, account or DB traffic."""
from uuid import uuid4

import pytest
from lead_app import admin_config, admin_settings
from lead_app import local_executor as executor
from lead_app import local_executor_identity as identity
from lead_app.scope import current_tenant, service_scope, tenant_scope


def test_shared_execution_default_is_explicit_and_inactive_until_published():
    assert admin_config.parse_key('execution/default') == ('execution', None, None, None)
    assert admin_settings.defaults('execution') == {'source': 'legacy', 'node_id': None}
    node = str(uuid4())
    assert admin_config.validate('execution/default', {'source': 'managed_node', 'node_id': node}) == {
        'source': 'managed_node', 'node_id': node}
    for body in ({'source': 'api'}, {'source': 'managed_node', 'token': 'not-a-policy-field'},
                 {'source': 'managed_node', 'node_id': 'not-a-node'}):
        with pytest.raises(ValueError):
            admin_config.validate('execution/default', body)


def test_old_scope_missing_route_is_personal_and_managed_requires_server_grant():
    source = getattr(executor, 'execution_source', None)
    assert callable(source), 'Immutable execution-source decoder is missing'
    assert source({}) == 'personal_v1'
    assert source({'execution_route': {'source': 'personal_v1'}}) == 'personal_v1'
    assert source({'execution_route': {'source': 'managed_node', 'grant_id': str(uuid4()),
        'policy_revision': 'execution/default@1'}}) == 'managed_node'
    for scope in ({'execution_route': {'source': 'managed_node'}},
                  {'execution_route': {'source': 'legacy'}}, {'execution_route': 'managed_node'}):
        with pytest.raises(ValueError):
            source(scope)


def test_studio_none_is_not_a_system_job_even_under_service_scope():
    capture = getattr(identity, 'capture_studio', None)
    assert callable(capture), 'Studio requires its own real-actor authority'
    with tenant_scope(current_tenant(), None), service_scope(), pytest.raises(ValueError, match='studio_actor'):
        capture(None, 'website_brief')


@pytest.mark.parametrize('code,status', [('executor_node_unavailable', 401),
    ('executor_node_epoch_changed', 409), ('executor_epoch_changed', 409),
    ('executor_scope_generation_changed', 409), ('executor_scope_forbidden', 403),
    ('executor_not_ready', 503)])
def test_node_and_scope_failure_codes_preserve_their_distinct_stop_boundary(code, status):
    from lead_app.executor_nodes_api import _error
    failure = _error(ValueError(code))
    assert failure.status_code == status and failure.detail == code
