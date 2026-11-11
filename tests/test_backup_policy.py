from types import SimpleNamespace

import pytest
from lead_app import backups, browser, managed_network, resources


@pytest.mark.parametrize('policy,local,offsite,expected', [
    (None, True, False, False),
    ('offsite', False, True, True),
    ('verified_local', True, False, True),
    ('verified_local', False, True, False),
])
def test_write_recovery_uses_only_selected_operator_policy(monkeypatch, policy, local, offsite, expected):
    monkeypatch.setattr(backups, 'env', lambda: {'LEAD_BACKUP_POLICY': policy})
    queries = []

    def query(conn, statement):
        queries.append(statement)
        return {'ready': local if 'local_restore_ready()' in statement else offsite}

    monkeypatch.setattr(backups, 'one', query)
    assert backups.write_backup_ready(object()) is expected
    assert ('local_restore_ready()' in queries[0]) is (policy == 'verified_local')


def test_unknown_backup_policy_is_rejected_before_query(monkeypatch):
    monkeypatch.setattr(backups, 'env', lambda: {'LEAD_BACKUP_POLICY': 'disabled'})
    monkeypatch.setattr(backups, 'one', lambda *args: pytest.fail('invalid policy reached database'))
    with pytest.raises(ValueError, match='备份策略'):
        backups.write_backup_ready(object())


@pytest.mark.parametrize('policy,local,offsite,passes_backup', [
    ('verified_local', True, False, True),
    ('verified_local', False, True, False),
    ('offsite', True, False, False),
])
def test_browser_write_gate_keeps_existing_recovery_policy(monkeypatch, policy, local, offsite, passes_backup):
    values = {'LEAD_DEPLOYMENT_MODE': 'production', 'LEAD_BACKUP_POLICY': policy}
    monkeypatch.setattr(browser, 'env', lambda: values)
    monkeypatch.setattr(backups, 'env', lambda: values)
    monkeypatch.setattr(managed_network, 'policy', lambda aid: None)
    monkeypatch.setattr(resources, 'ensure_capacity', lambda *args: None)

    def query(conn, statement, *args):
        if 'recovery_control' in statement:
            return {'state': 'normal'}
        if 'profile_maintenance' in statement:
            return None
        if 'local_restore_ready()' in statement:
            return {'ready': local}
        if 'offsite_backup_ready()' in statement:
            return {'ready': offsite}
        pytest.fail('unexpected query: ' + statement)

    monkeypatch.setattr(browser, 'one', query)
    monkeypatch.setattr(backups, 'one', query)

    class FollowingAuthorizationCheck(Exception):
        pass

    def require_agent(*args):
        raise FollowingAuthorizationCheck

    monkeypatch.setattr(browser, 'require_agent', require_agent)
    actor = SimpleNamespace(aid='fixture', profile={'slot': 'default'})
    if passes_backup:
        with pytest.raises(FollowingAuthorizationCheck):
            browser.Actor.gate(actor, object(), {'intent': {'platform': 'telegram'}})
    else:
        with pytest.raises(ValueError, match='数据库恢复点'):
            browser.Actor.gate(actor, object(), {'intent': {'platform': 'telegram'}})
