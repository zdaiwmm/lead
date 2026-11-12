from datetime import timedelta

from lead_app.db import j, one, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish, recover_due
from lead_app.growth.store import submit_work


def new_work(aid):
    with tx(aid) as c:
        c.execute("""UPDATE lead.agents SET desired_state='running',
          onboarding_state=onboarding_state || %s WHERE id=%s""",
          (j({"growth_runtime": {"decision": {"engine": "growth", "generation": 1}}}), aid))
        return submit_work(c, aid, TaskSpec(kind="research", purpose="product", payload={},
                                           review_at=utcnow() + timedelta(hours=1)))


def test_fixed_ref_delivery(growth_agent):
    ref = new_work(growth_agent)
    first = advance(ref)
    assert first["state"] == "ready"
    assert advance(ref)["state"] == "wait"
    done = finish(first["claim"], WorkResult(outcome="completed", output={"observed": "source"}))
    assert done["accepted_control"]
    assert advance(ref)["state"] == "terminal"


def test_retry_episode(growth_agent):
    ref = new_work(growth_agent)
    for attempt in range(3):
        first = advance(ref, now=utcnow() + timedelta(minutes=attempt))
        assert first["state"] == "ready"
        finish(first["claim"], WorkResult(outcome="failed", failure_code="source_unavailable"))
    assert advance(ref, now=utcnow() + timedelta(hours=1))["state"] == "terminal"
    with tx(growth_agent) as c:
        row = one(c, "SELECT state,result FROM lead.tasks WHERE id=%s", (ref["task_id"],))
        assert row["state"] == "failed"
        assert row["result"]["runtime"]["attempts"] == 3


def test_missing_wake_recovery(growth_agent):
    ref = new_work(growth_agent)
    claim = advance(ref)["claim"]
    with tx(growth_agent) as c:
        c.execute("""UPDATE lead.tasks SET result=jsonb_set(result,'{runtime,lease_until}',to_jsonb(%s::text))
          WHERE id=%s""", ((utcnow() - timedelta(seconds=1)).isoformat(), ref["task_id"]))
    recovered = recover_due(growth_agent)
    assert ref["task_id"] in recovered["checked_refs"]
    assert not finish(claim, WorkResult(outcome="completed", output={"stale": True}))["accepted_control"]
