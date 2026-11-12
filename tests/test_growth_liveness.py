from datetime import timedelta

from lead_app import budget
from lead_app.db import j, one, tx, uid
from lead_app.growth.contracts import ExperimentSpec, utcnow
from lead_app.growth.runtime import (
    advance,
    consume_triggers,
    ensure_decision,
    recover_due,
)
from test_growth_runtime import new_work


def test_lease_expiry_cannot_be_extended_by_heartbeats(growth_agent):
    ref = new_work(growth_agent)
    now = utcnow()
    for index in range(3):
        claimed = advance(ref,now=now+timedelta(minutes=10*index))
        assert claimed["state"]=="ready"
        with tx(growth_agent) as c:
            # A transport heartbeat is not a new work artifact/checkpoint.
            c.execute("UPDATE lead.agents SET heartbeat_at=now() WHERE id=%s",(growth_agent,))
        recover_due(growth_agent,now=now+timedelta(minutes=10*index+10))
    assert advance(ref,now=now+timedelta(minutes=31))["state"]=="terminal"
    with tx(growth_agent) as c:
        row = one(c,"SELECT state,result FROM lead.tasks WHERE id=%s",(ref["task_id"],))
        assert row["state"]=="failed" and row["result"]["runtime"]["recovery"]["attempts"]==3


def test_event_storm_coalesces_and_new_fact_survives_consumption(growth_agent):
    new_work(growth_agent)
    first = {"reason":"business_receipt","event_name":"CoreValue","ingest_id":"first"}
    ref = ensure_decision(growth_agent,[first])
    assert isinstance(ref,dict)
    for index in range(100):
        assert ensure_decision(growth_agent,[{**first,"ingest_id":str(index)}])==ref
    consume_triggers(growth_agent,[first])
    with tx(growth_agent) as c:
        state = one(c,"SELECT onboarding_state FROM lead.agents WHERE id=%s",(growth_agent,))["onboarding_state"]["growth_runtime"]
        assert len(state["pending_triggers"])==1
        latest = next(iter(state["pending_triggers"].values()))
        assert latest["ingest_id"]=="99"
        c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s",(ref["task_id"],))
    consume_triggers(growth_agent,[latest])
    assert ensure_decision(growth_agent,[latest]) is None
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.decision'")["n"]==1


def test_experiment_recovery_rotates_past_healthy_first_page(growth_agent):
    new_work(growth_agent)
    spec = ExperimentSpec(question_type="audience",question="A bounded independent question",
        hypothesis="An audience needs it",learning_signal="Real evidence",token_limit=1000,
        review_at=utcnow()+timedelta(hours=2)).model_dump(mode="json")
    identifiers = sorted(uid() for _ in range(45))
    with tx(growth_agent) as c:
        for identifier in identifiers:
            # Scanner stress fixtures are deliberately larger than the proposal
            # admission limit and do not exercise model acceptance here.
            c.execute("""INSERT INTO lead.growth_experiments(id,tenant_id,agent_id,question_key,variation_key,specification)
                VALUES(%s,lead.ctx_tenant(),%s,%s,'initial',%s)""",(identifier,growth_agent,identifier,j(spec)))
    context = budget.growth_cost_context.set({"experiment_id":identifiers[-1]})
    try:
        budget.reserve(growth_agent,"fairness-budget:"+uid(),1000,"test-model","test")
    finally:
        budget.growth_cost_context.reset(context)
    for _ in range(3):
        recover_due(growth_agent)
    with tx(growth_agent) as c:
        assert one(c,"SELECT state FROM lead.growth_experiments WHERE id=%s",(identifiers[-1],))["state"]=="closed"
        assert one(c,"SELECT count(*) n FROM lead.growth_experiments WHERE state='active'")["n"]==44


def test_distinct_feedback_pages_survive_pause_and_are_consumed_once(growth_agent):
    import json

    from lead_app.growth.agent import MAX_CONTEXT_BYTES, build_context
    new_work(growth_agent)
    tasks = []
    from lead_app.growth.contracts import TaskSpec
    from lead_app.growth.store import submit_work
    with tx(growth_agent) as c:
        for n in range(45):
            ref = submit_work(c,growth_agent,TaskSpec(kind="research",purpose="evidence",dedupe_key=str(n),
                review_at=utcnow()+timedelta(hours=1)))
            c.execute("UPDATE lead.tasks SET state='succeeded',result=%s WHERE id=%s",(
                j({"output":{"output":{"fact":f"independent fact {n}","excerpt":"事实"*2000}}}),ref["task_id"]))
            tasks.append(ref["task_id"])
        c.execute("UPDATE lead.agents SET desired_state='paused' WHERE id=%s",(growth_agent,))
    for task in tasks:
        assert ensure_decision(growth_agent,[{"reason":"evidence_added","task_id":task}]) is None
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(growth_agent,))
    consumed = []
    for _ in range(6):
        ref = ensure_decision(growth_agent)
        assert ref
        with tx(growth_agent) as c:
            row = one(c,"SELECT input FROM lead.tasks WHERE id=%s",(ref["task_id"],))
            triggers = row["input"]["spec"]["payload"]["triggers"]
        assert 1<=len(triggers)<=8
        context = build_context(growth_agent,triggers)
        assert len(json.dumps(context,default=str,ensure_ascii=False).encode())<=MAX_CONTEXT_BYTES
        assert {t["task_id"] for t in triggers} <= {str(w["id"]) for w in context["work"]}
        consumed.extend(t["task_id"] for t in triggers)
        with tx(growth_agent) as c:
            c.execute("UPDATE lead.tasks SET state='succeeded',completed_at=now() WHERE id=%s",(ref["task_id"],))
        consume_triggers(growth_agent,triggers)
    assert len(consumed)==45 and set(consumed)==set(tasks)
    assert ensure_decision(growth_agent) is None


def test_release_hold_waits_before_account_or_reconcile_work(growth_agent):
    ref = new_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",(
            j({"home_release_maintenance":{"request_id":"owned-test"}}),growth_agent))
    result = advance(ref)
    assert result["state"]=="wait" and result["wait"]["reason"]=="release_maintenance"
