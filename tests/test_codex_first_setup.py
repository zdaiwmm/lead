from uuid import uuid4

from lead_app import domain
from lead_app.db import one, tx


def test_first_project_can_pair_before_research_and_confirm_same_identity():
    client_id = str(uuid4())
    aid = domain.create_agent("website-draft:" + client_id, draft_client_id=client_id, runtime_engine="growth")
    try:
        with tx(aid) as c:
            assert one(c, "SELECT desired_state FROM lead.agents WHERE id=%s", (aid,))["desired_state"] == "draft"
            assert one(c, "SELECT count(*) n FROM lead.tasks")["n"] == 0
        from lead_app import local_executor
        pairing = local_executor.create_pairing(aid)
        local_executor.pair(pairing["code"], "My Codex", "darwin", {"ready": True})
        assert local_executor.enabled(aid)
        brief = {"product_name": "Source-backed product", "product_url": "https://example.com",
            "introduction": "An owner-confirmed product", "features": [], "customers": [],
            "scenarios": [], "advantages": [], "notes": ""}
        assert domain.confirm_draft_agent(aid, client_id, brief) == aid
        assert domain.confirm_draft_agent(aid, client_id, brief) == aid
        with tx(aid) as c:
            assert one(c, "SELECT count(*) n FROM lead.business_profiles")["n"] == 1
            assert one(c, "SELECT name FROM lead.agents WHERE id=%s", (aid,))["name"] == brief["product_name"]
    finally:
        domain.set_control(aid, "archive")
