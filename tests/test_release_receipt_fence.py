"""Check receipt classification independently of other tenants' active work."""

import psycopg
from lead_app.config import database_kwargs

from scripts.home_release_fence import BUSY, TABLES


def test_release_fence_distinguishes_durable_receipts_from_inflight_operations():
    with psycopg.connect(**database_kwargs(owner=True)) as c:
        for table in TABLES:
            c.execute(psycopg.sql.SQL("""CREATE TEMP TABLE {} (
                state text, desired_state text, status text, expires_at timestamptz,
                type text, input jsonb, result jsonb)""").format(psycopg.sql.Identifier(table)))
        probe = BUSY.replace("lead.", "pg_temp.")
        c.execute("""INSERT INTO tasks(state,type,input,result) VALUES
            ('reconciling','community.engage','{"account_id":"old","external_account_id":"native"}',
             '{"reply_state":"unknown"}')""")
        assert c.execute(probe).fetchone() == (False,)
        c.execute("UPDATE tasks SET state='running'")
        assert c.execute(probe).fetchone() == (True,)
        c.execute("UPDATE tasks SET state='reconciling',result=NULL")
        assert c.execute(probe).fetchone() == (True,)
        c.execute("UPDATE tasks SET state='canceled'")
        c.execute("INSERT INTO actions(state) VALUES ('dispatching')")
        assert c.execute(probe).fetchone() == (True,)
        c.execute("DELETE FROM actions")
        c.execute("INSERT INTO profile_leases(state) VALUES ('held')")
        assert c.execute(probe).fetchone() == (True,)
