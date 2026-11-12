"""Original action admission distinguishes the current account's connection."""
import pytest
from lead_app.db import one, tx
from lead_app.growth.tools import submission_gate
from test_browser_action_transactions import native_work
from test_growth_account_connection import login_work


def test_native_other_account_connection_does_not_block_action_but_same_account_does(growth_agent):
    actor,action,claim,_,_,social=native_work(growth_agent)
    with tx(growth_agent) as c:
        other=one(c,"""INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,connection_origin)
            VALUES(lead.ctx_tenant(),%s,%s,'web','pending','https://different-site.test') RETURNING id""",
            (growth_agent,social['profile_id']))
    login_work(growth_agent,other['id'])
    with tx(growth_agent) as c:
        submission_gate(c,actor,action,claim)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.takeover_sessions SET state='ended',ended_at=now() WHERE state='active'")
    login_work(growth_agent,social['id'])
    with pytest.raises(ValueError,match='owner_controls_profile'):
        with tx(growth_agent) as c:
            submission_gate(c,actor,action,claim)
