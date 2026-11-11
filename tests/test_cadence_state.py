from datetime import datetime, timedelta, timezone

import pytest
from lead_app import domain
from lead_app.db import audit, j, one, tx, uid
from lead_app.scope import current_tenant, service_scope


@pytest.fixture
def cadence_agent():
    aid = domain.create_agent()
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running',onboarding_state=%s WHERE id=%s",
                  (j({"community_growth": {"enabled": True, "mode": "contextual_outreach", "allow_promotion": False},
                      "telegram_executor": {"execution_cadence_protocol": 1, "cadence_policy_version": 1}}), aid))
        profile = one(c, "SELECT id FROM lead.browser_profiles")["id"]
        account = one(c, """INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled)
            VALUES(%s,%s,%s,'telegram',%s,'connected',true) RETURNING *""", (current_tenant(), aid, profile, uid()))
        candidate = {"telegram_url": "https://t.me/cadence_fixture", "name": "Language exchange", "selection_basis": "observe_recent_chat"}
        disc = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,completed_at,scheduled_at,workflow_id)
            VALUES(%s,%s,'community.discover','succeeded',%s,%s,now(),now(),gen_random_uuid()::text) RETURNING id""",
                   (current_tenant(), aid, j({"account_id": str(account["id"]), "external_account_id": account["external_account_id"], "config_revision": 1}), j({"candidates": [candidate]})))
        candidate["discovery_task_id"] = str(disc["id"])
        card = one(c, """INSERT INTO lead.interaction_cards(tenant_id,agent_id,type,state,payload,expires_at)
            VALUES(%s,%s,'manage_community','accepted',%s,now()+interval '1 day') RETURNING id""",
                   (current_tenant(), aid, j({"purpose": "bounded_candidate_participation", "account_id": str(account["id"]),
                    "external_account_id": account["external_account_id"], "allowed_actions": ["read_recent_history", "join_public_group"], "discovery_task": str(disc["id"]), "continue_discovery": True})))
        old = one(c, """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,completed_at,scheduled_at,workflow_id)
            VALUES(%s,%s,'community.engage','succeeded',%s,%s,now(),now(),gen_random_uuid()::text) RETURNING *""",
                  (current_tenant(), aid, j({"account_id": str(account["id"]), "external_account_id": account["external_account_id"],
                    "control_version": 1, "config_revision": 1, "delegation_id": str(card["id"]), "candidate": candidate}),
                   j({"outcome": {"state": "blocked", "reason": "daily_participation_limit"}})))
    return aid, account, card, old


def attest(c, aid, origin):
    audit(c, aid, "cadence.parameter_origins_attested", {"config_revision": 1, "config_snapshot": one(c,"SELECT onboarding_state FROM lead.agents WHERE id=%s",(aid,))["onboarding_state"]["community_growth"], "origins": {
        "combined_per_24h": {"origin": origin, "value": 1, "evidence_ref": "reviewed-release:fixture"}}})


def test_explicit_one_is_not_inferred_as_default(cadence_agent):
    from lead_app.cadence_state import preview_migration

    aid, *_ = cadence_agent
    with service_scope(), tx(aid) as c:
        attest(c, aid, "owner_explicit")
        item = next(x for x in preview_migration(c, aid, expected_control=1)["items"] if x["parameter"] == "combined_per_24h")
        assert item["origin"] == "owner_explicit"
        assert item["disposition"] == "preserve"


def test_missing_history_preserves_unknown_limit(cadence_agent):
    from lead_app.cadence_state import preview_migration

    aid, *_ = cadence_agent
    with tx(aid) as c:
        preview = preview_migration(c, aid, expected_control=1)
        assert all(x["origin"] == "unresolved" for x in preview["items"])


def activate_default(aid):
    from lead_app.cadence_state import activate, preview_migration

    with service_scope(), tx(aid) as c:
        attest(c, aid, "verified_system_default")
        preview = preview_migration(c, aid, expected_control=1)
        activate(c, aid, preview["preview_digest"], 1)


def test_activate_rejects_changed_preview(cadence_agent):
    from lead_app.cadence_state import activate, preview_migration

    aid, *_ = cadence_agent
    with service_scope(), tx(aid) as c:
        preview = preview_migration(c, aid, expected_control=1)
        c.execute("UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s", (aid,))
        with pytest.raises(ValueError):
            activate(c, aid, preview["preview_digest"], 1)


def test_old_default_join_does_not_consume_reply(cadence_agent):
    from lead_app.cadence_state import participation_due

    aid, *_ = cadence_agent
    activate_default(aid)
    now = datetime.now(timezone.utc)
    with tx(aid) as c:
        assert participation_due(c, aid, "reply", [("join", now-timedelta(minutes=1))], now) is None


def test_owner_combined_limit_is_preserved(cadence_agent):
    from lead_app.cadence_state import activate, participation_due, preview_migration

    aid, *_ = cadence_agent
    now = datetime.now(timezone.utc)
    with service_scope(), tx(aid) as c:
        attest(c, aid, "owner_explicit")
        preview = preview_migration(c, aid, expected_control=1)
        activate(c, aid, preview["preview_digest"], 1)
        assert participation_due(c, aid, "reply", [("join", now-timedelta(minutes=1))], now)["reason"] == "daily_participation_limit"


def test_resume_is_idempotent_and_original_result_is_unchanged(cadence_agent):
    from lead_app.cadence_state import resume_eligible

    aid, _, _, old = cadence_agent
    activate_default(aid)
    with tx(aid) as c:
        first = resume_eligible(c, aid, policy_version=1)
        second = resume_eligible(c, aid, policy_version=1)
        assert len(first) == 1
        assert first == second
        assert first[0] != str(old["id"])
        current = one(c, "SELECT input,result FROM lead.tasks WHERE id=%s", (old["id"],))
        assert current["result"] == old["result"]
        assert current["input"] == old["input"]


@pytest.mark.parametrize("change", ["unknown", "binding", "grant"])
def test_changed_binding_or_revoked_grant_cannot_resume(cadence_agent, change):
    from lead_app.cadence_state import resume_eligible

    aid, account, card, old = cadence_agent
    activate_default(aid)
    with tx(aid) as c:
        if change == "unknown":
            c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s", (j({"reply_state": "unknown"}), old["id"]))
        elif change == "binding":
            c.execute("UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s", (account["id"],))
        else:
            c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s", (card["id"],))
        assert resume_eligible(c, aid, policy_version=1) == []


def test_strategy_edit_invalidates_active_preview(cadence_agent):
    from lead_app.community_growth import set_strategy

    aid, *_ = cadence_agent
    activate_default(aid)
    set_strategy(aid, {"participation_policy": {"combined_per_24h": 1}})
    with tx(aid) as c:
        state = one(c, "SELECT onboarding_state FROM lead.agents")["onboarding_state"]
        assert state["execution_cadence"]["mode"] == "paused"
        event = one(c, "SELECT payload FROM lead.audit_events WHERE type='community.strategy_updated' ORDER BY occurred_at DESC LIMIT 1")
        assert event["payload"]["_explicit_policy_fields"] == ["combined_per_24h"]


@pytest.mark.parametrize("change", ["protocol", "anchor_account", "strategy_snapshot", "other_unknown"])
def test_resume_revalidates_full_scope(cadence_agent, change):
    from lead_app.cadence_state import participation_due, resume_eligible

    aid, account, card, old = cadence_agent
    activate_default(aid)
    with tx(aid) as c:
        if change == "protocol":
            c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{telegram_executor,execution_cadence_protocol}','0') WHERE id=%s", (aid,))
        elif change == "anchor_account":
            anchor = one(c, "INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,completed_at,scheduled_at,workflow_id,created_at) VALUES(%s,%s,'community.discover','succeeded',%s,'{}',now(),now(),gen_random_uuid()::text,now()-interval '1 hour') RETURNING id", (current_tenant(), aid, j({"account_id": uid(), "external_account_id": "wrong", "config_revision": 1})))
            c.execute("UPDATE lead.interaction_cards SET payload=jsonb_set(payload,'{discovery_task}',%s) WHERE id=%s", (j(str(anchor["id"])), card["id"]))
        elif change == "strategy_snapshot":
            c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{community_growth,participation_policy}',%s) WHERE id=%s", (j({"combined_per_24h": 0}), aid))
            assert participation_due(c, aid, "reply", [], account_id=account["id"])["reason"] == "cadence_version_mismatch"
        else:
            c.execute("INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,scheduled_at,workflow_id) VALUES(%s,%s,'community.engage','failed','{}',%s,now(),gen_random_uuid()::text)", (current_tenant(), aid, j({"reply_state": "unknown"})))
        assert resume_eligible(c, aid, policy_version=1) == []


def test_shadow_version_is_the_version_activated(cadence_agent):
    from lead_app.cadence_state import activate, prepare_shadow, preview_migration
    aid,*_=cadence_agent
    with service_scope(), tx(aid) as c:
        attest(c,aid,"verified_system_default")
        preview=preview_migration(c,aid,expected_control=1)
        shadow=prepare_shadow(c,aid,preview["preview_digest"],1)
        assert shadow["mode"]=="shadow" and shadow["policy_version"]==1
        active=activate(c,aid,preview["preview_digest"],1)
        assert active["policy_version"]==shadow["policy_version"]


def test_policy_decision_keeps_quota_and_interval_evidence(cadence_agent):
    from lead_app.cadence_state import participation_decision
    aid,account,*_=cadence_agent
    now=datetime.now(timezone.utc)
    with service_scope(),tx(aid) as c:
        attest(c,aid,"owner_explicit")
        decision=participation_decision(c,aid,"reply",[("reply",now-timedelta(minutes=1))],now,account_id=str(account['id']))
        assert decision.state=='waiting_time'
        assert len(decision.constraints)==2
        assert decision.next_at==now+timedelta(hours=24)-timedelta(minutes=1)
        assert {item.scope.account_id for item in decision.constraints}=={str(account['id'])}
