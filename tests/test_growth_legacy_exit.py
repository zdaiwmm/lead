from lead_app.db import one, tx, uid
from lead_app.operating_kernel import cycle


async def test_legacy_business_cycle_exits_for_new_owner(growth_agent):
    with tx(growth_agent) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(growth_agent,))
        before = one(c,"SELECT count(*) n FROM lead.tasks")["n"]
    result = await cycle({"agent_id":growth_agent,"cycle_id":uid()})
    assert result.get("stop") and result["reason"]=="growth_owns_scope"
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks")["n"]==before
