from uuid import UUID, uuid5

import pytest
from lead_app import community_health
from lead_app.db import require_agent, tx
from test_telegram_delivery_and_fairness import create_task, workspace  # noqa: F401

import pc_resident_agent as resident


def test_production_opportunities_are_scoped_and_atomic(workspace, monkeypatch):
    aid, _ = workspace
    monkeypatch.setattr(resident, "AID", aid)
    monkeypatch.setattr(resident, "SLOTS", {k: uuid5(UUID(aid), k) for k in ("reply", "promotion")})
    ad = {"standalone": True, "promote": True, "permission_key": "fixture"}
    reply = {"standalone": False, "promote": False, "reply_to_mid": "31"}
    with tx(aid) as c:
        legacy = create_task(c, workspace, "-100", "failed", {"reply_state": "failed"}, reply=ad)
        resident.claim(c, legacy, "-100")
        for peer in ("-100", "-200"):
            for kind in (ad, reply):
                task = create_task(c, workspace, peer, "running", {}, reply=kind)
                resident.claim(c, task, peer, production=True)
    with pytest.raises(ValueError, match="already_claimed"), tx(aid) as c:
        duplicate = create_task(c, workspace, "-100", "running", {}, reply=ad)
        resident.claim(c, duplicate, "-100", production=True)


@pytest.mark.asyncio
async def test_missing_clearance_blocks_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert await resident.official_account_status(None, "42") != "no_limits"
    for op in ("reply", "join", "profile_bio", "leave_restricted"):
        result = await resident.dispatch(None, "42", "/agents/"+resident.AID+"/community/step",
                                         {"operation": op}, production=True, writable=False)
        assert result["state"] == "blocked"


def test_inventory_explains_read_hold_without_recording_message_text(workspace, monkeypatch):
    aid, base = workspace
    monkeypatch.setattr(resident, "AID", aid)
    events = {}
    monkeypatch.setattr(resident, "report", lambda event, **fields: events.update({event: fields}))
    with tx(aid) as c:
        task = create_task(c, workspace, "-701", "failed", {"browser_diagnostic": {"operation": "observe", "error": "ValueError"}})
        community_health.failed(c, aid, task, {"error_code": "browser_error", "scope": "account", "stage": "observe",
            "message": "公开群路由未能绑定已核验目标：example-private-detail"})
        resident.inventory(c, require_agent(c, aid), base["external_account_id"])
    backoff = events["reconciliation_status"]["account_backoff"]
    assert (backoff["operation"], backoff["peer"], backoff["cause"]) == ("observe", "-701", "peer_route_unresolved")
    assert "example-private-detail" not in str(events)


def test_patrol_snapshot_is_scoped_and_persists_next_deadline(workspace, monkeypatch):
    from datetime import datetime, timedelta, timezone
    aid, base = workspace
    monkeypatch.setattr(resident,"AID",aid)
    now=datetime.now(timezone.utc)
    events={}
    monkeypatch.setattr(resident,"report",lambda event,**fields: events.update({event:fields}))
    with tx(aid) as c:
        member=create_task(c,workspace,"-710","succeeded",{"join_state":"confirmed","membership_origin":"existing",
                "history_observed_at":(now-timedelta(hours=2)).isoformat(),"history_observation":{"joined":True,"new_messages":4}})
        resident.record_patrol_schedule(c,require_agent(c,aid),base["external_account_id"],now)
        sample=events["patrol_schedule"]
        assert any(row["peer"]=="-710" for row in sample["eligible"])
        assert sample["due_groups"] >= 1 and not sample["patrol_stalled"]
        current=require_agent(c,aid)
        assert current["onboarding_state"]["telegram_executor"]["next_patrol_at"]
        resident.record_patrol_schedule(c,current,base["external_account_id"],now+timedelta(minutes=31))
        assert events["patrol_schedule"]["patrol_stalled"]


@pytest.mark.asyncio
async def test_bound_official_chat_needs_new_clearance_after_limit(tmp_path,monkeypatch):
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from lead_app import community_receipts
    import asyncio
    monkeypatch.setenv("HOME",str(tmp_path))
    path=tmp_path/"spambot-owner-query-20261108.json"
    path.write_text(json.dumps({"account":"42","peer":"178220800","incoming":"100","status":"limited"}))
    chat=SimpleNamespace(evaluate=AsyncMock())
    monkeypatch.setattr(community_receipts,"bound_view",AsyncMock())
    monkeypatch.setattr(resident.telegram,"selected_chat",AsyncMock(return_value=chat))
    actor=SimpleNamespace(lock=asyncio.Lock(),page=object())
    chat.evaluate.return_value={"mid":"100","text":"Your account is no longer limited"}
    assert await resident.official_account_status(actor,"42")!="no_limits"
    chat.evaluate.return_value={"mid":"101","text":"Your account is no longer limited"}
    assert await resident.official_account_status(actor,"42")=="no_limits"
    evidence=json.loads(path.read_text())["evidence_observed_at"]
    assert await resident.official_account_status(actor,"42")=="no_limits"
    assert json.loads(path.read_text())["evidence_observed_at"]==evidence


def test_production_uses_per_opportunity_grants_with_optional_pilot_list(monkeypatch):
    monkeypatch.setenv("LEAD_TG_DELIVERY_MODE","production")
    monkeypatch.delenv("LEAD_TG_PILOT_PEERS",raising=False)
    assert resident.pilot_allows("-71")
    monkeypatch.setenv("LEAD_TG_PILOT_PEERS","-72")
    assert not resident.pilot_allows("-71") and resident.pilot_allows("-72")
    monkeypatch.delenv("LEAD_TG_PILOT_PEERS",raising=False)
    monkeypatch.setenv("LEAD_TG_DELIVERY_MODE","readonly")
    assert not resident.pilot_allows("-71")
