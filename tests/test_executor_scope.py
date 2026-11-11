"""A private executor cannot fall back to another profile through shared routes."""

import asyncio
from uuid import uuid4

import pytest
from lead_app import executor_scope


@pytest.mark.asyncio
async def test_concurrent_channel_scopes_do_not_change_each_other():
    first, second = str(uuid4()), str(uuid4())
    p1, p2 = str(uuid4()), str(uuid4())
    ready = asyncio.Event()

    async def lane(agent, profile):
        with executor_scope.profile(agent, profile):
            ready.set()
            await asyncio.sleep(0)
            assert executor_scope.selected_profile(agent) == profile
            with pytest.raises(ValueError, match="profile"):
                executor_scope.selected_profile(agent, str(uuid4()))
            with pytest.raises(ValueError, match="agent"):
                executor_scope.selected_profile(str(uuid4()), profile)
        assert executor_scope.selected_profile(agent) is None

    await asyncio.gather(lane(first, p1), lane(second, p2))
    assert executor_scope.selected_profile(first) is None


def test_scope_restores_after_failure_and_accepts_same_explicit_profile():
    agent, profile = str(uuid4()), str(uuid4())
    with pytest.raises(RuntimeError):
        with executor_scope.profile(agent, profile):
            assert executor_scope.selected_profile(agent, profile) == profile
            raise RuntimeError("channel failed")
    assert executor_scope.selected_profile(agent, profile) == profile


@pytest.mark.asyncio
async def test_shared_gateway_rejects_profile_escape_before_acquisition(monkeypatch):
    from lead_app import browser, domain

    agent, selected, other = domain.create_agent(), str(uuid4()), str(uuid4())

    def unexpected(*args, **kwargs):
        raise AssertionError("profile selection reached database")

    monkeypatch.setattr(browser, "tx", unexpected)
    with executor_scope.profile(agent, selected):
        with pytest.raises(ValueError, match="profile"):
            await browser.Gateway().get(agent, other)


@pytest.fixture
def scoped_profiles(monkeypatch):
    from lead_app import browser, domain
    from lead_app.browser_tools import BrowserTools
    from lead_app.db import j, one, tx, uid
    from lead_app.scope import current_tenant

    aid = domain.create_agent()
    with tx(aid) as c:
        default = one(c, "SELECT * FROM lead.browser_profiles WHERE slot='default'")
        selected = one(
            c,
            """INSERT INTO lead.browser_profiles
            (tenant_id,agent_id,provider,engine_version,sdk_version,config_ref,config_hash,
             storage_ref,key_material_ref,network_identity_id,slot)
            SELECT tenant_id,agent_id,provider,engine_version,sdk_version,config_ref,%s,
                   %s,key_material_ref,network_identity_id,'telegram-fixture'
            FROM lead.browser_profiles WHERE id=%s RETURNING *""",
            (uid(), uid(), default["id"]),
        )
        accounts = []
        for profile in (default, selected):
            account = one(
                c,
                """INSERT INTO lead.social_accounts
                (tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled,verified_at)
                VALUES(%s,%s,%s,'telegram','connected',%s,true,now()) RETURNING *""",
                (current_tenant(), aid, profile["id"], uid()),
            )
            accounts.append(account)
            c.execute(
                """INSERT INTO lead.interaction_cards
                (tenant_id,agent_id,type,state,payload,expires_at)
                VALUES(%s,%s,'manage_community','accepted',%s,now()+interval '1 hour')""",
                (
                    current_tenant(),
                    aid,
                    j(
                        {
                            "account_id": str(account["id"]),
                            "external_account_id": account["external_account_id"],
                        }
                    ),
                ),
            )
    launched = []

    async def launch(actor):
        launched.append(str(actor.profile["id"]))
        actor.page = object()
        actor.session = uuid4()

    async def forbid_external_browser(*args, **kwargs):
        raise AssertionError("Scope tests must not call an external browser gateway")

    monkeypatch.setattr(BrowserTools, "command", forbid_external_browser)
    monkeypatch.setattr(browser.Actor, "launch", launch)
    gateway = browser.Gateway()
    monkeypatch.setattr(browser, "gateway", gateway)
    return aid, str(default["id"]), str(selected["id"]), accounts, launched


@pytest.mark.asyncio
async def test_http_descendants_use_bound_profile_for_search_and_research(
    monkeypatch, scoped_profiles
):
    import httpx
    from lead_app import browser, community_search, hybrid_research, research
    from lead_app.db import one, tx, uid
    from lead_app.scope import current_tenant

    aid, default, selected, accounts, launched = scoped_profiles
    command_run, hybrid_run = uid(), uid()
    with tx(aid) as c:
        agent = one(c, "SELECT * FROM lead.agents")
        for run in (command_run, hybrid_run):
            c.execute(
                """INSERT INTO lead.browser_runs
                (id,tenant_id,agent_id,goal,control_version,config_revision)
                VALUES(%s,%s,%s,'fixture',%s,%s)""",
                (
                    run,
                    current_tenant(),
                    aid,
                    agent["control_version"],
                    agent["config_revision"],
                ),
            )

    async def search(actor, task_id, query):
        return {"profile": str(actor.profile["id"])}

    async def command(controller, operation, args, command_id):
        return {"profile": str(controller.actor.profile["id"])}

    async def ensure(controller):
        controller.cdp_url = "fixture:" + str(controller.actor.profile["id"])

    monkeypatch.setattr(community_search, "native_search", search)
    monkeypatch.setattr(research.ResearchPage, "command", command)
    monkeypatch.setattr(hybrid_research.HybridResearchPage, "ensure", ensure)
    monkeypatch.setattr(browser, "internal_token", lambda: "fixture-token")
    with executor_scope.profile(aid, selected):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=browser.app),
            base_url="http://fixture",
            headers={"authorization": "Bearer fixture-token"},
        ) as client:
            responses = await asyncio.gather(
                client.post(
                    f"/agents/{aid}/community/search",
                    json={"task_id": uid(), "query": "reading"},
                ),
                client.post(
                    f"/agents/{aid}/research/command",
                    json={
                        "run_id": command_run,
                        "command_id": uid(),
                        "operation": "observe",
                    },
                ),
                client.post(f"/agents/{aid}/research/{hybrid_run}/hybrid"),
            )
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert [r.json() for r in responses] == [
        {"profile": selected},
        {"profile": selected},
        {"cdp_url": "fixture:" + selected},
    ]
    assert launched == [selected]
    assert default not in browser.gateway.actors


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["open", "inspect", "screenshot", "input"])
async def test_manual_routes_reject_other_account_or_takeover_before_browser_launch(
    monkeypatch, scoped_profiles, route
):
    from lead_app import account_profiles, browser

    aid, default, selected, accounts, launched = scoped_profiles
    monkeypatch.setattr(account_profiles, "takeover_profile", lambda *args: default)
    with (
        executor_scope.profile(aid, selected),
        pytest.raises(ValueError, match="profile"),
    ):
        if route == "open":
            await browser.open_browser(
                aid,
                browser.OpenRequest(platform="telegram", account_id=accounts[0]["id"]),
            )
        elif route == "inspect":
            await browser.inspect_account(
                aid,
                browser.OpenRequest(platform="telegram", account_id=accounts[0]["id"]),
            )
        elif route == "screenshot":
            await browser.screenshot(aid, takeover=uuid4())
        else:
            await browser.user_input(
                aid,
                browser.InputRequest(takeover=uuid4(), kind="text", text="not sent"),
            )
    assert launched == []
    assert browser.gateway.actors == {}


@pytest.mark.asyncio
async def test_inspect_uses_identity_from_actual_selected_profile(
    monkeypatch, scoped_profiles
):
    from types import SimpleNamespace

    from lead_app import adapters, browser, telegram
    from lead_app.db import tx

    aid, default, selected, accounts, launched = scoped_profiles
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET verified_at=now()+interval '1 day' WHERE id=%s",
            (accounts[0]["id"],),
        )
    opened = []

    async def open_account(page, identity):
        opened.append(identity)

    async def account(page):
        return {"external_account_id": opened[-1]}

    monkeypatch.setattr(telegram, "open_account", open_account)
    monkeypatch.setattr(
        adapters, "get_adapter", lambda platform: SimpleNamespace(account=account)
    )
    with executor_scope.profile(aid, selected):
        result = await browser.inspect_account(
            aid, browser.OpenRequest(platform="telegram", account_id=accounts[1]["id"])
        )
    assert result == {"external_account_id": accounts[1]["external_account_id"]}
    assert opened == [accounts[1]["external_account_id"]]
    assert launched == [selected]


@pytest.mark.asyncio
async def test_close_research_cannot_close_another_profile(
    monkeypatch, scoped_profiles
):
    from types import SimpleNamespace

    from lead_app import browser

    aid, default, selected, accounts, launched = scoped_profiles
    rid = str(uuid4())
    closed = []

    async def close():
        closed.append(True)

    controller = SimpleNamespace(close=close)
    actor = SimpleNamespace(
        aid=aid,
        profile={"id": default},
        lock=asyncio.Lock(),
        research_pages={rid: controller},
    )
    browser.gateway.actors[aid] = actor
    with (
        executor_scope.profile(aid, selected),
        pytest.raises(ValueError, match="profile"),
    ):
        await browser.close_research(aid, rid)
    assert closed == [] and actor.research_pages[rid] is controller


def setup_discovery(monkeypatch, aid, *, contextual=False):
    from lead_app import community_growth
    from lead_app.db import one, tx
    from lead_app.scope import current_tenant

    monkeypatch.setattr(community_growth, "emit", lambda *args, **kwargs: None)
    community_growth.set_strategy(
        aid,
        {"enabled": True, "mode": "contextual_outreach" if contextual else "discovery"},
    )
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            """INSERT INTO lead.business_profiles
            (tenant_id,agent_id,revision,description,facts,source_refs)
            VALUES(%s,%s,1,'reading fixture','{}','[]')""",
            (current_tenant(), aid),
        )
        return one(c, "SELECT * FROM lead.agents")


def discovery_checkpoint(aid, agent, binding, *, state="running"):
    from lead_app.db import j, one, tx, uid
    from lead_app.scope import current_tenant

    with tx(aid) as c:
        return one(
            c,
            """INSERT INTO lead.tasks
            (tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
            VALUES(%s,%s,'community.discover',%s,%s,%s,%s,now()) RETURNING *""",
            (
                current_tenant(),
                aid,
                state,
                "fixture:" + uid(),
                j(
                    {
                        "checkpoint_version": "2",
                        "control_version": agent["control_version"],
                        "config_revision": agent["config_revision"],
                        **binding,
                    }
                ),
                j(
                    {
                        "phase": "assess",
                        "cursor": 0,
                        "queue": ["https://example.org"],
                        "link_extractor_version": 5,
                        "sources": [],
                        "assessed_urls": [],
                        "errors": [],
                        "observed_candidates": [
                            {
                                "name": "fixture",
                                "telegram_url": "https://t.me/reading_fixture",
                                "source_url": "https://example.org",
                                "page_excerpt": "Reading group",
                            }
                        ],
                    }
                ),
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding_kind", ["other_profile", "other_account", "legacy_public"]
)
@pytest.mark.parametrize("state", ["running", "failed", "succeeded"])
async def test_discovery_skips_incompatible_checkpoint_without_rewriting(
    monkeypatch, scoped_profiles, binding_kind, state
):
    from lead_app import community_discovery
    from lead_app.db import one, tx

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid)
    binding = (
        {"channel_profile_id": default}
        if binding_kind == "other_profile"
        else (
            {
                "account_id": str(accounts[0]["id"]),
                "external_account_id": accounts[0]["external_account_id"],
            }
            if binding_kind == "other_account"
            else {}
        )
    )
    task = discovery_checkpoint(aid, agent, binding, state=state)

    from lead_app import admin_settings, community_growth
    from lead_app.db import all_rows

    monkeypatch.setattr(admin_settings, "tool_enabled", lambda *args: True)
    calls = []

    async def local(result_type, *args, **kwargs):
        calls.append(result_type)
        raise TimeoutError("local checkpoint wait")

    with executor_scope.profile(aid, selected):
        for _ in range(2):
            await community_growth.discover_communities(aid, invoke_model=local)
    with tx(aid) as c:
        unchanged = one(
            c, "SELECT input,result,state FROM lead.tasks WHERE id=%s", (task["id"],)
        )
        fresh = all_rows(
            c,
            "SELECT * FROM lead.tasks WHERE type='community.discover' AND id<>%s",
            (task["id"],),
        )
    assert unchanged == {key: task[key] for key in ("input", "result", "state")}
    assert calls == [community_discovery.QueryPlan, community_discovery.QueryPlan]
    assert len(fresh) == 1
    assert fresh[0]["input"]["channel_profile_id"] == selected
    assert fresh[0]["result"]["execution_steps_count"] == 2
    assert launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding_kind", ["stamped", "legacy_account", "legacy_default"]
)
async def test_discovery_preserves_verified_original_checkpoint_binding(
    monkeypatch, scoped_profiles, binding_kind
):
    from lead_app import community_discovery, community_growth
    from lead_app.db import one, tx

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid)
    binding = (
        {"channel_profile_id": selected}
        if binding_kind == "stamped"
        else (
            {
                "account_id": str(accounts[1]["id"]),
                "external_account_id": accounts[1]["external_account_id"],
            }
            if binding_kind == "legacy_account"
            else {}
        )
    )
    task = discovery_checkpoint(aid, agent, binding)
    calls = []

    async def local(*args, **kwargs):
        calls.append(True)
        return community_growth.Discovery(candidates=[], summary="Local assessment")

    with executor_scope.profile(
        aid, default if binding_kind == "legacy_default" else selected
    ):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        saved = one(c, "SELECT input,result FROM lead.tasks WHERE id=%s", (task["id"],))
    assert calls == [True]
    assert saved["input"] == task["input"]
    assert saved["result"]["assessed_urls"] == ["https://t.me/reading_fixture"]


@pytest.mark.asyncio
async def test_new_discovery_stamps_scope_and_selects_its_own_account(
    monkeypatch, scoped_profiles
):
    from lead_app import community_discovery
    from lead_app.db import one, tx

    aid, default, selected, accounts, launched = scoped_profiles
    setup_discovery(monkeypatch, aid, contextual=True)
    calls = []

    async def local(result_type, *args, **kwargs):
        calls.append(result_type)
        return community_discovery.QueryPlan(
            queries=["reading"], rationale="local fixture"
        )

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        task = one(c, "SELECT * FROM lead.tasks WHERE type='community.discover'")
    assert calls == [community_discovery.QueryPlan]
    assert task["input"]["channel_profile_id"] == selected
    assert task["input"]["account_id"] == str(accounts[1]["id"])
    assert task["input"]["external_account_id"] == accounts[1]["external_account_id"]
    assert launched == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["different_account", "changed_identity", "missing_identity"]
)
async def test_stamped_checkpoint_still_verifies_original_account_identity(
    monkeypatch, scoped_profiles, fault
):
    from lead_app import community_discovery
    from lead_app.db import one, tx

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid)
    account = accounts[0] if fault == "different_account" else accounts[1]
    binding = {"channel_profile_id": selected, "account_id": str(account["id"])}
    if fault != "missing_identity":
        binding["external_account_id"] = (
            "stale-identity"
            if fault == "changed_identity"
            else account["external_account_id"]
        )
    task = discovery_checkpoint(aid, agent, binding)

    async def local(*args, **kwargs):
        return community_discovery.QueryPlan(
            queries=["reading"], rationale="fresh account"
        )

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        saved = one(
            c, "SELECT input,result,state FROM lead.tasks WHERE id=%s", (task["id"],)
        )
    assert saved == {key: task[key] for key in ("input", "result", "state")}


@pytest.mark.asyncio
async def test_rotated_request_keeps_old_evidence_and_starts_new_bound_discovery(
    monkeypatch, scoped_profiles
):
    from lead_app import community_discovery
    from lead_app.db import all_rows, j, tx, uid

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid, contextual=True)
    old = discovery_checkpoint(
        aid,
        agent,
        {
            "channel_profile_id": default,
            "account_id": str(accounts[0]["id"]),
            "external_account_id": accounts[0]["external_account_id"],
        },
    )
    request = uid()
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
            (j({"community_research_request": request}), aid),
        )

    async def local(*args, **kwargs):
        return community_discovery.QueryPlan(
            queries=["reading"], rationale="local fixture"
        )

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        rows = all_rows(
            c,
            "SELECT * FROM lead.tasks WHERE type='community.discover' ORDER BY created_at",
        )
    assert len(rows) == 2
    assert rows[0]["id"] == old["id"] and rows[0]["state"] == "running"
    assert rows[0]["input"] == old["input"] and rows[0]["result"] == old["result"]
    assert rows[1]["input"]["research_request"] == request
    assert rows[1]["input"]["channel_profile_id"] == selected
    assert rows[1]["input"]["account_id"] == str(accounts[1]["id"])


@pytest.mark.asyncio
async def test_inspection_keeps_explicit_account_with_multiple_logins_in_profile(
    monkeypatch, scoped_profiles
):
    from types import SimpleNamespace

    from lead_app import adapters, browser, telegram
    from lead_app.db import tx, uid
    from lead_app.scope import current_tenant

    aid, default, selected, accounts, launched = scoped_profiles
    with tx(aid) as c:
        c.execute(
            """INSERT INTO lead.social_accounts
            (tenant_id,agent_id,profile_id,platform,state,external_account_id,verified_at)
            VALUES(%s,%s,%s,'telegram','connected',%s,now()+interval '1 day')""",
            (current_tenant(), aid, selected, uid()),
        )
    opened = []

    async def open_account(page, identity):
        opened.append(identity)

    async def account(page):
        return {"external_account_id": opened[-1]}

    monkeypatch.setattr(telegram, "open_account", open_account)
    monkeypatch.setattr(
        adapters, "get_adapter", lambda platform: SimpleNamespace(account=account)
    )
    with executor_scope.profile(aid, selected):
        result = await browser.inspect_account(
            aid, browser.OpenRequest(platform="telegram", account_id=accounts[1]["id"])
        )
    assert result == {"external_account_id": accounts[1]["external_account_id"]}
    assert opened == [accounts[1]["external_account_id"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("new_request", [False, True])
async def test_replaced_account_in_same_profile_never_reuses_or_cancels_old_checkpoint(
    monkeypatch, scoped_profiles, new_request
):
    from lead_app import community_discovery
    from lead_app.db import all_rows, j, one, tx, uid
    from lead_app.scope import current_tenant

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid, contextual=True)
    original = accounts[1]
    old = discovery_checkpoint(
        aid,
        agent,
        {
            "channel_profile_id": selected,
            "account_id": str(original["id"]),
            "external_account_id": original["external_account_id"],
        },
    )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.social_accounts SET state='disconnected',automation_enabled=false WHERE id=%s",
            (original["id"],),
        )
        replacement = one(
            c,
            """INSERT INTO lead.social_accounts
            (tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled)
            VALUES(%s,%s,%s,'telegram','connected',%s,true) RETURNING *""",
            (current_tenant(), aid, selected, uid()),
        )
        c.execute(
            """INSERT INTO lead.interaction_cards
            (tenant_id,agent_id,type,state,payload,expires_at)
            VALUES(%s,%s,'manage_community','accepted',%s,now()+interval '1 hour')""",
            (
                current_tenant(),
                aid,
                j(
                    {
                        "account_id": str(replacement["id"]),
                        "external_account_id": replacement["external_account_id"],
                    }
                ),
            ),
        )
        if new_request:
            c.execute(
                "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                (j({"community_research_request": uid()}), aid),
            )

    async def local(*args, **kwargs):
        return community_discovery.QueryPlan(
            queries=["reading"], rationale="new account"
        )

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        rows = all_rows(
            c,
            "SELECT * FROM lead.tasks WHERE type='community.discover' ORDER BY created_at",
        )
    assert len(rows) == 2
    assert {key: rows[0][key] for key in ("input", "result", "state")} == {
        key: old[key] for key in ("input", "result", "state")
    }
    assert rows[1]["input"]["account_id"] == str(replacement["id"])
    assert rows[1]["input"]["external_account_id"] == replacement["external_account_id"]
    assert rows[1]["input"]["channel_profile_id"] == selected


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "failed", "succeeded"])
async def test_foreign_checkpoint_never_masks_existing_owned_checkpoint(
    monkeypatch, scoped_profiles, state
):
    from lead_app import community_discovery, community_growth
    from lead_app.db import all_rows, j, tx

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid)
    if state == "running":
        foreign = discovery_checkpoint(
            aid, agent, {"channel_profile_id": default}, state=state
        )
        owned = discovery_checkpoint(
            aid, agent, {"channel_profile_id": selected}, state=state
        )
    else:
        owned = discovery_checkpoint(
            aid, agent, {"channel_profile_id": selected}, state=state
        )
        foreign = discovery_checkpoint(
            aid, agent, {"channel_profile_id": default}, state=state
        )
    if state == "succeeded":
        # This finished batch has pending query planning for its original account.
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.tasks SET input=input || %s,result=result || %s WHERE id=%s",
                (
                    j(
                        {
                            "account_id": str(accounts[1]["id"]),
                            "external_account_id": accounts[1]["external_account_id"],
                            "plan_before_native": True,
                        }
                    ),
                    j({"native_selector_version": 8, "native_cursor": 0}),
                    owned["id"],
                ),
            )
    calls = []

    async def local(result_type, *args, **kwargs):
        calls.append(result_type)
        if result_type is community_discovery.QueryPlan:
            return result_type(queries=["reading"], rationale="current owner")
        return community_growth.Discovery(candidates=[], summary="current owner")

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    with tx(aid) as c:
        rows = all_rows(c, "SELECT * FROM lead.tasks WHERE type='community.discover'")
    assert len(rows) == 2
    by_id = {row["id"]: row for row in rows}
    assert calls == [
        community_discovery.QueryPlan
        if state == "succeeded"
        else community_growth.Discovery
    ]
    assert {key: by_id[foreign["id"]][key] for key in ("input", "result", "state")} == {
        key: foreign[key] for key in ("input", "result", "state")
    }
    if state == "succeeded":
        assert by_id[owned["id"]]["result"]["query_plan_rounds"] == 1
    else:
        assert by_id[owned["id"]]["result"]["assessed_urls"] == [
            "https://t.me/reading_fixture"
        ]


def scheduled_work(
    aid,
    account,
    *,
    task_type="telegram.publish",
    state="running",
    payload=None,
    result=None,
    unknown=False,
):
    from lead_app.db import j, one, tx, uid
    from lead_app.scope import current_tenant

    with tx(aid) as c:
        agent = one(c, "SELECT * FROM lead.agents")
        task = one(
            c,
            """INSERT INTO lead.tasks
            (tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,now()) RETURNING *""",
            (
                current_tenant(),
                aid,
                task_type,
                state,
                uid(),
                j(
                    payload
                    if payload is not None
                    else {
                        "account_id": str(account["id"]),
                        "external_account_id": account["external_account_id"],
                    }
                ),
                j(result or {}),
            ),
        )
        if unknown:
            target = one(
                c,
                """INSERT INTO lead.account_targets
                (tenant_id,agent_id,account_id,target_type,external_target_id,display_name,verified_permissions,state)
                VALUES(%s,%s,%s,'channel',%s,'fixture','[]','active') RETURNING id""",
                (current_tenant(), aid, account["id"], uid()),
            )
            grant = one(
                c,
                """INSERT INTO lead.authorization_grants
                (tenant_id,agent_id,target_id,version,allowed_actions,granted_by)
                VALUES(%s,%s,%s,1,'[]','fixture') RETURNING id""",
                (current_tenant(), aid, target["id"]),
            )
            c.execute(
                """INSERT INTO lead.actions
                (tenant_id,agent_id,task_id,target_id,grant_id,operation_key,type,intent,
                 expected_control_version,expected_config_revision,state)
                VALUES(%s,%s,%s,%s,%s,%s,'publish','{}',%s,%s,'unknown')""",
                (
                    current_tenant(),
                    aid,
                    task["id"],
                    target["id"],
                    grant["id"],
                    uid(),
                    agent["control_version"],
                    agent["config_revision"],
                ),
            )
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", [False, True])
@pytest.mark.parametrize("owner", ["own", "other_telegram", "reddit"])
async def test_discovery_only_waits_for_work_in_its_own_profile(
    monkeypatch, scoped_profiles, focus, owner
):
    from lead_app import community_discovery
    from lead_app.db import j, one, tx, uid
    from lead_app.scope import current_tenant

    aid, default, selected, accounts, launched = scoped_profiles
    setup_discovery(monkeypatch, aid, contextual=True)
    with tx(aid) as c:
        if focus:
            c.execute(
                "UPDATE lead.agents SET onboarding_state=onboarding_state || %s WHERE id=%s",
                (
                    j(
                        {
                            "growth_trial_policy": {
                                "mode": "active",
                                "focus": "first_users",
                            }
                        }
                    ),
                    aid,
                ),
            )
        account = accounts[1] if owner == "own" else accounts[0]
        if owner == "reddit":
            account = one(
                c,
                """INSERT INTO lead.social_accounts
                (tenant_id,agent_id,profile_id,platform,state,external_account_id,automation_enabled)
                VALUES(%s,%s,%s,'reddit','connected',%s,true) RETURNING *""",
                (current_tenant(), aid, default, uid()),
            )
    scheduled_work(
        aid,
        account,
        task_type="reddit.publish" if owner == "reddit" else "telegram.publish",
        unknown=True,
    )
    calls = []

    async def local(result_type, *args, **kwargs):
        calls.append(result_type)
        return community_discovery.QueryPlan(
            queries=["reading"], rationale="scope fixture"
        )

    with executor_scope.profile(aid, selected):
        await community_discovery.advance(aid, invoke_model=local)
    assert calls == ([] if owner == "own" else [community_discovery.QueryPlan])
    with tx(aid) as c:
        # No scope keeps the existing agent-wide nonfocus and Telegram-only focus policy.
        assert bool(
            community_discovery.account_discovery_blocked(c, focus, aid=aid)
        ) == (not focus or owner != "reddit")


@pytest.mark.parametrize("field", ["channel_profile_id", "profile_id"])
@pytest.mark.parametrize("owned", [False, True])
def test_nonfocus_discovery_respects_explicit_profile_work(
    scoped_profiles, field, owned
):
    from lead_app import community_discovery
    from lead_app.db import tx

    aid, default, selected, accounts, launched = scoped_profiles
    scheduled_work(
        aid,
        accounts[1],
        task_type="account.prepare",
        payload={field: selected if owned else default},
    )
    with executor_scope.profile(aid, selected), tx(aid) as c:
        assert (
            bool(community_discovery.account_discovery_blocked(c, False, aid=aid))
            == owned
        )


@pytest.mark.parametrize(
    "kind,state,payload,result,blocked",
    [
        ("community.engage", "reconciling", {}, {}, False),
        (
            "community.engage",
            "running",
            {"reply": {"standalone": True}},
            {
                "reply_state": "unknown",
                "receipt_review_after": "2099-01-01T00:00:00+00:00",
            },
            False,
        ),
        ("community.engage", "running", {}, {}, True),
        ("community.discover", "running", {}, {}, False),
        (
            "account.prepare",
            "succeeded",
            {"fresh_channel": True},
            {"awaiting_first_publish": True},
            True,
        ),
    ],
)
def test_nonfocus_scope_preserves_existing_receipt_and_fresh_channel_gates(
    scoped_profiles, kind, state, payload, result, blocked
):
    from lead_app import community_discovery
    from lead_app.db import tx

    aid, default, selected, accounts, launched = scoped_profiles
    scheduled_work(
        aid,
        accounts[1],
        task_type=kind,
        state=state,
        payload={"account_id": str(accounts[1]["id"]), **payload},
        result=result,
    )
    with executor_scope.profile(aid, selected), tx(aid) as c:
        assert (
            bool(community_discovery.account_discovery_blocked(c, False, aid=aid))
            == blocked
        )


@pytest.mark.parametrize("owned", [False, True])
def test_candidate_pressure_only_counts_current_profile_without_changing_authority(
    monkeypatch, scoped_profiles, owned
):
    from datetime import datetime, timezone

    from lead_app import community_growth, community_opportunities
    from lead_app.db import j, tx

    aid, default, selected, accounts, launched = scoped_profiles
    agent = setup_discovery(monkeypatch, aid, contextual=True)
    account = accounts[1] if owned else accounts[0]
    task = discovery_checkpoint(
        aid,
        agent,
        {
            "account_id": str(account["id"]),
            "external_account_id": account["external_account_id"],
        },
        state="succeeded",
    )
    with tx(aid) as c:
        c.execute(
            "UPDATE lead.tasks SET completed_at=now(),result=%s WHERE id=%s",
            (
                j(
                    {
                        "candidates": [
                            {"telegram_url": f"https://t.me/fixture{i}"}
                            for i in range(5)
                        ]
                    }
                ),
                task["id"],
            ),
        )
        c.execute(
            """UPDATE lead.interaction_cards SET payload=payload || %s
            WHERE payload->>'account_id'=%s""",
            (
                j(
                    {
                        "purpose": "bounded_candidate_participation",
                        "allowed_actions": ["join_public_group", "read_recent_history"],
                        "discovery_task": str(task["id"]),
                    }
                ),
                str(account["id"]),
            ),
        )
    config = community_growth.GrowthConfig(enabled=True, mode="contextual_outreach")
    now = datetime(2026, 11, 11, 12, 5, tzinfo=timezone.utc)
    with executor_scope.profile(aid, selected), tx(aid) as c:
        pressure = community_opportunities.discovery_pressure(c, agent, config, now)
        assert pressure == (
            {"reason": "candidate_backlog", "ready": 5, "delay": 120} if owned else None
        )
        c.execute("UPDATE lead.interaction_cards SET state='expired'")
        assert community_opportunities.discovery_pressure(c, agent, config, now) is None
