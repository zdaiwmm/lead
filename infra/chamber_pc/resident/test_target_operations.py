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


@pytest.mark.asyncio
async def test_login_handoff_works_without_a_bound_account(monkeypatch):
    import asyncio
    from contextlib import nullcontext
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from uuid import uuid4

    import httpx

    gateway = resident.ChamberGateway()
    actor = SimpleNamespace(disconnected=False)
    state = {'desired_state': 'paused'}
    monkeypatch.setattr(resident, 'tx', lambda aid: nullcontext())
    monkeypatch.setattr(resident, 'require_agent', lambda c, aid: state)
    monkeypatch.setattr(resident, 'bound_account', Mock(side_effect=AssertionError('login requires binding')))
    monkeypatch.setattr(resident, 'require_original_profile', Mock())
    opened = AsyncMock(return_value={'session_id': str(uuid4())})
    monkeypatch.setattr(resident.browser, 'open_browser', opened)
    monkeypatch.setattr(resident.browser, 'screenshot', AsyncMock(return_value='frame'))
    monkeypatch.setattr(resident.browser, 'user_input', AsyncMock(return_value={'ok': True}))
    inspect = AsyncMock(return_value={'external_account_id': '42'})
    monkeypatch.setattr(resident.browser, 'inspect_account', inspect)
    monkeypatch.setitem(resident.browser.gateway.actors, resident.AID, actor)
    check = Mock()
    monkeypatch.setattr(resident.browser, 'require_takeover', check)

    prefix = '/agents/' + resident.AID
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://test') as client:
        body = {'platform': 'telegram'}
        assert (await client.post(prefix+'/open', json=body)).status_code == 403
        client.headers['Authorization'] = 'Bearer ' + resident.browser.internal_token()
        assert (await client.get('/health')).json()['ok']
        assert (await client.post('/agents/'+str(uuid4())+'/open', json=body)).status_code == 403
        assert (await client.post(prefix+'/publish', json={})).status_code == 403
        assert (await client.post(prefix+'/open', json={'platform':'lab'})).status_code == 403
        opened.assert_not_awaited()
        state['desired_state'] = 'running'
        assert (await client.post(prefix+'/open', json=body)).status_code == 409
        state['desired_state'] = 'paused'
        gateway.handoff.clear()
        async with gateway.owner:
            opening = asyncio.create_task(client.post(prefix+'/open', json=body))
            await asyncio.wait_for(gateway.handoff.wait(), 1)
            opened.assert_not_awaited()
        assert (await opening).json() == opened.return_value
        takeover = str(uuid4())
        assert (await client.get(prefix+'/screenshot', params={'takeover':takeover})).status_code == 200
        assert (await client.post(prefix+'/input', json={'takeover':takeover, 'kind':'click'})).status_code == 200
        assert (await client.get(prefix+'/screenshot')).status_code == 422
        assert (await client.post(prefix+'/input', json={'kind':'click'})).status_code == 409
        assert (await client.post(prefix+'/account/inspect', json=body)).json() == inspect.return_value
        check.assert_called_once_with(resident.AID, actor, None)


def test_disconnected_account_allows_login_only(workspace, monkeypatch):
    from lead_app import channels
    from lead_app.db import one

    aid, base = workspace
    monkeypatch.setattr(resident, 'AID', aid)
    with tx(aid) as c:
        account = one(c, 'SELECT verified_at FROM lead.social_accounts WHERE id=%s', (base['account_id'],))
        assert resident.bound_account(c) == base['external_account_id']
    channels.disconnect_account(aid, base['account_id'], account['verified_at'])
    with tx(aid) as c:
        assert resident.bound_account(c, required=False) is None
        with pytest.raises(ValueError, match='bound_account_unavailable'):
            resident.bound_account(c)


@pytest.mark.asyncio
async def test_unbound_resident_serves_login_and_starts_work_only_after_resume(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager, nullcontext
    from unittest.mock import AsyncMock

    import httpx

    listening = asyncio.Event()
    operating = asyncio.Event()
    state = {'desired_state': 'running'}
    account = None
    app = None

    @asynccontextmanager
    async def listen(login_app):
        nonlocal app
        app = login_app
        listening.set()
        yield

    async def operate(mode, gateway):
        assert mode == 'run' and account == '42' and state['desired_state'] == 'running'
        operating.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(resident, 'serve_gateway', listen)
    monkeypatch.setattr(resident, 'agent_tenant', lambda aid: aid)
    monkeypatch.setattr(resident, 'tx', lambda aid: nullcontext())
    monkeypatch.setattr(resident, 'require_agent', lambda c, aid: state)
    monkeypatch.setattr(resident, 'bound_account', lambda c, *, required: account)
    monkeypatch.setattr(resident, 'run', operate)
    monkeypatch.setattr(resident.browser.gateway, 'maintenance', asyncio.Event().wait)
    monkeypatch.setattr(resident.browser.gateway, 'close', AsyncMock())
    monkeypatch.setattr(resident, 'require_original_profile', lambda: None)
    monkeypatch.setattr(resident.browser, 'open_browser', AsyncMock(return_value={'session_id':'test'}))
    task = asyncio.create_task(resident.serve())
    try:
        await asyncio.wait_for(listening.wait(), 1)
        assert not operating.is_set()
        state['desired_state'] = 'paused'  # /connect pauses before opening the browser.
        headers = {'Authorization':'Bearer ' + resident.browser.internal_token()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=headers) as client:
            assert (await client.post('/agents/'+resident.AID+'/open', json={'platform':'telegram'})).status_code == 200
        account = '42'
        await asyncio.sleep(2.1)
        assert not operating.is_set()
        state['desired_state'] = 'running'
        await asyncio.wait_for(operating.wait(), 4)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
