from datetime import timedelta

import psycopg
import pytest
from lead_app import domain
from lead_app.account_intents import set_intent
from lead_app.db import one, tx, uid
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


def account(aid):
    domain.connect_lab(aid)
    with tx(aid) as c:
        return str(one(c, "SELECT id FROM lead.social_accounts WHERE platform='lab'")["id"])


def test_owner_pause_survives_reconnection(growth_agent):
    sid = account(growth_agent)
    assert set_intent(growth_agent, sid, "running", source="owner_command", source_ref="command:first") == 1
    assert set_intent(growth_agent, sid, "paused", source="owner_command", source_ref="command:pause", expected_revision=1) == 2
    assert set_intent(growth_agent, sid, "paused", source="owner_command", source_ref="command:pause") == 2
    with tx(growth_agent) as c:
        assert one(c, "SELECT desired FROM lead.account_operating_intents")["desired"] == "paused"


def test_runtime_cannot_forge_intent(growth_agent):
    sid = account(growth_agent)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tx(growth_agent) as c:
            c.execute("""INSERT INTO lead.account_operating_intents(account_id,tenant_id,agent_id,desired,source_kind,source_event_ref)
              VALUES(%s,lead.ctx_tenant(),%s,'running','connection','forged')""", (sid, growth_agent))
    with pytest.raises(psycopg.errors.RaiseException, match="connection_source_unverified"):
        set_intent(growth_agent,sid,"running",source="connection",source_ref="forged:connection")


def login_work(aid, account_id):
    from lead_app.execution_routes import register
    with tx(aid) as c:
        social = one(c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account_id,))
        session = one(c,"SELECT id FROM lead.browser_sessions WHERE profile_id=%s AND ended_at IS NULL",(social["profile_id"],))
        if not session:
            session = one(c, """INSERT INTO lead.browser_sessions(tenant_id,agent_id,profile_id,node_id,actor_id,epoch,
                control_version,state) VALUES(lead.ctx_tenant(),%s,%s,'synthetic','test',1,1,'ready') RETURNING id""", (aid,social["profile_id"]))
        takeover = one(c, """INSERT INTO lead.takeover_sessions(tenant_id,agent_id,browser_session_id,profile_id,
            token_hash,control_version,purpose,state,expires_at) VALUES(lead.ctx_tenant(),%s,%s,%s,%s,1,'login','active',now()+interval '30 minutes') RETURNING *""",
            (aid,session["id"],social["profile_id"],uid()))
        ref = submit_work(c, aid, TaskSpec(kind="account", purpose="connect", payload={"takeover_id":str(takeover["id"]),
            "profile_id":str(social["profile_id"]),"connection_account_id":str(account_id)}, review_at=utcnow()+timedelta(minutes=30)))
        row = one(c,"SELECT * FROM lead.tasks WHERE id=%s", (ref["task_id"],))
    route = register(aid, social["profile_id"])
    return row, {**takeover,"route_generation":route.route_generation,"binding_version":social["binding_version"]}, social


def test_background_connection_is_atomic_and_idempotent(growth_agent):
    from lead_app.account_connection import complete
    sid = account(growth_agent)
    row,snapshot,social = login_work(growth_agent,sid)
    observation = {"status":"authenticated","operating_subject":{"namespace":"lab","external_id":social["external_account_id"],
        "id_kind":"stable"},"evidence_refs":["synthetic:self-session"]}
    assert complete(growth_agent,row,snapshot,observation) == sid
    set_intent(growth_agent,sid,"paused",source="owner_command",source_ref="owner:pause")
    assert complete(growth_agent,row,snapshot,observation) == sid
    with tx(growth_agent) as c:
        assert one(c,"SELECT binding_version FROM lead.social_accounts WHERE id=%s",(sid,))["binding_version"] == social["binding_version"]
        assert one(c,"SELECT desired FROM lead.account_operating_intents")["desired"] == "paused"
        assert one(c,"SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.account_connected'")["n"] == 1


def test_ime_last_input_prevents_early_completion(growth_agent):
    from lead_app.account_connection import complete
    sid = account(growth_agent)
    row,snapshot,social = login_work(growth_agent,sid)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET input_seq=input_seq+1,composing=true WHERE id=%s",(snapshot["id"],))
    with pytest.raises(psycopg.errors.RaiseException, match="connection_changed"):
        complete(growth_agent,row,snapshot,{"status":"authenticated","operating_subject":{"namespace":"lab",
            "external_id":social["external_account_id"],"id_kind":"stable"},"evidence_refs":["synthetic:self-session"]})
    with tx(growth_agent) as c:
        assert one(c,"SELECT completed_account_id FROM lead.takeover_sessions WHERE id=%s",(snapshot["id"],))["completed_account_id"] is None


def test_handle_case_is_alias_but_unproved_rename_does_not_rebind(growth_agent):
    from lead_app.account_connection import complete
    sid = account(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.social_accounts SET external_account_id='KnownOwner' WHERE id=%s",(sid,))
    row,snapshot,social = login_work(growth_agent,sid)
    observation = {"status":"authenticated","operating_subject":{"namespace":"lab","external_id":"knownowner",
        "id_kind":"handle"},"evidence_refs":["synthetic:self-header"]}
    set_intent(growth_agent,sid,"paused",source="owner_command",source_ref="owner:existing-pause")
    assert complete(growth_agent,row,snapshot,observation)==sid
    row,snapshot,social = login_work(growth_agent,sid)
    observation["operating_subject"]["external_id"] = "anothername"
    with pytest.raises(psycopg.errors.RaiseException,match="connection_identity_ambiguous"):
        complete(growth_agent,row,snapshot,observation)
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.social_accounts")["n"]==1
        assert one(c,"SELECT desired FROM lead.account_operating_intents")["desired"]=="paused"
