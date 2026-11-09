"""Use a private schema to exercise the node and discovery owner together."""

import psycopg
import pytest
from lead_app.config import database_kwargs
from lead_app.db import j
from lead_app.discovery_handoff import check_discovery_handoff
from psycopg.rows import dict_row

from scripts import home_live_maintenance as site


class TempTables:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, query, params=None):
        return self.conn.execute(query.replace("lead.", "pg_temp."), params)


@pytest.fixture
def tables(monkeypatch):
    with psycopg.connect(**database_kwargs(owner=True), autocommit=True, row_factory=dict_row) as db:
        c = TempTables(db)
        c.execute("""CREATE TEMP TABLE agents(id int,desired_state text,onboarding_state jsonb,
                        control_version int,config_revision int,authorization_version int)""")
        for name in ("tasks", "actions", "browser_runs", "chat_jobs", "takeover_sessions",
                     "social_accounts", "account_targets", "authorization_grants", "interaction_cards"):
            c.execute(f"""CREATE TEMP TABLE {name}(id int,agent_id int,type text,state text,status text,
                    external_account_id text,automation_enabled bool,input jsonb,result jsonb,payload jsonb,
                    completed_at timestamptz,expires_at timestamptz)""")

        def query(stmt):
            try:
                return c.execute(stmt)
            except psycopg.Error:
                db.execute("ROLLBACK")
                raise

        monkeypatch.setattr(site, "site_sql", query)
        c.execute("""INSERT INTO agents VALUES(1,'running',jsonb_build_object('telegram_executor',
            jsonb_build_object('native_search_after',now()+interval '30 min','discovery_handoff_version',1)),1,2,3)""")
        c.execute("INSERT INTO social_accounts(id,agent_id,state,external_account_id,automation_enabled) VALUES(3,1,'connected','acct',true)")
        c.execute("""INSERT INTO tasks(id,agent_id,type,state,input,result) VALUES(4,1,'community.discover',
            'running',%s,%s)""", (j({"account_id":"3","external_account_id":"acct","checkpoint_version":"2",
                                     "config_revision":2,"control_version":1}), j({"phase":"native_collect","native_cursor":7})))
        yield c


def idle_boundary(c):
    with c.conn.transaction():
        agent = c.execute("SELECT * FROM agents WHERE id=1 FOR NO KEY UPDATE").fetchone()
        return check_discovery_handoff(c, agent, "acct")


def test_hold_restore_duplicate_and_interrupted_drain(tables):
    c, sha = tables, "a"*40
    assert not idle_boundary(c)
    with pytest.raises(psycopg.errors.RaiseException, match="waiting for active work"):
        site.maintenance("hold", sha)
    for _ in range(2):
        site.request_discovery_drain(sha)
        assert idle_boundary(c)
    # Simulate the release process exiting before hold; recover from storage.
    c.execute("UPDATE agents SET onboarding_state=onboarding_state-'home_discovery_drain'")
    assert not idle_boundary(c)
    site.request_discovery_drain(sha)
    assert idle_boundary(c)
    site.maintenance("hold", sha)
    assert idle_boundary(c)
    site.maintenance("restore", sha)
    assert not idle_boundary(c)
    assert not idle_boundary(c)
    assert c.execute("SELECT id,state,result FROM tasks").fetchall() == [
        {"id":4,"state":"running","result":{"phase":"native_collect","native_cursor":7}}]


@pytest.mark.parametrize("barrier", ["unknown", "takeover", "scope"])
def test_blocked_and_changed_owner(tables, barrier):
    c, sha = tables, "a"*40
    site.request_discovery_drain(sha)
    if barrier == "scope":
        assert idle_boundary(c)
        c.execute("UPDATE agents SET control_version=2")
        with pytest.raises(psycopg.errors.RaiseException, match="checkpoint changed"):
            site.maintenance("hold", sha)
        assert idle_boundary(c)
        assert c.execute("SELECT state FROM tasks").fetchone()["state"] == "canceled"
    else:
        if barrier == "unknown":
            c.execute("""INSERT INTO tasks(agent_id,type,state,result)
                         VALUES(1,'community.engage','reconciling','{"reply_state":"unknown"}')""")
        else:
            c.execute("INSERT INTO takeover_sessions(agent_id,state,expires_at) VALUES(1,'active',now()+interval '2 minutes')")
        assert idle_boundary(c)
        assert c.execute("SELECT state FROM tasks WHERE id=4").fetchone()["state"] == "running"
        with pytest.raises(psycopg.errors.RaiseException, match="waiting for active work"):
            site.maintenance("hold", sha)


def test_release_process_stops_renewing_its_request(tables):
    c = tables
    site.request_discovery_drain("a"*40)
    assert idle_boundary(c)
    c.execute("""UPDATE agents SET onboarding_state=jsonb_set(onboarding_state,
        '{home_discovery_drain,until}',to_jsonb(now()-interval '1 second'))""")
    assert not idle_boundary(c)
    assert c.execute("SELECT id,state FROM tasks").fetchall() == [{"id":4,"state":"running"}]
