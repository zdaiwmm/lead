from datetime import timedelta

from lead_app.db import one, tx
from lead_app.growth.contracts import TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish, recover_due
from lead_app.growth.store import submit_work
from test_growth_runtime import new_work


def test_retry_episode_survives_new_task_name(growth_agent):
    ref = new_work(growth_agent)
    for n in range(3):
        claim = advance(ref, now=utcnow()+timedelta(minutes=n))["claim"]
        finish(claim, WorkResult(outcome="failed", failure_code="source_unavailable"))
    with tx(growth_agent) as c:
        other = submit_work(c, growth_agent, TaskSpec(kind="research", purpose="renamed",
            payload={}, dedupe_key="different name", review_at=utcnow()+timedelta(hours=1)))
    # A renamed dynamic research task may try an independent capability. The
    # shared failure budget remains on its original resource and episode.
    assert advance(other)["state"] == "ready"
    with tx(growth_agent) as c:
        root = one(c, "SELECT result FROM lead.tasks WHERE id=%s", (ref["task_id"],))
        assert root["result"]["runtime"]["recovery"]["attempts"] == 3


def test_waiting_does_not_exhaust_business_failures(growth_agent):
    from lead_app.growth.contracts import Wait
    ref = new_work(growth_agent)
    for n in range(5):
        claim = advance(ref, now=utcnow()+timedelta(minutes=n))["claim"]
        wait = Wait(reason="platform review", producer="platform.receipt", recovery_action="reconcile",
                    check_at=utcnow()+timedelta(seconds=1), episode_root=ref["task_id"])
        finish(claim, WorkResult(outcome="waiting", wait=wait))
    assert advance(ref, now=utcnow()+timedelta(minutes=10))["state"] == "ready"


def test_fairness_and_paging(growth_agent):
    new_work(growth_agent)
    with tx(growth_agent) as c:
        for n in range(44):
            submit_work(c, growth_agent, TaskSpec(kind="research", purpose="independent", dedupe_key=str(n),
                review_at=utcnow()+timedelta(hours=1)))
    pages = [recover_due(growth_agent)["checked_refs"] for _ in range(3)]
    assert [len(x) for x in pages] == [20, 20, 5]
    assert len(set(sum(pages, []))) == 45


async def test_matching_probe_recovers_original_once(growth_agent, monkeypatch):
    from lead_app.growth import tools
    from lead_app.growth.runtime import probe_episode, resume_task
    from lead_app.growth.tools import RecoverInput
    ref = new_work(growth_agent)
    for n in range(3):
        finish(advance(ref,now=utcnow()+timedelta(minutes=n))["claim"], WorkResult(outcome="failed",failure_code="source_unavailable"))
    with tx(growth_agent) as c:
        root = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
    import pytest
    with pytest.raises(ValueError,match="recovery_condition_unchanged"):
        resume_task(growth_agent,ref["task_id"],proof_ref="unrelated-browser-online")
    doc, _ = tools._remember(growth_agent,{"url":"https://example.com","title":"synthetic source","text":"A recovered public source."})
    async def recovered(*args):
        return WorkResult(outcome="completed",evidence_refs=[doc])
    monkeypatch.setattr(tools,"research",recovered)
    data = RecoverInput(episode_root=ref["task_id"],condition_revision=root["result"]["runtime"]["condition_revision"])
    result = await probe_episode(growth_agent,root,data)
    new_ref = result.output["resumed"]
    assert new_ref["task_id"] == ref["task_id"] and new_ref["task_generation"] == 2
    assert advance(ref)["state"] == "terminal"
    assert advance(new_ref)["state"] == "ready"
    assert (await probe_episode(growth_agent,root,data)).output["state"] == "obsolete_probe"
    with tx(growth_agent) as c:
        state = one(c,"SELECT result FROM lead.tasks WHERE id=%s",(ref["task_id"],))["result"]["runtime"]
        assert state["recovery"]["attempts"] == 3 and state["attempts"] == 4


async def test_paid_probe_exhaustion_persists_and_remains_visible(growth_agent,monkeypatch):
    from lead_app import model
    from lead_app.budget import BudgetExceeded
    from lead_app.growth.runtime import probe_episode
    from lead_app.growth.tools import RecoverInput
    from lead_app.growth.views import render_growth
    ref = new_work(growth_agent)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.tasks SET type='growth.content',input=jsonb_set(input,'{spec,kind}','\"content\"') WHERE id=%s",(ref["task_id"],))
    for n in range(3):
        finish(advance(ref,now=utcnow()+timedelta(minutes=n))["claim"],WorkResult(outcome="failed",failure_code="ModelHTTPError"))
    with tx(growth_agent) as c:
        root = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
    calls = []
    async def exhausted(*args,**kwargs):
        calls.append(1)
        raise BudgetExceeded("recovery_budget_exhausted")
    monkeypatch.setattr(model,"run_model",exhausted)
    data = RecoverInput(episode_root=ref["task_id"],condition_revision=root["result"]["runtime"]["condition_revision"])
    assert (await probe_episode(growth_agent,root,data)).output["state"]=="recovery_budget_exhausted"
    assert (await probe_episode(growth_agent,root,data)).output["state"]=="recovery_budget_exhausted"
    assert len(calls)==1
    recover_due(growth_agent,now=utcnow()+timedelta(hours=2))
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.recover'")["n"]==0
    assert render_growth(growth_agent)["waiting"][0]["wait"]["reason"]=="recovery_budget_exhausted"


def test_invalid_decision_does_not_poison_other_model_work_or_new_server_fact(growth_agent):
    new_work(growth_agent)
    with tx(growth_agent) as c:
        failed=submit_work(c,growth_agent,TaskSpec(kind="decision",purpose="growth",payload={"triggers":[{"reason":"business_receipt","ingest_id":"first"}]},review_at=utcnow()+timedelta(hours=1)))
    finish(advance(failed)["claim"],WorkResult(outcome="failed",failure_code="invalid_proposal"))
    with tx(growth_agent) as c:
        content=submit_work(c,growth_agent,TaskSpec(kind="content",purpose="draft",payload={"brief":"An independent factual draft"},review_at=utcnow()+timedelta(hours=1)))
        new=submit_work(c,growth_agent,TaskSpec(kind="decision",purpose="growth",payload={"triggers":[{"reason":"business_receipt","ingest_id":"new-real-fact"}]},review_at=utcnow()+timedelta(hours=1)))
    assert advance(content)["state"]=="ready"
    assert advance(new)["state"]=="ready"
