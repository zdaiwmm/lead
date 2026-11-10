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
