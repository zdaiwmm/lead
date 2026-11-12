from datetime import timedelta

import pytest
from lead_app.db import j, one, tx
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, utcnow
from lead_app.growth.store import create_experiment, submit_work


async def test_renamed_content_does_not_mint_new_progress(growth_agent,monkeypatch):
    from lead_app.growth import tools
    async def draft(output,*args,**kwargs):
        return output(body="Compare a quote in one place.",title="Quote comparison",claim_checks=[])
    monkeypatch.setattr(tools,"run_model",draft)
    outputs = []
    for purpose in ("first-title","renamed-title"):
        with tx(growth_agent) as c:
            ref = submit_work(c,growth_agent,TaskSpec(kind="content",purpose=purpose,payload={"brief":"Explain comparison"},review_at=utcnow()+timedelta(hours=1)))
            row = one(c,"SELECT * FROM lead.tasks WHERE id=%s",(ref["task_id"],))
        outputs.append(await tools.execute_tool(growth_agent,row,{}))
    assert outputs[0].artifacts==outputs[1].artifacts
    assert outputs[0].output["knowledge_progress"] and not outputs[1].output["knowledge_progress"]
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.content_items")["n"]==1


def test_closed_problem_cannot_be_reopened_by_changing_variation(growth_agent):
    spec = ExperimentSpec(question_type="audience",question="Who needs it?",hypothesis="An audience needs it",
        learning_signal="Demand evidence",token_limit=1000,review_at=utcnow()+timedelta(hours=1))
    with tx(growth_agent) as c:
        for name in ("one","two"):
            row = create_experiment(c,growth_agent,spec.model_copy(update={"variation":name}))
            c.execute("UPDATE lead.growth_experiments SET state='closed',closed_at=now(),conclusion=%s WHERE id=%s",(j({"verified_progress":False}),row["id"]))
    with pytest.raises(ValueError,match="no_progress_path_exhausted"):
        with tx(growth_agent) as c:
            create_experiment(c,growth_agent,spec.model_copy(update={"question":"Another title?","variation":"three"}))
