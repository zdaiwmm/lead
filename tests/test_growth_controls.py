import psycopg
import pytest
from fastapi.testclient import TestClient
from lead_app import domain
from lead_app.account_intents import set_intent
from lead_app.api import app
from lead_app.channels import disconnect_account
from lead_app.db import j, one, tx, uid
from lead_app.growth.runtime import advance
from lead_app.growth.tools import prepare_channel
from lead_app.security import token
from test_growth_account_connection import account
from test_growth_channel_operation import channel_work


def test_user_account_controls_are_canonical_and_replayable(growth_agent):
    sid = account(growth_agent)
    set_intent(growth_agent,sid,"running",source="owner_command",source_ref="owner:first")
    client = TestClient(app)
    client.cookies.set("lead_session",token("owner"))
    body = {"command_id":uid(),"kind":"account.pause","expected_revision":1,"payload":{"account_id":sid}}
    headers = {"Origin":"http://127.0.0.1:3100","X-Lead-Request":"1"}
    first = client.post(f"/api/agents/{growth_agent}/growth/commands",headers=headers,json=body)
    assert first.status_code==200,first.text
    assert client.post(f"/api/agents/{growth_agent}/growth/commands",headers=headers,json=body).json()==first.json()
    # A response can be lost after canonical commit but before the API's receipt.
    assert set_intent(growth_agent,sid,"paused",source="owner_command",source_ref="growth-user:"+body["command_id"],expected_revision=1)==2
    with pytest.raises(psycopg.errors.RaiseException,match="intent_revision"):
        set_intent(growth_agent,sid,"running",source="owner_command",source_ref="owner:stale",expected_revision=1)


def test_disconnect_revokes_intent_but_keeps_unknown_receipt(growth_agent):
    ref,data,social = channel_work(growth_agent)
    advance(ref)
    action = prepare_channel(growth_agent,ref["task_id"],data)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.social_accounts SET platform='facebook' WHERE id=%s",(social["id"],))
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action["id"],))
        c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action["id"],))
    disconnect_account(growth_agent,social["id"],social["verified_at"])
    with tx(growth_agent) as c:
        assert one(c,"SELECT desired FROM lead.account_operating_intents WHERE account_id=%s",(social["id"],))["desired"]=="revoked"
        assert one(c,"SELECT state FROM lead.actions WHERE id=%s",(action["id"],))["state"]=="unknown"


def test_pause_resume_can_prepare_without_account_or_model(growth_agent):
    with tx(growth_agent) as c:
        c.execute("""INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,facts,source_refs)
            VALUES(lead.ctx_tenant(),%s,1,'A supplied product',%s,'[]')""",(growth_agent,j({})))
    domain.set_control(growth_agent,"pause")
    domain.set_control(growth_agent,"resume")
    with tx(growth_agent) as c:
        assert one(c,"SELECT desired_state FROM lead.agents WHERE id=%s",(growth_agent,))["desired_state"]=="running"
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.research' AND state='planned'")["n"]==1
