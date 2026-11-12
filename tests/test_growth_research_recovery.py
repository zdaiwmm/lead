from datetime import timedelta

from lead_app import config, model
from lead_app.db import one, tx
from lead_app.growth import tools
from lead_app.growth.contracts import ExperimentSpec, TaskSpec, WorkResult, utcnow
from lead_app.growth.runtime import advance, execute, finish, probe_episode
from lead_app.growth.store import create_experiment, submit_work
from lead_app.growth.tools import RecoverInput
from pydantic_ai.exceptions import ModelHTTPError
from test_growth_runtime import new_work


async def test_research_assessment_provider_recovers_original_research(growth_agent,monkeypatch):
    new_work(growth_agent)
    monkeypatch.setattr(config,'any_model_configured',lambda:True)
    async def fetch(url,**kwargs):
        return {'url':url,'title':'Original request','text':'Independent makers need a way to compare quote line items without retyping them.','links':[]}
    async def failed(*args,**kwargs):
        raise ModelHTTPError(503,'synthetic-model',{'message':'Synthetic provider outage'})
    monkeypatch.setattr(tools,'fetch_product',fetch)
    monkeypatch.setattr(tools,'run_model',failed)
    with tx(growth_agent) as c:
        exp=create_experiment(c,growth_agent,ExperimentSpec(question_type='audience',question='Who needs quote comparison?',
            hypothesis='Makers need it',learning_signal='An original relevant request',token_limit=100000,review_at=utcnow()+timedelta(hours=1)))
        ref=submit_work(c,growth_agent,TaskSpec(kind='research',purpose='demand',experiment_id=exp['id'],
            payload={'question':'Who needs quote comparison?','urls':['https://forum.example/original']},review_at=utcnow()+timedelta(hours=1)))
    for n in range(3):
        claim=advance(ref,now=utcnow()+timedelta(minutes=n))['claim']
        result=WorkResult.model_validate(await execute(claim))
        assert result.failure_code=='ModelHTTPError'
        finish(claim,result)
    with tx(growth_agent) as c:
        root=one(c,'SELECT * FROM lead.tasks WHERE id=%s',(ref['task_id'],))
        episode=root['result']['runtime']['recovery']
        assert episode['resource'].startswith('model:') and episode['status']=='blocked'
    async def healthy(output,*args,**kwargs):
        return output(ready=True)
    monkeypatch.setattr(model,'run_model',healthy)
    result=await probe_episode(growth_agent,root,RecoverInput(episode_root=ref['task_id'],condition_revision=episode['condition_revision']))
    assert result.output['state']=='recovered'
    assert result.output['resumed']['task_id']==ref['task_id']
    assert advance(result.output['resumed'])['state']=='ready'
