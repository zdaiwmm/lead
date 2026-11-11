from concurrent.futures import ThreadPoolExecutor

from lead_app import account_profiles, domain, reddit_workspace
from lead_app.db import all_rows, one, tx, uid
from lead_app.scope import current_tenant, tenant_scope


def test_device_owned_agent_delivers_independent_scoped_reddit_review(monkeypatch):
    aid = domain.create_agent()
    monkeypatch.setenv("LEAD_SPLIT_REDDIT_LOGIN_AGENT_IDS", aid)
    tenant = current_tenant()
    profile = account_profiles.for_platform(
        aid, "reddit", create=True, isolated_login=True
    )
    with tx(aid) as c:
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,))
        c.execute(
            """INSERT INTO lead.business_profiles(tenant_id,agent_id,revision,description)
            VALUES(%s,%s,1,'A first party test product')""",
            (tenant, aid),
        )
        acct = one(
            c,
            """UPDATE lead.social_accounts SET state='connected',external_account_id=%s,
            automation_enabled=true WHERE id=%s RETURNING id""",
            (uid(), profile["account_id"]),
        )

    def collect():
        with tenant_scope(tenant, None):
            reddit_workspace.review_enabled(tenant)

    # A device holds this lock while reviewing Telegram. Reddit's internal
    # draft must finish before that transaction releases the project row.
    with tx(aid) as held, ThreadPoolExecutor(1) as pool:
        held.execute("SELECT id FROM lead.agents WHERE id=%s FOR NO KEY UPDATE", (aid,))
        pool.submit(collect).result(timeout=8)

    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda _: collect(), range(2)))
    with tx(aid) as c:
        rows = all_rows(
            c, "SELECT * FROM lead.tasks WHERE type='reddit.workspace_review'"
        )
        assert len(rows) == 1
        assert rows[0]["input"]["account_id"] == str(acct["id"])
        assert (
            rows[0]["result"]["editorial_brief"]["source_excerpt"]
            == "A first party test product"
        )
        draft = rows[0]["result"]["editorial_brief"]
        assert "我是" not in draft["body"] and "产品方" not in draft["body"]
        assert draft["source_excerpt"] not in draft["body"]
        assert rows[0]["result"]["confirmed_posts"] == rows[0]["result"]["clicks"] == 0
        c.execute(
            "UPDATE lead.social_accounts SET binding_version=binding_version+1,automation_enabled=false WHERE id=%s",
            (acct["id"],),
        )
    collect()
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.tasks WHERE type='reddit.workspace_review'",
            )["n"]
            == 1
        )
        c.execute(
            "UPDATE lead.social_accounts SET automation_enabled=true WHERE id=%s",
            (acct["id"],),
        )
    collect()
    with tx(aid) as c:
        assert (
            one(
                c,
                "SELECT count(*) AS n FROM lead.tasks WHERE type='reddit.workspace_review'",
            )["n"]
            == 2
        )

    with tx(aid) as c:
        c.execute("""UPDATE lead.agents SET onboarding_state=jsonb_set(onboarding_state,'{public_voice}',
            '"independent_promoter"'::jsonb) WHERE id=%s""", (aid,))
    collect()
    acct_view = next(a for a in domain.agent_detail(aid)["accounts"] if a["id"] == acct["id"])
    assert "协助" in acct_view["editorial_review"]["editorial_brief"]["disclosure"]
