from uuid import uuid4

import psycopg
from lead_app.config import TENANT
from lead_app.db import j
from test_migrations import isolated_database  # noqa: F401

from scripts.migrate import upgrade


def test_schema_upgrade_fences_legacy_without_changing_user_control(request):
    database = request.getfixturevalue("isolated_database")
    upgrade(database,target="0030_fault_isolation",roles=False)
    identifier = uuid4()
    with psycopg.connect(**database) as c:
        c.execute("INSERT INTO lead.tenants(id,name) VALUES(%s,'Synthetic upgrade')",(TENANT,))
        c.execute("""INSERT INTO lead.agents(id,tenant_id,name,desired_state,control_version,onboarding_state)
            VALUES(%s,%s,'Synthetic old project','paused',7,%s)""",(identifier,TENANT,j({"daily_token_limit":123456})))
        c.execute("""INSERT INTO lead.agent_revisions(tenant_id,agent_id,revision,provider,model_ref,
            prompt_version,toolset_version,output_schema_version) VALUES(%s,%s,1,'synthetic','test','test','test','1')""",(TENANT,identifier))
    upgrade(database)
    upgrade(database)
    with psycopg.connect(**database) as c:
        state,control,limits,engine = c.execute("""SELECT desired_state,control_version,
            onboarding_state->>'daily_token_limit',onboarding_state#>>'{growth_runtime,decision,engine}'
            FROM lead.agents WHERE id=%s""",(identifier,)).fetchone()
        assert (state,control,limits,engine)==("paused",7,"123456","migrating")
        assert c.execute("""SELECT count(*) FROM pg_class WHERE relnamespace='lead'::regnamespace
            AND relname IN ('growth_experiments','account_operating_intents','profile_execution_routes')
            AND relrowsecurity AND relforcerowsecurity""").fetchone()[0]==3
        assert not c.execute("SELECT has_function_privilege('lead_runtime','lead.growth_set_intent(uuid,uuid,text,text,text,bigint,jsonb)','EXECUTE')").fetchone()[0]
        assert c.execute("SELECT has_function_privilege('lead_auth','lead.growth_set_intent(uuid,uuid,text,text,text,bigint,jsonb)','EXECUTE')").fetchone()[0]
        assert c.execute("SELECT has_function_privilege('lead_runtime','lead.growth_confirm_publication(uuid)','EXECUTE')").fetchone()[0]
