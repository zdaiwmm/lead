from datetime import timedelta

import pytest
from lead_app.budget import BudgetExceeded, growth_cost_context, reserve, settle
from lead_app.db import tx
from lead_app.growth.contracts import ExperimentSpec, utcnow
from lead_app.growth.store import create_experiment


def test_budget_cancel_unknown_and_question_variations(growth_agent):
    spec = ExperimentSpec(question_type="audience",question="Who needs the product?",hypothesis="Makers need it",
        learning_signal="A source-backed need",token_limit=1000,review_at=utcnow()+timedelta(hours=1))
    with tx(growth_agent) as c:
        first = create_experiment(c,growth_agent,spec)
        second = create_experiment(c,growth_agent,spec.model_copy(update={"audience":"another audience", "variation":"different"}))
    token = growth_cost_context.set({"task_id":"synthetic", "experiment_id":str(first["id"])})
    try:
        a = reserve(growth_agent,"growth:one",600)
        settle(growth_agent,a)  # Unknown billing retains its reservation.
        growth_cost_context.set({"task_id":"synthetic2", "experiment_id":str(second["id"])})
        with pytest.raises(BudgetExceeded,match="question_limit"):
            reserve(growth_agent,"growth:two",500)
        b = reserve(growth_agent,"growth:two-small",300)
        settle(growth_agent,b,known_unbilled=True)
        assert reserve(growth_agent,"growth:three",400)
    finally:
        growth_cost_context.reset(token)
