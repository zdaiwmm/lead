"""A verification request must stay with its account's active login window."""

import pytest
from fastapi import HTTPException
from lead_app import account_profiles, api, domain
from lead_app.db import one, tx, uid
from lead_app.scope import current_tenant


def takeover(aid, profile):
    with tx(aid) as c:
        version = one(c, "SELECT control_version FROM lead.agents")["control_version"]
        session = one(
            c,
            """INSERT INTO lead.browser_sessions
            (tenant_id,agent_id,profile_id,node_id,actor_id,epoch,control_version,state)
            VALUES(%s,%s,%s,'fixture','fixture',1,%s,'ready') RETURNING id""",
            (current_tenant(), aid, profile["id"], version),
        )
        return one(
            c,
            """INSERT INTO lead.takeover_sessions
            (tenant_id,agent_id,profile_id,browser_session_id,token_hash,control_version,state,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,'active',now()+interval '10 minutes') RETURNING id""",
            (current_tenant(), aid, profile["id"], session["id"], uid(), version),
        )["id"]


@pytest.mark.parametrize("operation", ["challenge/status", "challenge/verify"])
@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "expired",
        "ended",
        "other_agent",
        "wrong_platform",
        "default_profile",
        "unbound",
    ],
)
def test_invalid_challenge_window_is_rejected_before_forwarding(
    monkeypatch, operation, case
):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS", aid)
    profile = account_profiles.for_platform(
        aid, "facebook", create=True, isolated_login=True
    )
    sid = takeover(aid, profile)
    with tx(aid) as c:
        if case == "expired":
            c.execute(
                "UPDATE lead.takeover_sessions SET expires_at=now()-interval '1 second' WHERE id=%s",
                (sid,),
            )
        elif case == "ended":
            c.execute(
                "UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE id=%s",
                (sid,),
            )
        elif case == "unbound":
            # A profile without the matching platform account cannot route verification.
            c.execute(
                "UPDATE lead.social_accounts SET platform='x' WHERE profile_id=%s",
                (profile["id"],),
            )
    body = {
        "platform": "x" if case == "wrong_platform" else "facebook",
        "takeover": str(sid),
    }
    if case == "missing":
        body.pop("takeover")
    elif case == "other_agent":
        aid = domain.create_agent()
        monkeypatch.setenv("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS", aid)
    elif case == "default_profile":
        body["takeover"] = str(
            takeover(aid, account_profiles.for_platform(aid, "telegram"))
        )
    with tx(aid) as c, pytest.raises(HTTPException) as rejected:
        api.login_browser_port(c, aid, f"/agents/{aid}/{operation}", body)
    assert rejected.value.status_code == 409


@pytest.mark.parametrize("operation", ["challenge/status", "challenge/verify"])
def test_default_telegram_challenge_keeps_its_executor(monkeypatch, operation):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_SPLIT_FACEBOOK_X_LOGIN_AGENT_IDS", aid)
    profile = account_profiles.for_platform(aid, "telegram")
    sid = takeover(aid, profile)
    with tx(aid) as c:
        assert (
            api.login_browser_port(
                c,
                aid,
                f"/agents/{aid}/{operation}",
                {
                    "platform": "telegram",
                    "takeover": str(sid),
                },
            )
            == 8089
        )
