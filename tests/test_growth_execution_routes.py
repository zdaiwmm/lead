import pytest
from lead_app import domain
from lead_app.db import one, tx
from lead_app.execution_routes import register, request_route, resolve
from lead_app.growth.runtime import advance
from lead_app.growth.tools import prepare_channel
from lead_app.research import begin_run
from test_growth_channel_operation import channel_work


def test_public_research_has_explicit_independent_profile(growth_agent):
    domain.connect_lab(growth_agent)
    run = begin_run(growth_agent,"Read public source")
    with tx(growth_agent) as c:
        social = one(c,"SELECT profile_id FROM lead.social_accounts LIMIT 1")
        work = one(c,"SELECT profile_id FROM lead.browser_runs WHERE id=%s",(run,))
        assert work["profile_id"] != social["profile_id"]
        reference = request_route(c,growth_agent,"/research/command",{"run_id":run})
        assert reference.profile_id == work["profile_id"]
        assert request_route(c,growth_agent,f"/agents/{growth_agent}/research/{run}/close")==reference


def test_uncertain_operation_keeps_original_executor(growth_agent,monkeypatch):
    monkeypatch.setenv("LEAD_BROWSER_TRANSPORTS",'{"shared":"http://127.0.0.1:8089","replacement":"http://127.0.0.1:18091"}')
    ref,data,account = channel_work(growth_agent)
    advance(ref)
    action = prepare_channel(growth_agent,ref["task_id"],data)
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.actions SET state='dispatching' WHERE id=%s",(action["id"],))
    register(growth_agent,account["profile_id"],"replacement")
    assert resolve(growth_agent,account_id=account["id"]).transport_ref=="replacement"
    with tx(growth_agent) as c:
        actual = request_route(c,growth_agent,"/publish",{"action_id":str(action["id"])})
        assert actual.transport_ref=="shared" and actual.route_generation==1
    monkeypatch.setenv("LEAD_BROWSER_TRANSPORTS",'{"replacement":"http://127.0.0.1:18091"}')
    with pytest.raises(ValueError,match="original_execution_route_unavailable"):
        with tx(growth_agent) as c:
            request_route(c,growth_agent,"/publish",{"action_id":str(action["id"])})


def test_missing_or_cross_project_route_has_no_default(growth_agent):
    from uuid import uuid4
    with pytest.raises(ValueError,match="execution_route_missing"):
        resolve(growth_agent,profile_id=uuid4())
    other = domain.create_agent()
    try:
        with tx(growth_agent) as c:
            profile = one(c,"SELECT id FROM lead.browser_profiles LIMIT 1")["id"]
        with pytest.raises(ValueError,match="profile_scope"):
            register(other,profile)
    finally:
        domain.set_control(other,"archive")
