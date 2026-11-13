from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from lead_app.account_intents import set_intent
from lead_app.db import one, tx, uid
from lead_app.execution_routes import register
from lead_app.growth.contracts import TaskSpec, Wait, WorkResult, utcnow
from lead_app.growth.runtime import advance, finish
from lead_app.growth.store import submit_work


def profile_tasks(aid, provider='chromium', count=3):
    with tx(aid) as c:
        profile=one(c,"UPDATE lead.browser_profiles SET provider=%s WHERE slot='default' RETURNING id",(provider,))
        c.execute("UPDATE lead.agents SET desired_state='running' WHERE id=%s",(aid,))
        accounts=[one(c,"""INSERT INTO lead.social_accounts(tenant_id,agent_id,profile_id,platform,state,
            external_account_id,connection_origin) VALUES(lead.ctx_tenant(),%s,%s,'web','connected',%s,%s) RETURNING id""",
            (aid,profile['id'],uid(),'https://site-'+str(index)+'.example'))['id'] for index in range(count)]
    register(aid,profile['id'])
    for account in accounts:
        set_intent(aid,account,'running',source='owner_command',source_ref='admission:'+str(account))
    return profile['id'],accounts,[discover(aid,account) for account in accounts]


def discover(aid,account):
    with tx(aid) as c:
        return submit_work(c,aid,TaskSpec(kind='account',purpose='discover',account_id=account,
            payload={},dedupe_key=uid(),review_at=utcnow()+timedelta(hours=1)))


def test_native_two_accounts_run_and_third_waits_until_a_real_claim_yields(growth_agent):
    _,accounts,refs=profile_tasks(growth_agent)
    first,second=advance(refs[0]),advance(refs[1])
    assert first['state']==second['state']=='ready'
    third=advance(refs[2])
    assert third['state']=='wait' and third['wait']['reason']=='resource_wait'
    same=advance(discover(growth_agent,accounts[0]))
    assert same['state']=='wait'
    result=finish(first['claim'],WorkResult(outcome='waiting',wait=Wait(reason='owner_input',
        producer='connection_control',recovery_action='owner',episode_root=refs[0]['task_id'],check_at=utcnow()+timedelta(minutes=1))))
    assert result['accepted_control']
    assert advance(refs[2],now=utcnow()+timedelta(seconds=2))['state']=='ready'


def test_native_admission_is_atomic_across_competing_claims(growth_agent):
    _,_,refs=profile_tasks(growth_agent,count=4)
    with ThreadPoolExecutor(max_workers=4) as workers:
        results=list(workers.map(advance,refs))
    assert sum(result['state']=='ready' for result in results)==2
    with tx(growth_agent) as c:
        assert one(c,"SELECT count(*) n FROM lead.tasks WHERE state='running'")['n']==2


def test_native_same_account_keeps_exclusive_claim_with_another_slot_free(growth_agent):
    _,accounts,refs=profile_tasks(growth_agent)
    assert advance(refs[0])['state']=='ready'
    assert advance(discover(growth_agent,accounts[0]))['state']=='wait'
    assert advance(refs[1])['state']=='ready'


def test_legacy_shared_profile_remains_exclusive(growth_agent):
    _,_,refs=profile_tasks(growth_agent,provider='camoufox')
    assert advance(refs[0])['state']=='ready'
    assert advance(refs[1])['state']=='wait'


def test_owner_connection_blocks_its_account_but_releases_other_native_account(growth_agent):
    from test_growth_account_connection import login_work
    _,accounts,refs=profile_tasks(growth_agent)
    login_work(growth_agent,accounts[0])
    assert advance(refs[0])['state']=='wait'
    assert advance(refs[1])['state']=='ready'


def test_native_failure_resource_is_account_scoped(growth_agent):
    _,_,refs=profile_tasks(growth_agent)
    first=advance(refs[0])
    second=advance(refs[1])
    assert first['state']==second['state']=='ready'
    with tx(growth_agent) as c:
        resources=c.execute("SELECT result#>>'{runtime,resource_key}' FROM lead.tasks WHERE state='running'").fetchall()
    assert len({next(iter(row.values())) for row in resources})==2
