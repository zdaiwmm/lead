"""Synthetic drill-name and restricted-role transport contracts; no database IO."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def transport(monkeypatch):
    path = Path(__file__).parents[1] / 'scripts/growth_runtime_upgrade.py'
    spec = importlib.util.spec_from_file_location('drill_role_transport', path)
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    state = SimpleNamespace(calls=[], closed=0, rolled_back=0, cleanups=0, forced_role=None)

    class Connection:
        @classmethod
        def connect(cls, conninfo='', **kwargs):
            state.calls.append((conninfo, kwargs))
            role = state.forced_role or kwargs.get('options', '').split(' -c role=')[-1]
            return SimpleNamespace(autocommit=False,
                execute=lambda query: SimpleNamespace(fetchone=lambda: {'role': role}),
                rollback=lambda: setattr(state, 'rolled_back', state.rolled_back + 1),
                close=lambda: setattr(state, 'closed', state.closed + 1))

    driver = SimpleNamespace(Connection=Connection, connect=Connection.connect)
    original = driver.connect
    monkeypatch.setitem(sys.modules, 'psycopg', driver)
    monkeypatch.setitem(sys.modules, 'lead_app.db_pool', SimpleNamespace(
        close_all=lambda: setattr(state, 'cleanups', state.cleanups + 1)))
    owner = {'host': '/synthetic/drill-socket', 'port': '25434',
             'user': 'synthetic_owner', 'password': 'synthetic-owner-password'}
    return helper, driver, original, owner, state


@pytest.mark.parametrize('prefix', ['lead_growth_drill_', 'lead_shared_drill_'])
@pytest.mark.parametrize('role', ['lead_auth', 'lead_runtime'])
def test_exact_drill_name_uses_owner_transport_but_verifies_restricted_role(transport, prefix, role):
    helper, driver, original, owner, state = transport
    name = prefix + '54f521f62edb'
    requested = {**owner, 'dbname': name, 'user': role, 'password': 'invalid-login',
                 'options': '-c statement_timeout=1000'}
    with helper.drill_role_connections(name, owner):
        driver.connect(**requested)
    assert state.calls == [('', {**requested, 'user': owner['user'], 'password': owner['password'],
                                'options': requested['options'] + ' -c role=' + role})]
    assert state.rolled_back == 1 and state.cleanups == 1 and state.closed == 0
    assert driver.connect is original


@pytest.mark.parametrize('name', ['lead', 'postgres', '', 'lead_shared_drill_',
    'lead_shared_drill_54f521f62ed', 'lead_shared_drill_54f521f62edbb',
    'lead_shared_drill_54F521f62edb', 'lead_shared_drill_54f521f62edg',
    'lead_unrelated_drill_54f521f62edb', 'xlead_shared_drill_54f521f62edb',
    'lead_shared_drill_54f521f62edb\n', 'lead_growth_drill_54f521f62edb_suffix'])
def test_other_database_names_are_rejected_before_transport_changes(transport, name):
    helper, driver, original, owner, state = transport
    with pytest.raises(ValueError, match='isolated drill database'):
        with helper.drill_role_connections(name, owner):
            pytest.fail('unapproved database name was admitted')
    assert state.calls == [] and state.cleanups == 0 and driver.connect is original


@pytest.mark.parametrize('prefix', ['lead_growth_drill_', 'lead_shared_drill_'])
@pytest.mark.parametrize('change', ['host', 'port', 'conninfo'])
def test_valid_name_does_not_admit_another_connection_target(transport, prefix, change):
    helper, driver, original, owner, state = transport
    name = prefix + '54f521f62edb'
    requested = {**owner, 'dbname': name, 'user': 'lead_runtime'}
    conninfo = ''
    if change == 'conninfo':
        conninfo = 'dbname=lead'
    else:
        requested[change] = 'unapproved-target'
    with pytest.raises(ValueError, match='connection target differs'):
        with helper.drill_role_connections(name, owner):
            driver.connect(conninfo, **requested)
    assert state.calls == [] and state.cleanups == 1 and driver.connect is original


@pytest.mark.parametrize('prefix', ['lead_growth_drill_', 'lead_shared_drill_'])
def test_wrong_effective_role_is_closed_and_connection_patch_is_restored(transport, prefix):
    helper, driver, original, owner, state = transport
    state.forced_role = owner['user']
    name = prefix + '54f521f62edb'
    with pytest.raises(ValueError, match='Restricted drill role was not applied'):
        with helper.drill_role_connections(name, owner):
            driver.connect(**{**owner, 'dbname': name, 'user': 'lead_runtime'})
    assert state.closed == 1 and state.rolled_back == 0 and state.cleanups == 1
    assert driver.connect is original


@pytest.mark.parametrize('prefix', ['lead_growth_drill_', 'lead_shared_drill_'])
@pytest.mark.parametrize('dbname,role', [('lead', 'lead_runtime'),
    ('lead_shared_drill_000000000000', 'lead_auth'), (None, 'synthetic_owner')])
def test_unrelated_connections_never_receive_owner_transport(transport, prefix, dbname, role):
    helper, driver, original, owner, state = transport
    requested = {**owner, 'dbname': dbname, 'user': role, 'password': 'unchanged'}
    with helper.drill_role_connections(prefix + '54f521f62edb', owner):
        driver.connect(**requested)
    assert state.calls == [('', requested)]
    assert state.rolled_back == 0 and state.cleanups == 1 and driver.connect is original
