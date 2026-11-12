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


def test_facebook_and_x_opt_into_same_exit_with_separate_profiles(monkeypatch):
    aid = domain.create_agent()
    profiles = {
        channel: account_profiles.for_platform(
            aid, channel, create=True, isolated_login=True
        )
        for channel in ("facebook", "x", "reddit")
    }
    assert len({p["id"] for p in profiles.values()}) == 3
    facebook, x, reddit = (profiles[channel] for channel in ("facebook", "x", "reddit"))
    assert reddit_login.assign_exit(aid, x) is None
    monkeypatch.setenv("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS", aid)
    with pytest.raises(ValueError, match="固定出口尚未配置"):
        reddit_login.assign_exit(aid, x)
    monkeypatch.setenv("LEAD_REDDIT_IPV4_" + aid.replace("-", "").upper(), "1.1.1.1")
    default = account_profiles.for_platform(aid, "telegram")
    for profile in (facebook, x, reddit):
        assert reddit_login.assign_exit(aid, profile) == {"server": reddit_login.SOCKET}
    assert (
        facebook["network_identity_id"]
        == x["network_identity_id"]
        == reddit["network_identity_id"]
    )
    assert default["network_identity_id"] != x["network_identity_id"]
    assert reddit_login.assign_exit(aid, default) is None
    monkeypatch.delenv("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS")
    with pytest.raises(ValueError, match="出口已停用"):
        reddit_login.assign_exit(aid, x)


def test_public_research_uses_only_its_registered_project_exit(monkeypatch):
    first,second = domain.create_agent(),domain.create_agent()
    public = account_profiles.public_research(first)
    other = account_profiles.public_research(second)
    base = account_profiles.for_platform(first,"telegram")
    assert reddit_login.assign_exit(first,public) is None
    monkeypatch.setenv("LEAD_REDDIT_IPV4_"+first.replace("-","").upper(),"1.1.1.1")
    login = account_profiles.for_platform(first,"reddit",create=True,isolated_login=True)
    reddit_login.assign_exit(first,login)
    assert reddit_login.assign_exit(first,public)=={"server":reddit_login.SOCKET}
    assert public["network_identity_id"]==login["network_identity_id"]
    assert public["id"]!=login["id"] and public["storage_ref"]!=login["storage_ref"]
    assert account_profiles.for_platform(first,"telegram")["network_identity_id"]==base["network_identity_id"]
    assert reddit_login.assign_exit(second,other) is None
    with pytest.raises(ValueError):
        reddit_login.assign_exit(first,other)
    monkeypatch.setenv("LEAD_REDDIT_IPV4_"+first.replace("-","").upper(),"8.8.8.8")
    with pytest.raises(ValueError,match="绑定已变化"):
        reddit_login.assign_exit(first,public)


def test_public_research_never_invents_or_silently_replaces_an_exit(monkeypatch):
    aid = domain.create_agent()
    public = account_profiles.public_research(aid)
    monkeypatch.setenv("LEAD_REDDIT_IPV4_"+aid.replace("-","").upper(),"1.1.1.1")
    with pytest.raises(ValueError):
        reddit_login.assign_exit(aid,public)
    with tx(aid) as c:
        assert not one(c,"SELECT id FROM lead.network_identities WHERE provider_ref=%s",(reddit_login.PROVIDER,))
