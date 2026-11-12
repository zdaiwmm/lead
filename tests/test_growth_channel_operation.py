from datetime import timedelta
from types import SimpleNamespace

import pytest
from lead_app import domain
from lead_app.account_intents import set_intent
from lead_app.db import j, one, tx
from lead_app.execution_routes import register
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import digest, submit_work
from lead_app.growth.tools import ChannelInput, prepare_channel, submission_gate
from test_growth_runtime import new_work


def channel_work(aid,entry=None):
    new_work(aid)
    domain.connect_lab(aid)
    with tx(aid) as c:
        account = one(c,"SELECT * FROM lead.social_accounts WHERE platform='lab'")
        target = one(c,"SELECT * FROM lead.account_targets WHERE account_id=%s",(account["id"],))
        body = {"body":"A real synthetic fixture publication","title":"","format":"text","media":[]}
        if entry:
            body["link"] = entry
        content = one(c,"""INSERT INTO lead.content_items(tenant_id,agent_id,revision,body,source_refs,validation_result,content_hash)
            VALUES(lead.ctx_tenant(),%s,1,%s,'[]','{}',%s) RETURNING *""",(aid,j(body),digest(body)))
    set_intent(aid,account["id"],"running",source="owner_command",source_ref="owner:connect-lab")
    register(aid,account["profile_id"])
    data = ChannelInput(content_id=content["id"],content_hash=content["content_hash"])
    with tx(aid) as c:
        ref = submit_work(c,aid,TaskSpec(kind="channel",purpose="publish",account_id=account["id"],target_id=target["id"],
            payload=data.model_dump(mode="json"),review_at=utcnow()+timedelta(hours=1)))
    return ref,data,account


def test_macro_operation_identity_is_immutable(growth_agent):
    ref,data,account = channel_work(growth_agent)
    claim = advance(ref)["claim"]
    action = prepare_channel(growth_agent,ref["task_id"],data)
    again = prepare_channel(growth_agent,ref["task_id"],data)
    assert action["id"] == again["id"] and action["operation_key"] == again["operation_key"]
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action["id"],))
    finish(claim,WorkResult(outcome="failed",failure_code="ReadTimeout"))
    with tx(growth_agent) as c:
        actual = one(c,"SELECT state,result FROM lead.tasks WHERE id=%s",(ref["task_id"],))
        assert actual["state"] == "reconciling"
        assert one(c,"SELECT count(*) n FROM lead.actions WHERE task_id=%s",(ref["task_id"],))["n"] == 1


def test_submission_gate_rechecks_intent_and_ignores_unrelated_config(growth_agent):
    ref,data,account = channel_work(growth_agent)
    claim=advance(ref)["claim"]
    action = prepare_channel(growth_agent,ref["task_id"],data)
    actor = SimpleNamespace(aid=growth_agent,profile={"id":account["profile_id"]},epoch=1)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.profile_leases SET epoch=1,state='held',expires_at=now()+interval '10 minutes' WHERE profile_id=%s",(account["profile_id"],))
        c.execute("""INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version)
            SELECT tenant_id,agent_id,revision+1,provider,model_ref,prompt_version,toolset_version,output_schema_version
            FROM lead.agent_revisions ORDER BY revision DESC LIMIT 1""")
        c.execute("UPDATE lead.agents SET config_revision=config_revision+1 WHERE id=%s",(growth_agent,))
        submission_gate(c,actor,action,claim)
    set_intent(growth_agent,account["id"],"paused",source="owner_command",source_ref="owner:pause")
    with pytest.raises(ValueError,match="scope_changed"):
        with tx(growth_agent) as c:
            submission_gate(c,actor,action,claim)


def test_model_result_without_actual_receipt_cannot_complete_channel(growth_agent):
    ref,data,_ = channel_work(growth_agent)
    claim = advance(ref)["claim"]
    prepare_channel(growth_agent,ref["task_id"],data)
    finish(claim,WorkResult(outcome="completed",output={"claimed_publish":True}))
    with tx(growth_agent) as c:
        assert one(c,"SELECT state FROM lead.tasks WHERE id=%s",(ref["task_id"],))["state"] == "failed"


def test_stale_browser_request_cannot_borrow_replacement_attempt_lease(growth_agent):
    from lead_app.growth.runtime import recover_due
    ref,data,account=channel_work(growth_agent)
    original_claim=advance(ref)["claim"]
    action=prepare_channel(growth_agent,ref["task_id"],data)
    future=utcnow()+timedelta(minutes=10)
    recover_due(growth_agent,now=future)
    replacement=advance(ref,now=future)["claim"]
    assert replacement["attempt_token"]!=original_claim["attempt_token"]
    actor=SimpleNamespace(aid=growth_agent,profile={"id":account["profile_id"]},epoch=1)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.profile_leases SET epoch=1,state='held',expires_at=now()+interval '1 hour' WHERE profile_id=%s",(account["profile_id"],))
    with pytest.raises(ValueError,match="stale_submission_claim"):
        with tx(growth_agent) as c:
            submission_gate(c,actor,action,original_claim)
    with tx(growth_agent) as c:
        submission_gate(c,actor,action,replacement)
        assert one(c,"SELECT count(*) n FROM lead.actions")["n"]==1
