from uuid import uuid4

from lead_app import api, domain
from lead_app.db import one, tx
from lead_app.growth.store import enabled
from lead_app.growth.tools import execute_tool
from lead_app.website_brief import ConfirmedBrief


async def test_public_creation_bootstraps_without_login_or_model():
    response = api.confirm_website(ConfirmedBrief(client_id=uuid4(),product_name="Generic quote tool",
        introduction="Independent makers can build and compare a client quote.",features=["Build a quote"],customers=["Independent makers"]))
    aid = response["id"]
    try:
        with tx(aid) as c:
            agent = one(c,"SELECT * FROM lead.agents WHERE id=%s",(aid,))
            assert enabled(agent) and agent["desired_state"]=="running"
            assert not one(c,"SELECT id FROM lead.social_accounts")
            task = one(c,"SELECT * FROM lead.tasks WHERE type='growth.research'")
            assert task and task["input"]["spec"]["purpose"]=="product"
        result = await execute_tool(aid,task,{})
        assert result.outcome=="completed" and result.output["trust"]=="owner_asserted"
        domain.set_control(aid,"pause")
        from lead_app.growth.migration import bootstrap
        with tx(aid) as c:
            bootstrap(c,aid)
            assert one(c,"SELECT desired_state FROM lead.agents WHERE id=%s",(aid,))["desired_state"]=="paused"
    finally:
        domain.set_control(aid,"archive")


def test_empty_public_project_cannot_reuse_another_product():
    source = api.confirm_website(ConfirmedBrief(client_id=uuid4(),introduction="A source product that belongs to its own project."))["id"]
    empty = api.create(api.CreateAgent(client_id=uuid4()))["id"]
    try:
        with tx(empty) as c:
            assert one(c,"SELECT count(*) n FROM lead.business_profiles")["n"]==0
            assert one(c,"SELECT count(*) n FROM lead.tasks")["n"]==0
        with tx(source) as c:
            assert one(c,"SELECT count(*) n FROM lead.tasks WHERE type='growth.research'")["n"]==1
    finally:
        domain.set_control(source,"archive")
        domain.set_control(empty,"archive")
