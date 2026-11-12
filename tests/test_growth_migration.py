import psycopg
import pytest
from lead_app import domain
from lead_app.activities import prepare_action
from lead_app.db import j, one, tx
from lead_app.execution_routes import register
from lead_app.growth.migration import apply_migration, preview_migration
from lead_app.scope import service_scope


def legacy_action(aid,unknown=False):
    domain.connect_lab(aid)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(aid,))
        profile = one(c,"SELECT profile_id FROM lead.social_accounts WHERE platform='lab'")["profile_id"]
    register(aid,profile)
    task = domain.queue_lab_publish(aid,"Synthetic original publication","synthetic:original")
    action = prepare_action(aid,task["id"])
    if unknown:
        with tx(aid) as c:
            c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action["id"],))
            c.execute("UPDATE lead.actions SET state='unknown' WHERE id=%s",(action["id"],))
            c.execute("UPDATE lead.tasks SET state='reconciling' WHERE id=%s",(task["id"],))
    return action


def test_migration_preserves_original_unknown_and_other_project():
    first = domain.create_agent(runtime_engine="legacy")
    second = domain.create_agent(runtime_engine="legacy")
    try:
        unknown = legacy_action(first,True)
        untouched = legacy_action(second)
        report = preview_migration(first)
        assert {str(row["id"]) for row in report["actions"]}=={str(unknown["id"])}
        with service_scope():
            result = apply_migration(first,report["report_hash"],0)
            duplicate = apply_migration(first,report["report_hash"],0)
        assert result["original_actions"]==1 and duplicate["duplicate"]
        with tx(first) as c:
            actual = one(c,"SELECT * FROM lead.actions WHERE id=%s",(unknown["id"],))
            task = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(unknown["task_id"],))
            assert actual["operation_key"]==unknown["operation_key"] and actual["intent"]==unknown["intent"]
            assert actual["state"]=="unknown" and task["type"]=="growth.channel"
            assert task["input"]["spec"]["payload"]["original_action_id"]==str(unknown["id"])
        with tx(second) as c:
            assert one(c,"SELECT state FROM lead.actions WHERE id=%s",(untouched["id"],))["state"]=="prepared"
    finally:
        domain.set_control(first,"archive")
        domain.set_control(second,"archive")


def test_migration_requires_snapshot_and_operator():
    aid = domain.create_agent(runtime_engine="legacy")
    try:
        report = preview_migration(aid)
        with pytest.raises(PermissionError):
            apply_migration(aid,report["report_hash"],0)
        with service_scope(),pytest.raises(ValueError,match="migration_snapshot_changed"):
            apply_migration(aid,"changed",0)
        with tx(aid) as c:
            assert not one(c,"SELECT onboarding_state FROM lead.agents WHERE id=%s",(aid,))["onboarding_state"].get("growth_runtime")
    finally:
        domain.set_control(aid,"archive")


def test_native_unknown_is_preserved_and_only_that_resource_reconciles():
    aid = domain.create_agent(runtime_engine="legacy")
    try:
        legacy_action(aid)
        with tx(aid) as c:
            account = one(c,"SELECT * FROM lead.social_accounts WHERE platform='lab'")
            native = one(c,"""INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
                VALUES(lead.ctx_tenant(),%s,'community.engage','reconciling',%s,%s,%s,now()) RETURNING *""",
                (aid,"native-original:"+str(account["id"]),j({"account_id":str(account["id"]),
                    "external_account_id":account["external_account_id"],"reply":{"body":"Original immutable reply"}}),
                    j({"reply_state":"unknown","target_id":"-20002","baseline_mid":"1"})))
            c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s",(aid,))
        report = preview_migration(aid)
        with service_scope():
            apply_migration(aid,report["report_hash"],0)
        with tx(aid) as c:
            original = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(native["id"],))
            assert original["result"]==native["result"] and original["input"]["reply"]==native["input"]["reply"]
            assert original["input"]["migration"]["owner"]=="receipt_only"
            receipt = one(c,"SELECT id,input FROM lead.tasks WHERE type='growth.observe'")
            assert receipt["input"]["spec"]["payload"]["legacy_native_task_id"]==str(native["id"])
        from lead_app.growth.runtime import advance
        assert advance({"agent_id":aid,"task_id":str(receipt["id"]),"task_generation":1})["state"]=="ready"
        with pytest.raises(psycopg.errors.RaiseException,match="legacy_native_owner_retired"):
            with tx(aid) as c:
                c.execute("UPDATE lead.tasks SET result=result || %s WHERE id=%s",(j({"reply_state":"dispatching"}),native["id"]))
    finally:
        domain.set_control(aid,"archive")


@pytest.mark.parametrize("explicit_pause",[False,True])
def test_legacy_false_is_not_an_inferred_owner_pause(explicit_pause):
    from lead_app.db import audit
    aid=domain.create_agent(runtime_engine="legacy")
    try:
        domain.connect_lab(aid)
        with tx(aid) as c:
            c.execute("UPDATE lead.social_accounts SET automation_enabled=false")
            if explicit_pause:
                audit(c,aid,"channel.paused",{"platform":"lab"})
        report=preview_migration(aid)
        with service_scope():
            apply_migration(aid,report["report_hash"],0)
        with tx(aid) as c:
            intent=one(c,"SELECT desired,scope FROM lead.account_operating_intents")
            assert intent["desired"]==("paused" if explicit_pause else "running")
            assert bool(intent["scope"]["source_owner_event"])==explicit_pause
            assert not one(c,"SELECT automation_enabled FROM lead.social_accounts")["automation_enabled"]
    finally:
        domain.set_control(aid,"archive")
