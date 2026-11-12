"""Native grouping must preserve the project's existing executor and exit choices."""
from lead_app import account_profiles, reddit_login
from lead_app.execution_routes import default_transport, register, resolve


def test_different_registered_transports_do_not_share_a_native_profile(growth_agent, monkeypatch):
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    monkeypatch.setenv('LEAD_BROWSER_TRANSPORTS',
        '{"shared":"http://127.0.0.1:8089","reddit":"http://127.0.0.1:18091"}')
    monkeypatch.setenv('LEAD_BROWSER_ROUTE_DEFAULTS', '{"reddit":"reddit","default":"shared"}')
    first = account_profiles.for_platform(growth_agent, 'facebook', create=True)
    register(growth_agent, first['id'], default_transport('facebook'))
    second = account_profiles.for_platform(growth_agent, 'reddit', create=True)
    register(growth_agent, second['id'], default_transport('reddit'))

    assert first['id'] != second['id']
    assert resolve(growth_agent, account_id=first['account_id']).transport_ref == 'shared'
    assert resolve(growth_agent, account_id=second['account_id']).transport_ref == 'reddit'


def test_native_reddit_profile_preserves_registered_fixed_exit(growth_agent, monkeypatch):
    monkeypatch.setattr(account_profiles, 'native_available', lambda: True)
    monkeypatch.setenv('LEAD_REDDIT_IPV4_' + growth_agent.replace('-', '').upper(), '1.1.1.1')
    base = account_profiles.for_platform(growth_agent, 'telegram')
    native = account_profiles.for_platform(growth_agent, 'reddit', create=True)

    assert reddit_login.assign_exit(growth_agent, native) == {'server': reddit_login.SOCKET}
    assert native['network_identity_id'] != base['network_identity_id']
