from datetime import timedelta

from lead_app.db import one, tx
from lead_app.growth.contracts import TaskSpec, utcnow
from lead_app.growth.store import submit_work


def test_no_period_plans_and_read_has_no_side_effect(growth_agent):
    from lead_app.api import app
    from lead_app.growth.views import render_growth
    with tx(growth_agent) as c:
        submit_work(c,growth_agent,TaskSpec(kind="research",purpose="product",review_at=utcnow()+timedelta(hours=1)))
        before = one(c,"SELECT count(*) n FROM lead.outbox_events")["n"]
    first = render_growth(growth_agent)
    second = render_growth(growth_agent)
    assert first["agent_id"] == growth_agent and len(first["current_work"]) == 1
    assert first["results"]["new_units"] is None and first["results"]["measurement"] == "missing"
    assert not any(path.startswith('/api/agents/{aid}/plans') for path in app.openapi()["paths"])
    from lead_app.domain import agent_detail
    assert "plans" not in agent_detail(growth_agent,chat=True)
    assert set(first)==set(second)
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.outbox_events")["n"] == before
