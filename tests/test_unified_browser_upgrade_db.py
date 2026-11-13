"""Real PostgreSQL preservation of the old schema's controls and task facts."""
import importlib
import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from lead_app.config import TENANT, database_kwargs
from lead_app.migrations import upgrade
from psycopg import sql


@pytest.fixture
def upgrade_database(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    helper=importlib.import_module('unified_browser_upgrade')
    name='lead_unified_upgrade_'+uuid4().hex
    owner=database_kwargs(True)
    with psycopg.connect(**owner,autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(name)))
    target={**owner,'dbname':name}
    try:
        yield helper,target
    finally:
        with psycopg.connect(**owner,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def test_additive_upgrade_keeps_original_columns_and_detects_control_mutation(upgrade_database):
    helper,target=upgrade_database
    upgrade(target,target='0031_growth_runtime')
    aid=uuid4()
    with psycopg.connect(**target) as c:
        c.execute("INSERT INTO lead.agents(id,tenant_id,name,desired_state,control_version) VALUES(%s,%s,'fixture old app','paused',7)",(aid,TENANT))
        c.execute("""INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version)
          VALUES(%s,%s,1,'fixture','fixture','fixture','fixture','fixture')""",(TENANT,aid))
        c.execute("""INSERT INTO lead.tasks(tenant_id,agent_id,type,state,workflow_id,input,result,scheduled_at)
          VALUES(%s,%s,'growth.observe','reconciling',%s,'{}','{"original_action_state":"unknown"}',now())""",(TENANT,aid,'fixture:'+str(aid)))
    def connect(name):
        assert name==target['dbname']
        return psycopg.connect(**target)
    columns=helper.column_inventory(connect,target['dbname'])
    before=helper.data_inventory(connect,target['dbname'],columns)
    upgrade(target)
    after=helper.data_inventory(connect,target['dbname'],columns)
    helper.assert_unchanged(before,after)
    upgrade(target)
    helper.assert_unchanged(before,helper.data_inventory(connect,target['dbname'],columns))
    with psycopg.connect(**target) as c:
        assert c.execute('SELECT desired_state,control_version FROM lead.agents WHERE id=%s',(aid,)).fetchone()==('paused',7)
        assert c.execute("SELECT result->>'original_action_state' FROM lead.tasks WHERE agent_id=%s",(aid,)).fetchone()[0]=='unknown'
        c.execute('UPDATE lead.agents SET control_version=control_version+1 WHERE id=%s',(aid,))
    with pytest.raises(ValueError,match='Original accounts'):
        helper.assert_unchanged(before,helper.data_inventory(connect,target['dbname'],columns))


def test_release_schema_readback_checks_actual_unified_permissions_and_guards(upgrade_database):
    _, target = upgrade_database
    release = importlib.import_module('home_systemd_release')
    upgrade(target)
    query = release.unified_schema_query()
    mutations = (
        "ALTER TABLE lead.experiences DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE lead.takeover_sessions DISABLE TRIGGER connection_login_lifetime",
        "ALTER TABLE lead.agents DISABLE TRIGGER expire_login_control",
        "DROP INDEX lead.browser_request_original_action",
        "DROP INDEX lead.one_live_takeover",
        "DROP INDEX lead.one_account_controller",
        "ALTER TABLE lead.takeover_sessions DROP CONSTRAINT connection_scoped_account",
        "ALTER TABLE lead.takeover_sessions DROP CONSTRAINT takeover_sessions_automatic_attempts_check; "
        "ALTER TABLE lead.takeover_sessions ADD CONSTRAINT takeover_sessions_automatic_attempts_check CHECK (automatic_attempts>=0)",
        "ALTER TABLE lead.social_accounts DROP CONSTRAINT web_connection_origin_required",
        "GRANT EXECUTE ON FUNCTION lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb) TO lead_runtime",
        "REVOKE EXECUTE ON FUNCTION lead.growth_complete_connection(uuid,uuid,uuid,bigint,bigint,bigint,jsonb,text,jsonb) FROM lead_auth",
        "CREATE OR REPLACE FUNCTION lead.clear_connection_login() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RETURN NEW; END$$",
    )
    with psycopg.connect(**target) as c:
        assert c.execute(query).fetchone()[0] is True
        for statement in mutations:
            c.execute('SAVEPOINT changed_guard')
            c.execute(statement)
            assert c.execute(query).fetchone()[0] is False, statement
            c.execute('ROLLBACK TO SAVEPOINT changed_guard')
            c.execute('RELEASE SAVEPOINT changed_guard')
            assert c.execute(query).fetchone()[0] is True


@pytest.mark.parametrize('name', ['growth_complete_connection','growth_receipt_valid','growth_confirm_publication'])
def test_unified_release_refuses_legacy_functions_even_with_additive_tables(upgrade_database, name):
    _, target = upgrade_database
    release = importlib.import_module('home_systemd_release')
    upgrade(target)
    legacy = (Path(__file__).resolve().parents[1]/'infra/migrations/sql/0031_growth_runtime.sql').read_text()
    with psycopg.connect(**target) as c:
        statement = re.search(r'CREATE FUNCTION lead\.'+name+r'\(.*?\$\$;', legacy, re.S)[0]
        c.execute(statement.replace('CREATE FUNCTION','CREATE OR REPLACE FUNCTION',1))
        # The old app still recognizes its own schema after an additive
        # migration. A native-browser cutover needs the newer receipt contract.
        assert c.execute(release.growth_schema_query()).fetchone()[0] is True
        assert c.execute(release.unified_schema_query()).fetchone()[0] is False


def test_codex_upgrade_preserves_existing_unified_business_evidence(upgrade_database):
    helper, target = upgrade_database
    upgrade(target, target='0033_unified_browser')
    aid = uuid4()
    with psycopg.connect(**target) as c:
        c.execute("INSERT INTO lead.agents(id,tenant_id,name,desired_state,control_version) VALUES(%s,%s,'existing unified app','paused',9)", (aid,TENANT))
        c.execute("""INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,prompt_version,toolset_version,output_schema_version)
            VALUES(%s,%s,1,'fixture','fixture','fixture','fixture','fixture')""", (TENANT,aid))
        c.execute("""INSERT INTO lead.experiences(tenant_id,agent_id,extraction_key,kind,problem,conclusion)
            VALUES(%s,%s,'original','research','existing evidence','preserve the original conclusion')""", (TENANT,aid))
        c.execute("""INSERT INTO lead.research_allowances(tenant_id,connection_ref,period_key,unit,limit_units,reserved_units,spent_units)
            VALUES(%s,'original','period','provider',100,40,30)""", (TENANT,))
    def connect(name):
        assert name == target['dbname']
        return psycopg.connect(**target)
    columns = helper.column_inventory(connect, target['dbname'])
    assert {'experiences','research_allowances'} <= set(columns)
    before = helper.data_inventory(connect, target['dbname'], columns)
    upgrade(target)
    helper.assert_unchanged(before, helper.data_inventory(connect, target['dbname'], columns))
    # An application rollback must also retain newly created transport facts.
    columns = helper.column_inventory(connect, target['dbname'])
    assert {'local_executor_requests','local_executor_events'} <= set(columns)


def test_transport_schema_rejects_weakened_keys_policies_and_privileges(upgrade_database):
    _, target = upgrade_database
    release = importlib.import_module('home_systemd_release')
    upgrade(target)
    query = release.local_executor_schema_query()
    mutations = (
        "ALTER TABLE lead.local_executor_devices DISABLE ROW LEVEL SECURITY",
        "ALTER TABLE lead.local_executor_commands NO FORCE ROW LEVEL SECURITY",
        "ALTER POLICY agent_scope ON lead.local_executor_devices USING(true) WITH CHECK(true)",
        "CREATE POLICY unexpected_allow ON lead.local_executor_devices USING(true)",
        "ALTER TABLE lead.local_executor_requests DROP CONSTRAINT local_executor_requests_tenant_id_agent_id_device_id_fkey",
        "ALTER TABLE lead.local_executor_requests DROP CONSTRAINT local_executor_requests_tenant_id_agent_id_request_key_key",
        "ALTER TABLE lead.local_executor_commands DROP CONSTRAINT local_executor_commands_kind_check",
        "ALTER TABLE lead.local_executor_requests DISABLE TRIGGER ALL",
        "REVOKE INSERT ON lead.local_executor_devices FROM lead_runtime",
        "GRANT DELETE ON lead.local_executor_events TO lead_runtime",
        "UPDATE public.lead_migration_version SET version_num='0033_unified_browser'",
    )
    with psycopg.connect(**target) as c:
        assert c.execute(query).fetchone()[0] is True
        assert c.execute(release.unified_schema_query()).fetchone()[0] is True
        for statement in mutations:
            c.execute('SAVEPOINT transport_guard')
            c.execute(statement)
            assert c.execute(query).fetchone()[0] is False, statement
            c.execute('ROLLBACK TO SAVEPOINT transport_guard')
            c.execute('RELEASE SAVEPOINT transport_guard')
            assert c.execute(query).fetchone()[0] is True
