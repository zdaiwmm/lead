"""Exercise maintenance against disposable tables, including retry and owner changes."""
import psycopg
import pytest
from lead_app.config import database_kwargs

from scripts import home_live_maintenance as site


def test_pause_and_restore_without_redelegating(monkeypatch):
    sha = "f" * 40
    with psycopg.connect(**database_kwargs(owner=True), autocommit=True) as c:
        c.execute("""CREATE TEMP TABLE agents(id int,desired_state text,onboarding_state jsonb,
            control_version int,config_revision int,authorization_version int)""")
        for name in ("tasks", "actions", "browser_runs", "chat_jobs", "takeover_sessions",
                     "social_accounts", "account_targets", "authorization_grants", "interaction_cards"):
            c.execute(psycopg.sql.SQL("""CREATE TEMP TABLE {} (id int,agent_id int,state text,status text,
                type text,payload jsonb,expires_at timestamptz,external_account_id text,automation_enabled bool)"""
                                      ).format(psycopg.sql.Identifier(name)))
        def sql(query):
            try:
                return c.execute(query.replace("lead.", "pg_temp."))
            except psycopg.Error:
                c.execute("ROLLBACK")
                raise
        monkeypatch.setattr(site, "site_sql", sql)
        c.execute("INSERT INTO agents VALUES(1,'running','{}',7,3,2),(2,'paused','{}',8,3,2)")
        c.execute("INSERT INTO tasks(state) VALUES('running'),('planned')")
        with pytest.raises(psycopg.errors.RaiseException, match="waiting for active work"):
            site.maintenance("hold", sha)
        c.execute("UPDATE tasks SET state='succeeded' WHERE state='running'")
        site.maintenance("hold", sha)
        site.maintenance("hold", sha)
        assert c.execute("SELECT desired_state FROM agents WHERE id=1").fetchone()[0] == "paused"
        c.execute("UPDATE agents SET control_version=9 WHERE id=1")
        with pytest.raises(psycopg.errors.RaiseException, match="state changed"):
            site.maintenance("restore", sha)
        c.execute("UPDATE agents SET control_version=7 WHERE id=1")
        site.maintenance("restore", sha)
        site.maintenance("restore", sha)
        assert c.execute("SELECT desired_state,control_version,onboarding_state FROM agents ORDER BY id").fetchall() == [
            ("running", 7, {}), ("paused", 8, {})]
        assert c.execute("SELECT count(*) FROM tasks WHERE state='planned'").fetchone() == (1,)
