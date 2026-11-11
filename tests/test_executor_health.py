"""A read retry must not reopen a channel's write gate."""
import psycopg
import pytest
from lead_app.community_health import eligible_sql
from lead_app.config import database_kwargs


def test_elapsed_read_cooldown_keeps_writes_blocked_and_identity_bound():
    with psycopg.connect(**database_kwargs(owner=True)) as conn:
        conn.execute('CREATE TEMP TABLE social_accounts(id text, agent_id text, external_account_id text, platform text, state text, automation_enabled boolean)')
        conn.execute('CREATE TEMP TABLE tasks(agent_id text,input jsonb,result jsonb,state text)')
        conn.execute('CREATE TEMP TABLE memory_entries(agent_id text,body jsonb)')
        conn.execute("INSERT INTO social_accounts VALUES('slot','agent','original','telegram','connected',true)")
        conn.execute('''INSERT INTO tasks VALUES('agent','{"account_id":"slot","external_account_id":"original","candidate":{"telegram_url":"https://t.me/fixture"}}','{"target_id":"-1"}','planned')''')
        conn.execute('''INSERT INTO memory_entries VALUES('agent',jsonb_build_object('type','community_target_health','account','original','peer','-1','state','blocked','read_after',now()-interval '1 hour'))''')
        def eligible(**kwargs):
            predicate = eligible_sql(**kwargs).replace('lead.', 'pg_temp.')
            return conn.execute('SELECT count(*) FROM tasks t WHERE '+predicate).fetchone()[0]
        assert eligible() == 1
        assert eligible(write=True) == 0
        conn.execute("UPDATE social_accounts SET external_account_id='replacement'")
        assert eligible() == eligible(write=True) == 0


def test_legacy_receipt_permission_never_authorizes_a_write():
    with pytest.raises(ValueError, match='receipt'):
        eligible_sql(legacy_receipts=True, write=True)
