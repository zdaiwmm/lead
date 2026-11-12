"""Real PostgreSQL preservation of the old schema's controls and task facts."""
import importlib
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
