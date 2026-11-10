from lead_app.dispatch_plan import shadow_batch
from lead_app.operating_kernel import Candidate


def choice(kind, key=None, operation=None):
    return Candidate(key or kind, kind, 1, "test", community_operation=operation)


def test_skip_busy_session_and_fill_independent_slot():
    work = [choice("audience"), choice("community_progress"), choice("content")]
    assert [item["key"] for item in shadow_batch(work)] == ["audience", "content"]


def test_compound_work_is_exclusive_and_unrecognized_work_stays_serial():
    for kind in ("publish", "account", "new_device_adapter"):
        assert len(shadow_batch([choice(kind), choice("kits")])) == 1
    batch = shadow_batch([choice("kits"), choice("publish"), choice("audience")])
    assert [item["key"] for item in batch] == ["kits", "audience"]


def test_paid_work_and_duplicate_candidates_do_not_compete_for_one_slot():
    work = [
        choice("operating.strategy"),
        choice("content"),
        choice("operating.strategy"),
        choice("community_progress", operation={"kind": "receipt", "paid": False}),
    ]
    assert [item["key"] for item in shadow_batch(work)] == [
        "operating.strategy",
        "community_progress",
    ]


def test_offline_dispatch_replay_pause_and_zero_budget(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from lead_app import atomic_dispatch, budget, domain
    from lead_app.db import all_rows, one, require_agent, tx
    from lead_app.scope import current_tenant

    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_PARALLEL_AGENT_IDS", str(aid))
    try:
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,)
            )
            budget.apply_daily_limit(c, aid, 0)
            business = one(
                c,
                """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description)
                VALUES(%s,%s,1,'fixture') RETURNING *""",
                (current_tenant(), aid),
            )
            agent = require_agent(c, aid, True)
            for _ in range(2):
                atomic_dispatch.enqueue_channel_work(c, aid, agent, business)
            jobs = all_rows(
                c, "SELECT id FROM lead.tasks WHERE type='dispatch.channel' ORDER BY id"
            )
            assert len(jobs) >= 2
            assert (
                len(jobs)
                == one(
                    c,
                    "SELECT count(*) AS n FROM lead.outbox_events WHERE event_type='dispatch.channel'",
                )["n"]
            )
        with ThreadPoolExecutor(2) as pool:
            list(
                pool.map(
                    lambda _: atomic_dispatch.execute_channel(aid, jobs[0]["id"]),
                    range(2),
                )
            )
        with tx(aid) as c:
            assert (
                one(
                    c, "SELECT count(*) n FROM lead.tasks WHERE type='channel.prepare'"
                )["n"]
                == 1
            )
            assert (
                one(c, "SELECT state FROM lead.tasks WHERE id=%s", (jobs[0]["id"],))[
                    "state"
                ]
                == "succeeded"
            )
            assert budget.available(c, aid)["available_units"] == 0
            c.execute(
                "UPDATE lead.agents SET desired_state='paused', control_version=control_version+1 WHERE id=%s",
                (aid,),
            )
        atomic_dispatch.execute_channel(aid, jobs[1]["id"])
        with tx(aid) as c:
            assert (
                one(c, "SELECT state FROM lead.tasks WHERE id=%s", (jobs[1]["id"],))[
                    "state"
                ]
                == "canceled"
            )
    finally:
        domain.set_control(aid, "archive")


def test_model_wait_keeps_discovery_cursor_and_does_not_spend_retry():
    from lead_app import domain
    from lead_app.budget import BudgetExceeded
    from lead_app.community_discovery import model_wait
    from lead_app.db import j, one, tx, uid
    from lead_app.scope import current_tenant

    aid = domain.create_agent()
    try:
        with tx(aid) as c:
            task = one(
                c,
                """INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
                VALUES(%s,%s,'community.discover','running',%s,'{}',%s,now()) RETURNING id""",
                (
                    current_tenant(),
                    aid,
                    uid(),
                    j({"phase": "assess", "cursor": 2, "model_attempts": 1}),
                ),
            )
        model_wait(
            aid,
            task["id"],
            "assess",
            BudgetExceeded("daily_token_limit", required_units=800),
        )
        with tx(aid) as c:
            saved = one(
                c, "SELECT state,result FROM lead.tasks WHERE id=%s", (task["id"],)
            )
            assert saved["state"] == "running"
            assert saved["result"] == {
                "phase": "assess",
                "cursor": 2,
                "model_attempts": 1,
                "model_wait": {
                    "phase": "assess",
                    "reason": "daily_token_limit",
                    "required_units": 800,
                },
            }
    finally:
        domain.set_control(aid, "archive")


def test_source_only_work_leaves_model_free_and_reserves_the_real_actor():
    read = choice("operating.journey")
    read.read_source = True
    assert [row["key"] for row in shadow_batch([read, choice("audience"), choice("operating.strategy")])] == [
        "operating.journey", "operating.strategy"
    ]


def test_versioned_channel_product_survives_control_changes(monkeypatch):
    from lead_app import atomic_dispatch, domain
    from lead_app.db import one, require_agent, tx
    from lead_app.operating_workspace import save_workspace
    from lead_app.scope import current_tenant

    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_PARALLEL_AGENT_IDS", str(aid))
    with tx(aid) as c:
        business = one(c, """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description)
            VALUES(%s,%s,1,'test') RETURNING *""", (current_tenant(),aid))
        agent = require_agent(c,aid,True)
        platform = atomic_dispatch.catalog()["channels"][0]["platform"]
        save_workspace(c, aid, agent, business, platform)
        c.execute("UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s", (aid,))
        atomic_dispatch.enqueue_channel_work(c, aid, require_agent(c,aid,True), business)
        assert not one(c,"SELECT 1 FROM lead.tasks WHERE type='dispatch.channel' AND input->>'platform'=%s",(platform,))
