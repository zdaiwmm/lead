"""Existing-account enablement is explicit, bound, and limited to that account."""

import pytest
from lead_app import domain
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def managed_pair():
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description) VALUES(%s,%s,1,'Language learning')",
            (current_tenant(), aid),
        )
        accounts = []
        for slot in ("facebook", "facebook-second"):
            p = one(
                c,
                """INSERT INTO lead.browser_profiles(tenant_id,agent_id,slot,provider,engine_version,sdk_version,config_ref,config_hash,storage_ref,key_material_ref,network_identity_id)
                SELECT tenant_id,agent_id,%s,provider,engine_version,sdk_version,config_ref,%s,%s,key_material_ref,network_identity_id
                FROM lead.browser_profiles WHERE agent_id=%s AND slot='default' RETURNING id""",
                (slot, uid(), "fixture:" + uid(), aid),
            )
            accounts.append(
                one(
                    c,
                    "INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,automation_enabled,external_account_id) VALUES(%s,%s,%s,'facebook','connected',false,%s) RETURNING *",
                    (current_tenant(), aid, p["id"], uid()),
                )
            )
    return aid, *accounts


def authorize(pair):
    from lead_app.channel_authority import authorize_managed_scope

    aid, a, _ = pair
    return authorize_managed_scope(aid, str(a["id"]), a["binding_version"], 1)


def test_login_does_not_authorize_managed_scope(managed_pair):
    from lead_app.channel_authority import managed_accounts

    aid, *_ = managed_pair
    with tx(aid) as c:
        assert managed_accounts(c, aid, "facebook") == []
        assert not one(c, "SELECT 1 FROM lead.tasks WHERE type='channel.clock'")


def test_managed_scope_does_not_enroll_sibling(managed_pair):
    from lead_app.channel_authority import managed_accounts

    aid, a, b = managed_pair
    assert authorize(managed_pair) == {"ok": True, "account_id": str(a["id"])}
    authorize(managed_pair)
    with tx(aid) as c:
        assert {row["id"] for row in managed_accounts(c, aid, "facebook")} == {a["id"]}
        sibling = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (b["id"],))
        assert (
            sibling["automation_enabled"] is False
            and sibling["binding_version"] == b["binding_version"]
        )
        assert (
            len(
                all_rows(
                    c,
                    "SELECT id FROM lead.tasks WHERE type='channel.clock' AND state='planned'",
                )
            )
            == 2
        )
        assert (
            len(
                all_rows(
                    c,
                    "SELECT id FROM lead.interaction_cards WHERE payload->>'source'='managed_target_binding'",
                )
            )
            == 1
        )


def test_no_double_owner_with_campaign_kernel(managed_pair, monkeypatch):
    from lead_app import channel_delivery, growth_experiments
    from lead_app.channel_authority import has_driver

    aid, a, b = managed_pair
    authorize(managed_pair)
    monkeypatch.setattr(growth_experiments, "enabled", lambda agent: True)
    with tx(aid) as c:
        agent = one(c, "SELECT * FROM lead.agents")
        assert has_driver(agent, "facebook")
        assert channel_delivery.owns(agent, "facebook")
        assert channel_delivery.owns(
            agent, "facebook", account_id=a["id"], connection=c
        )
        assert not channel_delivery.owns(
            agent, "facebook", account_id=b["id"], connection=c
        )


def test_rebinding_revokes_old_managed_scope(managed_pair):
    from lead_app.channel_authority import managed_accounts

    aid, a, b = managed_pair
    authorize(managed_pair)
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s",
            (a["id"],),
        )
        assert managed_accounts(c, aid, "facebook") == []
    with pytest.raises(ValueError):
        authorize(managed_pair)


def test_facebook_member_target_is_rejected(managed_pair):
    from lead_app.channels import store_target

    aid, a, b = managed_pair
    authorize(managed_pair)
    with pytest.raises(ValueError, match="普通成员"):
        store_target(
            aid,
            str(a["id"]),
            {
                "access_mode": "ordinary_member",
                "external_target_id": "member",
                "verified_permissions": ["publish"],
                "canonical_url": "https://www.facebook.com/groups/example",
                "display_name": "Members",
            },
        )


@pytest.mark.parametrize("change", ["control", "binding", "revoke"])
def test_submit_revalidates_managed_account(managed_pair, change):
    from lead_app.atomic_dispatch import connected_inputs
    from lead_app.channel_authority import managed_accounts

    aid, a, b = managed_pair
    authorize(managed_pair)
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE type='dispatch.channel'")
        if change == "control":
            c.execute(
                "UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
        elif change == "binding":
            c.execute(
                "UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s",
                (a["id"],),
            )
        else:
            c.execute(
                "UPDATE lead.interaction_cards SET state='canceled' WHERE payload->>'source'='managed_target_binding'"
            )
        assert connected_inputs(c, aid, task["id"], task["version"]) is None
        if change != "control":
            assert managed_accounts(c, aid, "facebook") == []


def test_research_revalidates_the_captured_account(managed_pair):
    from lead_app.channel_authority import research_binding_valid

    aid, a, b = managed_pair
    authorize(managed_pair)
    captured = {
        "platform": "facebook",
        "channel_account_id": str(a["id"]),
        "channel_account_binding": a["binding_version"],
        "channel_profile_id": str(a["profile_id"]),
    }
    with tx(aid) as c:
        assert research_binding_valid(c, aid, captured)
        c.execute(
            "UPDATE lead.social_accounts SET automation_enabled=true WHERE id=%s",
            (b["id"],),
        )
        c.execute(
            "UPDATE lead.interaction_cards SET state='canceled' WHERE payload->>'source'='managed_target_binding'"
        )
        assert not research_binding_valid(c, aid, captured)


def test_starting_one_account_preserves_sibling_settings(
    managed_pair, local_model_settings
):
    aid, a, b = managed_pair
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s", (aid,))
        c.execute(
            "UPDATE lead.social_accounts SET automation_enabled=true WHERE id=%s",
            (b["id"],),
        )
    authorize(managed_pair)
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT automation_enabled FROM lead.social_accounts WHERE id=%s",
                (b["id"],),
            )["automation_enabled"]
            is True
        )
