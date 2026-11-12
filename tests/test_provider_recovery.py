"""Provider recovery reuses actual paid-call facts without resending requests."""
from datetime import timedelta

import pytest
from lead_app import research_providers as providers
from lead_app.db import j, one, tx
from lead_app.growth import runtime, tools
from lead_app.growth.contracts import utcnow
from test_research_providers import claim, setup_config, task

SCOPE={'provider':'tavily','operation':'web.search','endpoint':providers.ENDPOINTS[('tavily','web.search')][1],
       'access_mode':'provider_public','resource':'original-resource','condition_revision':'original-condition'}


def root_task(aid):
    tid=task(aid)
    with tx(aid) as c:
        row=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(tid,))
        inputs=row['input']
        inputs['spec']['payload']={'provider_calls':[{'provider':'tavily','operation':'web.search','args':{'query':'community'}}]}
        state=row['result']
        state['runtime']['recovery']={'last_failure_at':utcnow().isoformat()}
        c.execute('UPDATE lead.tasks SET input=%s,result=%s WHERE id=%s',(j(inputs),j(state),tid))
        return one(c,'SELECT * FROM lead.tasks WHERE id=%s',(tid,))


@pytest.mark.parametrize('result_state,billing', [('complete','known'),('complete','unknown'),('unavailable','unknown')])
async def test_recovery_reads_later_success_without_new_call_or_charge(growth_agent,monkeypatch,result_state,billing):
    conf=setup_config(monkeypatch,10000)
    root=root_task(growth_agent)
    tid=task(growth_agent)
    reservation,_=providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'community'},'later-response',conf,claim(growth_agent,tid))
    providers._settle(growth_agent,reservation,conf,{'state':result_state,'billing_state':billing},1000 if billing=='known' else None)
    with tx(growth_agent) as c:
        before=one(c,'SELECT count(*) n FROM lead.budget_reservations')['n']
    result=await tools.probe_research_resource(growth_agent,root,SCOPE)
    assert result.outcome==('completed' if result_state=='complete' else 'failed')
    if result_state=='complete':
        assert result.output['recovery_checks'][0]=={
            'resource':'original-resource','condition_revision':'original-condition','state':'ready',
            'evidence_ref':'reservation:'+reservation}
    with tx(growth_agent) as c:
        assert one(c,'SELECT count(*) n FROM lead.budget_reservations')['n']==before
        assert one(c,'SELECT state FROM lead.budget_reservations WHERE id=%s',(reservation,))['state']==('settled' if billing=='known' else 'unknown')


async def test_old_or_previous_configuration_response_cannot_prove_recovery(growth_agent,monkeypatch):
    conf=setup_config(monkeypatch,10000)
    tid=task(growth_agent)
    reservation,_=providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'community'},'old-response',conf,claim(growth_agent,tid))
    providers._settle(growth_agent,reservation,conf,{'state':'complete','billing_state':'known'},1000)
    root=root_task(growth_agent)
    assert (await tools.probe_research_resource(growth_agent,root,SCOPE)).outcome=='failed'
    root['result']['runtime']['recovery']['last_failure_at']=(utcnow()-timedelta(minutes=1)).isoformat()
    monkeypatch.setenv('TAVILY_API_KEY','fixture-replaced-key')
    assert (await tools.probe_research_resource(growth_agent,root,SCOPE)).outcome=='failed'


def test_provider_resource_condition_changes_with_credential_but_not_spend(growth_agent,monkeypatch):
    conf=setup_config(monkeypatch,10000)
    with tx(growth_agent) as c:
        key,condition=runtime._current_research_resource(c,SCOPE)
    tid=task(growth_agent)
    providers._reserve(growth_agent,tid,'tavily','web.search',{'query':'community'},'condition-spend',conf,claim(growth_agent,tid))
    with tx(growth_agent) as c:
        assert runtime._current_research_resource(c,SCOPE)==(key,condition)
    monkeypatch.setenv('TAVILY_API_KEY','fixture-new-condition-key')
    with tx(growth_agent) as c:
        new_key,new_condition=runtime._current_research_resource(c,SCOPE)
    assert new_key==key and new_condition!=condition
