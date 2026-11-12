from datetime import timedelta

import pytest
from lead_app import domain
from lead_app.db import one, tx, uid
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


@pytest.fixture
def growth_agent():
    aid = domain.create_agent(runtime_engine="growth")
    yield aid
    domain.set_control(aid, "archive")


def test_fixed_ref_delivery(growth_agent):
    spec = TaskSpec(kind="research", purpose="product", payload={"question": "Inspect product"},
                    review_at=utcnow() + timedelta(hours=1))
    with tx(growth_agent) as c:
        first = submit_work(c, growth_agent, spec)
        second = submit_work(c, growth_agent, spec)
        assert first == second
        assert one(c, "SELECT count(*) n FROM lead.tasks WHERE type='growth.research'")["n"] == 1
        assert one(c, "SELECT count(*) n FROM lead.outbox_events WHERE event_type='growth.work'")["n"] == 1


def test_scope_and_dependency(growth_agent):
    other = domain.create_agent()
    try:
        spec = TaskSpec(kind="research", purpose="product", account_id=uid(), payload={},
                        review_at=utcnow() + timedelta(hours=1))
        with pytest.raises(ValueError, match="account"):
            with tx(growth_agent) as c:
                submit_work(c, growth_agent, spec)
        with tx(other) as c:
            assert one(c, "SELECT id FROM lead.agents WHERE id=%s", (growth_agent,)) is None
    finally:
        domain.set_control(other, "archive")
