"""Exercise maintenance against disposable tables, including retry and owner changes."""
from uuid import UUID

import psycopg
import pytest
from lead_app.config import database_kwargs
from psycopg.types.json import Jsonb

from scripts import home_live_maintenance as site

SHA = "f" * 40
RUNNING = UUID("12345678-1234-1234-1234-000000000001")
PAUSED = UUID("12345678-1234-1234-1234-000000000002")


@pytest.fixture
def maintenance_tables(monkeypatch):
    with psycopg.connect(**database_kwargs(owner=True), autocommit=True) as c:
        c.execute("""CREATE TEMP TABLE agents(id uuid PRIMARY KEY,desired_state text,onboarding_state jsonb,
            control_version int,config_revision int,authorization_version int)""")
        for name in ("tasks", "actions", "browser_runs", "chat_jobs", "takeover_sessions",
                     "social_accounts", "account_targets", "authorization_grants", "interaction_cards"):
            c.execute(psycopg.sql.SQL("""CREATE TEMP TABLE {} (id uuid,agent_id uuid,state text,status text,
                type text,input jsonb,result jsonb,payload jsonb,expires_at timestamptz,external_account_id text,automation_enabled bool)"""
                                      ).format(psycopg.sql.Identifier(name)))

        def sql(query):
            try:
                return c.execute(query.replace("lead.", "pg_temp."))
            except psycopg.Error:
                c.execute("ROLLBACK")
                raise

        monkeypatch.setattr(site, "site_sql", sql)
        c.execute("INSERT INTO agents VALUES(%s,'running','{}',7,3,2),(%s,'paused','{}',8,3,2)",
                  (RUNNING, PAUSED))
        yield c


def test_pause_and_restore_without_redelegating(maintenance_tables):
    c = maintenance_tables
    c.execute("INSERT INTO tasks(state) VALUES('running'),('planned')")
    with pytest.raises(psycopg.errors.RaiseException, match="waiting for active work"):
        site.maintenance("hold", SHA)
    c.execute("UPDATE tasks SET state='succeeded' WHERE state='running'")
    site.maintenance("hold", SHA)
    site.maintenance("hold", SHA)
    assert c.execute("SELECT desired_state FROM agents WHERE id=%s", (RUNNING,)).fetchone() == ("paused",)
    c.execute("UPDATE agents SET onboarding_state=onboarding_state #- '{home_release_maintenance,previous_desired_state}' "
              "WHERE id=%s", (RUNNING,))
    c.execute("UPDATE agents SET control_version=9 WHERE id=%s", (RUNNING,))
    with pytest.raises(psycopg.errors.RaiseException, match="state changed"):
        site.maintenance("restore", SHA)
    c.execute("UPDATE agents SET control_version=7 WHERE id=%s", (RUNNING,))
    site.maintenance("restore", SHA)
    site.maintenance("restore", SHA)
    assert c.execute("SELECT desired_state,control_version,onboarding_state FROM agents ORDER BY id").fetchall() == [
        ("running", 7, {}), ("paused", 8, {})]
    assert c.execute("SELECT count(*) FROM tasks WHERE state='planned'").fetchone() == (1,)


def test_hold_paused_and_restore_preserve_owner_pause(maintenance_tables, monkeypatch):
    c = maintenance_tables
    metadata = {"owner_marker": {"source": "fixture"}, "home_discovery_drain": {"sha": "d" * 40}}
    c.execute("UPDATE agents SET onboarding_state=%s WHERE id=%s", (Jsonb(metadata), PAUSED))
    before = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
    monkeypatch.setattr(site.os, "geteuid", lambda: 0)

    site.main(["hold-paused", SHA, str(PAUSED)])
    held = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
    recorded = held[1][2][site.KEY]
    assert recorded["sha"] == SHA
    assert recorded["previous_desired_state"] == "paused"
    assert held == [before[0], (PAUSED, "paused", {**metadata, site.KEY: recorded}, 8, 3, 2)]

    site.maintenance("hold-paused", SHA, str(PAUSED))
    assert c.execute("SELECT * FROM agents ORDER BY id").fetchall() == held
    site.maintenance("restore", SHA)
    site.maintenance("restore", SHA)
    assert c.execute("SELECT * FROM agents ORDER BY id").fetchall() == before


def test_paused_hold_refuses_competing_release_and_owner_resume(maintenance_tables):
    c = maintenance_tables
    before = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
    with pytest.raises(psycopg.errors.RaiseException, match="paused executor agent changed"):
        site.maintenance("hold-paused", SHA, str(RUNNING))
    assert c.execute("SELECT * FROM agents ORDER BY id").fetchall() == before

    site.maintenance("hold-paused", SHA, str(PAUSED))
    held = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
    with pytest.raises(psycopg.errors.RaiseException, match="other maintenance"):
        site.maintenance("hold-paused", "d" * 40, str(PAUSED))
    assert c.execute("SELECT * FROM agents ORDER BY id").fetchall() == held

    c.execute("UPDATE agents SET desired_state='running' WHERE id=%s", (PAUSED,))
    resumed = c.execute("SELECT * FROM agents ORDER BY id").fetchall()
    with pytest.raises(psycopg.errors.RaiseException, match="state changed"):
        site.maintenance("restore", SHA)
    assert c.execute("SELECT * FROM agents ORDER BY id").fetchall() == resumed


def test_hold_paused_rejects_invalid_agent_before_site_sql(monkeypatch):
    monkeypatch.setattr(site, "site_sql", lambda query: pytest.fail("invalid agent reached SQL"))
    with pytest.raises(ValueError):
        site.maintenance("hold-paused", SHA, "g" * 36)
