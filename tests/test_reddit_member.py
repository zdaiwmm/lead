"""Ordinary-member posting uses real isolated DB/browser; no live Reddit writes."""

import os

import pytest
from lead_app import (
    activities,
    browser,
    channel_delivery,
    channel_research,
    domain,
    reddit_member,
)
from lead_app.account_profiles import for_platform
from lead_app.adapters.core import ScopeChanged
from lead_app.adapters.web import WebAdapter
from lead_app.browser import Gateway
from lead_app.db import j, one, tx, uid
from lead_app.scope import current_tenant
from test_adapters import ROOT, TARGETS, FixtureSite
from test_channel_delivery import site_database as site_database
from test_migrations import isolated_database as isolated_database

RULES = "Relevant product feedback posts are welcome. Disclose promotional affiliation and automated participation. No unsolicited direct messages."


@pytest.mark.parametrize("focus", ["first_users", "bounded_trial", "chamber_first_users"])
@pytest.mark.parametrize("member", [False, True])
def test_campaign_clock_handoff_preserves_other_experiment_ownership(focus, member, monkeypatch):
    monkeypatch.setenv("LEAD_CHANNEL_ATOM_AGENT_IDS", "fixture-agent")
    agent = dict(id="fixture-agent", onboarding_state={
        "reddit_member_delivery": member,
        "growth_trial_policy": {"mode": "active", "focus": focus},
    })
    assert channel_delivery.owns(agent, "reddit") == (member and focus == "chamber_first_users")
    assert not channel_delivery.owns(agent, "telegram")
    assert not channel_delivery.owns(agent, "facebook")


class MemberSite(FixtureSite):
    rules = RULES
    blocked = False

    async def route(self, route):
        if "/__fixture/" in route.request.url:
            return await super().route(route)
        import json

        html = (
            (ROOT / "reddit.html")
            .read_text()
            .replace('<a href="/mod/lead_sub">Mod tools</a>', "")
        )
        html = html.replace("__POSTS__", json.dumps(self.posts).replace("<", "\\u003c"))
        if "/about/rules" in route.request.url:
            html = (
                '<header><a href="https://www.reddit.com/user/lead_owner/">Profile</a></header><main>'
                + self.rules
                + "</main>"
            )
        if self.blocked and "/submit" in route.request.url:
            html = "<main>This community only accepts approved posters.</main>"
        await route.fulfill(body=html, content_type="text/html")


def setup_account():
    aid = domain.create_agent()
    profile = for_platform(aid, "reddit", create=True, isolated_login=True)
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        account = one(
            c,
            """UPDATE lead.social_accounts SET external_account_id='lead_owner',state='connected',automation_enabled=true
            WHERE id=%s RETURNING *""",
            (profile["account_id"],),
        )
        c.execute(
            "INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description,product_url) VALUES(%s,%s,1,'A team scheduling worksheet','https://site.example')",
            (current_tenant(), aid),
        )
    return aid, account, profile


@pytest.mark.parametrize("campaign", [False, True])
def test_member_scope_is_explicit_and_does_not_enroll_telegram(site_database, campaign, monkeypatch):
    aid, account, _ = setup_account()
    monkeypatch.setenv("LEAD_APPROVED_DISCOVERY_CHANNELS", "")
    with tx(aid) as c:
        if campaign:
            c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,
                '{growth_trial_policy}',%s) WHERE id=%s""",
                (j({"mode": "active", "focus": "chamber_first_users", "version": "v1"}), aid))
        c.execute("""INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,external_account_id,state,automation_enabled)
            SELECT tenant_id,agent_id,id,'telegram',gen_random_uuid()::text,'connected',true FROM lead.browser_profiles WHERE slot='default'""")
    reddit_member.authorize(aid, account["id"], account["binding_version"])
    with tx(aid) as c:
        assert one(
            c,
            "SELECT automation_enabled FROM lead.social_accounts WHERE platform='telegram'",
        )["automation_enabled"]
        assert reddit_member.member_scope(c, account)
        assert {
            r["input"]["platform"]
            for r in c.execute(
                "SELECT input FROM lead.tasks WHERE type='channel.clock'"
            )
        } == {"reddit"}
        clocks = list(c.execute("SELECT * FROM lead.tasks WHERE type='channel.clock'"))
        assert len(clocks) == 2
    for clock in clocks:
        args = dict(agent_id=aid, task_id=str(clock["id"]), generation=clock["version"], tick="first")
        if clock["input"]["lane"] == "research":
            assert channel_research.select(args) == ({"delay": 1800, "reason": "source_permission_required"}, None)
        else:
            assert channel_delivery.next_target(args)["reason"] == "no_available_verified_target"
    reddit_member.authorize(aid, account["id"], account["binding_version"])
    with tx(aid) as c:
        assert one(c, "SELECT count(*) AS n FROM lead.tasks WHERE type='channel.clock'")["n"] == 2
        c.execute(
            "UPDATE lead.social_accounts SET binding_version=binding_version+1 WHERE id=%s",
            (account["id"],),
        )
        changed = one(
            c, "SELECT * FROM lead.social_accounts WHERE id=%s", (account["id"],)
        )
        assert not reddit_member.member_scope(c, changed)
    with pytest.raises(ValueError, match="绑定已变化"):
        reddit_member.authorize(aid, account["id"], account["binding_version"])


@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="requires native disposable browser",
)
async def test_member_real_first_post_recovers_without_duplicate(
    site_database, monkeypatch
):
    import psycopg
    from lead_app.config import database_kwargs

    aid, account, profile = setup_account()
    reddit_member.authorize(aid, account["id"], account["binding_version"])
    with psycopg.connect(**database_kwargs(True)) as c:
        c.execute(
            "UPDATE lead.platform_capabilities SET permission_state='approved' WHERE platform='reddit'"
        )
    gateway = Gateway()
    actor = await gateway.get(aid, profile["id"])
    monkeypatch.setattr(browser, "gateway", gateway)
    site = MemberSite("reddit")
    await actor.context.route("https://**/*", site.route)

    async def rpc(path, data):
        if path.endswith("/target/inspect"):
            return await browser.inspect_target(
                aid, browser.TelegramTargetRequest(**data)
            )
        return await browser.publish_action(aid, browser.PublishRequest(**data))

    monkeypatch.setattr(activities, "browser_rpc", rpc)
    from lead_app import admin_operations

    admission = admin_operations.admission
    publish_lanes = []

    def record_admission(aid, category, **kwargs):
        if category == "publish":
            publish_lanes.append(kwargs.get("lane"))
        return admission(aid, category, **kwargs)

    monkeypatch.setattr(admin_operations, "admission", record_admission)
    try:
        adapter = WebAdapter("reddit")
        await actor.page.goto(TARGETS["reddit"])
        with pytest.raises(ScopeChanged, match="管理权限"):
            await adapter.target(actor.page, TARGETS["reddit"], "lead_owner")
        observed = await browser.inspect_target(
            aid,
            browser.TelegramTargetRequest(
                account_id=account["id"], url=TARGETS["reddit"]
            ),
        )
        assert observed["access_mode"] == "ordinary_member"
        assert observed["verified_permissions"] == ["publish"]
        with tx(aid) as c:
            agent = one(c, "SELECT * FROM lead.agents WHERE id=%s", (aid,))
            business = one(c, "SELECT * FROM lead.business_profiles")
            evidence = one(
                c,
                """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at)
                VALUES(%s,%s,'operating.validate','succeeded',gen_random_uuid()::text,%s,%s,now(),now()) RETURNING id""",
                (
                    current_tenant(),
                    aid,
                    j(
                        dict(
                            platform="reddit",
                            candidate_url=TARGETS["reddit"],
                            business_profile_id=str(business["id"]),
                            config_revision=agent["config_revision"],
                        )
                    ),
                    j(
                        dict(
                            status="observed",
                            channel_fit="viable",
                            source_urls=[TARGETS["reddit"]],
                            cold_entry=dict(
                                source_url=TARGETS["reddit"],
                                sharing_rule=RULES,
                                observation="Team handoff question",
                            ),
                        )
                    ),
                ),
            )["id"]
            clock = one(
                c,
                "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='research'",
            )
            candidate = channel_research.unbound_source(c, "reddit", agent, business)
            assert candidate["evidence_id"] == evidence

        async def draft(schema, context, **kwargs):
            assert context["untrusted_rules"] == site.rules
            return schema(
                suitable=True,
                reason="Feedback allowed",
                title="What is missing from this handoff worksheet?",
                body="I am an automated assistant helping promote this worksheet. "
                + context["untrusted_opportunity"]["cold_entry"]["observation"],
                disclosure="I am an automated assistant helping promote this worksheet.",
                rule_quote=site.rules,
            )

        monkeypatch.setattr("lead_app.model.run_model", draft)
        args = {
            "agent_id": aid,
            "task_id": str(clock["id"]),
            "generation": clock["version"],
        }
        assert (await channel_research.inspect_source(args, candidate))[
            "reason"
        ] == "target_verification_queued"
        with tx(aid) as c:
            assert channel_research.unbound_source(c, "reddit", agent, business) is None
            task = one(
                c, "SELECT * FROM lead.tasks WHERE type='connection.verify_publish'"
            )
            assert task["input"]["member_contribution"]
            assert "lead-verify-" not in task["input"]["body"]
            assert task["input"]["operating_plan"]["channel_opportunity"][
                "evidence_task_id"
            ] == str(evidence)
            assert channel_delivery.opportunity(c, task["input"]["target_id"]) is None
        action = activities.prepare_action(aid, str(task["id"]))
        # Changed rules cancel only work known not to have been submitted, then
        # independently prepare a fresh first contribution instead of stalling.
        old_task, old_action = task, action
        site.rules = RULES + " Keep titles specific."
        await channel_research.inspect_source(args, candidate)
        with tx(aid) as c:
            assert (
                one(
                    c, "SELECT state FROM lead.actions WHERE id=%s", (old_action["id"],)
                )["state"]
                == "canceled"
            )
            assert (
                one(c, "SELECT state FROM lead.tasks WHERE id=%s", (old_task["id"],))[
                    "state"
                ]
                == "canceled"
            )
            task = one(
                c,
                "SELECT * FROM lead.tasks WHERE type='connection.verify_publish' AND state='queued'",
            )
            assert task and task["id"] != old_task["id"]
        action = activities.prepare_action(aid, str(task["id"]))
        from lead_app.social_publish import gate

        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute(
                "UPDATE lead.platform_capabilities SET permission_state='pending' WHERE platform='reddit'"
            )
        with tx(aid) as c, pytest.raises(ScopeChanged, match="权限审查"):
            gate(actor, c, action)
        with psycopg.connect(**database_kwargs(True)) as c:
            c.execute(
                "UPDATE lead.platform_capabilities SET permission_state='approved' WHERE platform='reddit'"
            )
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.social_accounts SET automation_enabled=false WHERE id=%s",
                (account["id"],),
            )
        with tx(aid) as c, pytest.raises(ScopeChanged, match="普通成员"):
            gate(actor, c, action)
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.social_accounts SET automation_enabled=true WHERE id=%s",
                (account["id"],),
            )
        assert not site.posts
        original = WebAdapter.submit

        async def lost(self, page, observation):
            async with page.expect_response(lambda r: "/__fixture/publish" in r.url):
                await original(self, page, observation)
            raise ConnectionError("lost after submission")

        with monkeypatch.context() as fault:
            fault.setattr(WebAdapter, "submit", lost)
            first = await activities.execute_task(
                {"agent_id": aid, "task_id": str(task["id"])}
            )
        assert first["state"] == "unknown"
        assert len(site.posts) == 1
        resumed = await activities.execute_task(
            {"agent_id": aid, "task_id": str(task["id"])}
        )
        assert resumed["state"] == "confirmed", resumed
        assert len(site.posts) == 1
        with tx(aid) as c:
            target = one(
                c,
                "SELECT * FROM lead.account_targets WHERE id=%s",
                (task["input"]["target_id"],),
            )
            assert target["verified_permissions"]["publishing_verified_formats"] == [
                "text"
            ]
            assert domain.metrics(c)["published"] == 1
            c.execute(
                "UPDATE lead.actions SET created_at=now()-interval '25 hours',confirmed_at=now()-interval '25 hours' WHERE state='confirmed'"
            )
            c.execute(
                "UPDATE lead.tasks SET completed_at=now()-interval '25 hours' WHERE id=%s",
                (task["id"],),
            )
            next_evidence = one(
                c,
                """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at,completed_at)
                SELECT tenant_id,agent_id,type,state,gen_random_uuid()::text,input,
                jsonb_set(result-'account_inspections','{cold_entry,observation}',to_jsonb('How do you acknowledge a shift swap?'::text)),now(),now()
                FROM lead.tasks WHERE id=%s RETURNING id""",
                (evidence,),
            )["id"]
            delivery = one(
                c,
                "SELECT * FROM lead.tasks WHERE type='channel.clock' AND input->>'lane'='delivery'",
            )
        delivered = await channel_delivery.channel_delivery_tick(
            dict(
                agent_id=aid,
                task_id=str(delivery["id"]),
                generation=delivery["version"],
                tick=uid(),
            )
        )
        with tx(aid) as c:
            next_task = one(c, "SELECT * FROM lead.tasks WHERE type='reddit.publish'")
            assert next_task, delivered
            assert next_task["input"]["operating_plan"]["channel_opportunity"][
                "evidence_task_id"
            ] == str(next_evidence)
            assert "automated assistant" in next_task["input"]["body"]
        assert (
            await activities.execute_task(
                dict(agent_id=aid, task_id=str(next_task["id"]))
            )
        )["state"] == "confirmed"
        assert len(site.posts) == 2
        assert publish_lanes and set(publish_lanes) == {
            "channel:browser:" + str(profile["id"])
        }
        with tx(aid) as c:
            assert domain.metrics(c)["published"] == 2
        prepared = await adapter.prepare(actor.page, target, task["input"], [], [])
        site.rules = (
            "No promotional posts or automated accounts are allowed in this community."
        )
        with pytest.raises(ScopeChanged, match="提交前社区规则"):
            await adapter.before_commit(actor.page, target, prepared, "lead_owner")
        with pytest.raises(ScopeChanged, match="规则已更新"):
            await adapter.prepare(actor.page, target, task["input"], [], [])
        assert len(site.posts) == 2
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.account_targets SET verified_permissions=jsonb_set(verified_permissions,'{rules,checked_at}',to_jsonb((now()-interval '25 hours')::text))"
            )
            c.execute(
                "UPDATE lead.tasks SET result=result-'account_inspections' WHERE id=%s",
                (evidence,),
            )
            assert channel_research.unbound_source(c, "reddit", agent, business)
    finally:
        await gateway.close(aid)
