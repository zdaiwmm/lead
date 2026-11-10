import pytest
from lead_app import account_profiles, domain, reddit_login
from lead_app.db import one, tx


def test_reddit_ipv4_assignment_is_scoped_persistent_and_repeatable(monkeypatch):
    aid = domain.create_agent()
    key = "LEAD_REDDIT_IPV4_" + aid.replace("-", "").upper()
    profile = account_profiles.for_platform(
        aid, "reddit", create=True, isolated_login=True
    )
    base = account_profiles.for_platform(aid, "telegram")
    assert profile["network_identity_id"] == base["network_identity_id"]
    assert reddit_login.assign_exit(aid, profile) is None
    monkeypatch.setenv(key, "35.72.111.159")
    assert reddit_login.assign_exit(aid, base) is None
    assert reddit_login.assign_exit(aid, profile) == {"server": reddit_login.SOCKET}
    assert reddit_login.assign_exit(aid, profile) == {"server": reddit_login.SOCKET}
    assert profile["network_identity_id"] != base["network_identity_id"]
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.network_identities")["n"] == 2
        assert (
            one(
                c,
                "SELECT network_identity_id FROM lead.browser_profiles WHERE slot='default'",
            )["network_identity_id"]
            == base["network_identity_id"]
        )
    monkeypatch.setenv(key, "1.1.1.1")
    with pytest.raises(ValueError, match="绑定已变化"):
        reddit_login.assign_exit(aid, profile)
    monkeypatch.delenv(key)
    with pytest.raises(ValueError, match="尚未配置"):
        reddit_login.assign_exit(aid, account_profiles.for_platform(aid, "reddit"))
