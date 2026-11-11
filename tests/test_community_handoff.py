"""Qualified partial checkpoints retain their original bounded delegation."""
import pytest
from test_cadence_state import cadence_agent
from lead_app.db import all_rows, j, one, tx, uid
from lead_app.scope import current_tenant


@pytest.fixture
def partial(cadence_agent):
    aid, account, card, old = cadence_agent
    with tx(aid) as c:
        anchor=old["input"]["candidate"]["discovery_task_id"]
        c.execute("UPDATE lead.tasks SET created_at=now()-interval '1 hour' WHERE id=%s",(anchor,))
        candidate={**old["input"]["candidate"],"rules_quote":"Questions about language exchange are welcome.","evidence_id":uid(),"reading_need":"Language exchange discussions", "selection_basis":"observe_recent_chat"}
        candidate.pop("discovery_task_id")
        batch=one(c,"INSERT INTO lead.tasks(tenant_id,agent_id,type,state,input,result,scheduled_at,started_at,workflow_id) VALUES(%s,%s,'community.discover','running',%s,%s,now(),now(),gen_random_uuid()::text) RETURNING *",(current_tenant(),aid,j({"account_id":str(account["id"]),"external_account_id":account["external_account_id"],"config_revision":1,"control_version":1}),j({"candidates":[candidate],"phase":"collect"})))
        # An unrelated previously finished target must not consume this new one.
        candidate["telegram_url"]="https://t.me/partial_fixture"
        c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{candidates}',%s) WHERE id=%s",(j([candidate]),batch["id"]))
        snapshot={"account_id":str(account["id"]),"external_account_id":account["external_account_id"],"binding_version":account["binding_version"],"config_revision":1,"control_version":1}
    return aid,account,card,batch,candidate,snapshot


def publish(partial):
    from lead_app.community_handoff import publish_candidate
    aid,_,_,batch,candidate,snapshot=partial
    with tx(aid) as c:
        return publish_candidate(c,aid,str(batch["id"]),candidate,snapshot)


def test_eligible_candidate_precedes_batch_completion(partial):
    from lead_app.community_handoff import next_handoff
    aid,account,_,batch,candidate,_=partial
    hid=publish(partial)
    with tx(aid) as c:
        found=next_handoff(c,aid,account,1)
        assert found["handoff_id"]==hid
        assert found["discovery_task_id"]==str(batch["id"])
        assert found["telegram_url"]==candidate["telegram_url"]
        assert one(c,"SELECT state FROM lead.tasks WHERE id=%s",(batch["id"],))["state"]=="running"


def test_partial_batch_requires_continue_discovery_grant(partial):
    from lead_app.community_handoff import next_handoff
    aid,account,card,*_=partial
    publish(partial)
    with tx(aid) as c:
        c.execute("UPDATE lead.interaction_cards SET payload=jsonb_set(payload,'{continue_discovery}','false') WHERE id=%s",(card["id"],))
        assert next_handoff(c,aid,account,1) is None


def test_duplicate_checkpoint_hands_off_once(partial):
    from lead_app.cadence_state import create_read_task
    from lead_app.community_handoff import next_handoff
    aid,account,card,*_=partial
    first=publish(partial)
    assert publish(partial)==first
    with tx(aid) as c:
        candidate=next_handoff(c,aid,account,1)
        payload={"candidate":candidate,"account_id":str(account["id"]),"external_account_id":account["external_account_id"],"delegation_id":str(card["id"]),"control_version":1,"config_revision":1}
        t=create_read_task(c,aid,payload,"handoff:"+first)
        assert create_read_task(c,aid,payload,"handoff:"+first)["id"]==t["id"]
        assert next_handoff(c,aid,account,1) is None
        assert len(all_rows(c,"SELECT id FROM lead.tasks WHERE input#>>'{candidate,handoff_id}'=%s",(first,)))==1


@pytest.mark.parametrize("change",["rules","binding","config","anchor","revoke"])
def test_changed_rules_or_binding_invalidate_handoff(partial,change):
    from lead_app.community_handoff import next_handoff
    aid,account,card,batch,candidate,snapshot=partial
    publish(partial)
    with tx(aid) as c:
        if change=="rules":
            c.execute("UPDATE lead.tasks SET result=jsonb_set(result,'{candidates,0,rules_quote}',%s) WHERE id=%s",(j("No automation"),batch["id"]))
        elif change=="binding":
            c.execute("UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s",(account["id"],))
        elif change=="config":
            c.execute("INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version) SELECT tenant_id,agent_id,2,provider,model_ref,prompt_version,toolset_version,output_schema_version FROM lead.agent_revisions WHERE agent_id=%s AND revision=1",(aid,))
            c.execute("UPDATE lead.agents SET config_revision=config_revision+1 WHERE id=%s",(aid,))
        elif change=="anchor":
            c.execute("UPDATE lead.tasks SET input=jsonb_set(input,'{account_id}',%s) WHERE id::text=(SELECT payload->>'discovery_task' FROM lead.interaction_cards WHERE id=%s)",(j(uid()),card["id"]))
        else:
            c.execute("UPDATE lead.interaction_cards SET state='canceled' WHERE id=%s",(card["id"],))
        assert next_handoff(c,aid,account,1) is None


def test_metadata_is_not_a_qualified_handoff(partial):
    from lead_app.community_handoff import publish_candidate
    aid,_,_,batch,candidate,snapshot=partial
    candidate.pop("evidence_id")
    with tx(aid) as c, pytest.raises(ValueError):
        publish_candidate(c,aid,str(batch["id"]),candidate,snapshot)


def test_handoff_guard_preserves_per_action_scope(partial):
    from lead_app.community_handoff import next_handoff
    from lead_app.community_runtime import guard
    aid,account,card,batch,_,_=partial
    publish(partial)
    class Actor:
        def __init__(self): self.aid=aid
        def gate(self,*a,**k): pass
    with tx(aid) as c:
        item=next_handoff(c,aid,account,1)
        task={"id":uid(),"agent_id":aid,"type":"community.engage","state":"running","result":{},"input":{"candidate":item,"account_id":str(account["id"]),"external_account_id":account["external_account_id"],"delegation_id":str(card["id"]),"control_version":1,"config_revision":1}}
        assert guard(Actor(),c,task,read_only=True)[0]["id"]==account["id"]
        task["input"]["join_approved"]=True
        assert guard(Actor(),c,task)[0]["id"]==account["id"]
        task["input"]["reply"]={"body":"Hello"}
        with pytest.raises(ValueError): guard(Actor(),c,task)


def test_mixed_executor_blocks_new_handoff_write_and_preserves_read(partial):
    from lead_app.community_handoff import next_handoff
    from lead_app.community_runtime import guard
    from test_cadence_state import activate_default
    aid,account,card,*_=partial
    publish(partial)
    activate_default(aid)
    class Actor:
        def __init__(self): self.aid=aid
        def gate(self,*a,**k): pass
    with tx(aid) as c:
        item=next_handoff(c,aid,account,1)
        task={"id":uid(),"agent_id":aid,"type":"community.engage","state":"running","result":{},"input":{"candidate":item,"account_id":str(account["id"]),"external_account_id":account["external_account_id"],"delegation_id":str(card["id"]),"control_version":1,"config_revision":1,"join_approved":True}}
        c.execute("UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{telegram_executor,execution_cadence_protocol}','0') WHERE id=%s",(aid,))
        assert guard(Actor(),c,task,read_only=True)[0]["id"]==account['id']
        with pytest.raises(ValueError,match='cadence_version_mismatch'): guard(Actor(),c,task)
