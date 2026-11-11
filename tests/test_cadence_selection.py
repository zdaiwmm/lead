"""Known participation deadlines prevent repeated paid reads, not receipts."""
# ruff: noqa: F811

from datetime import datetime, timedelta, timezone

import pytest
from lead_app import community_runtime, community_selection, operating_kernel
from lead_app.community_growth import GrowthConfig
from lead_app.db import j, one, tx
from lead_app.scope import service_scope
from test_cadence_state import activate_default, attest, cadence_agent  # noqa: F401


@pytest.mark.parametrize("origin,age,expected", [("owner_explicit", 1, None), ("owner_explicit", 25, "candidate"), ("verified_system_default", 1, "candidate")])
def test_selector_honors_current_account_deadline(cadence_agent, monkeypatch, origin, age, expected):
    from lead_app.cadence_state import activate, preview_migration
    aid, account, _, old = cadence_agent
    with service_scope(), tx(aid) as c:
        attest(c, aid, origin)
        preview = preview_migration(c, aid, expected_control=1)
        activate(c, aid, preview["preview_digest"], 1)
        c.execute("UPDATE lead.tasks SET result=%s WHERE id=%s", (j({"join_state":"confirmed", "join_dispatched_at":(datetime.now(timezone.utc)-timedelta(hours=age)).isoformat()}), old["id"]))
    monkeypatch.setattr(operating_kernel, "blocked", lambda *a, **k: {"unknown_write_scope":False, "accounts":[]})
    monkeypatch.setattr(community_runtime, "membership_review", lambda *a, **k: None)
    monkeypatch.setattr(community_runtime, "next_voice_edit", lambda *a, **k: None)
    calls=[]
    def candidate(*a, **k):
        calls.append(1)
        return {"telegram_url":"https://t.me/local_fixture"}
    monkeypatch.setattr(community_runtime, "next_candidate", candidate)
    with tx(aid) as c:
        agent=one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
        result=community_selection.choose(c, agent, GrowthConfig.model_validate(agent["onboarding_state"]["community_growth"]))
    assert (result or {}).get("kind") == expected
    assert bool(calls) == (expected is not None)
