"""Case selection and counterevidence plumbing; every model is an explicit fake."""
import importlib.util
import json
from pathlib import Path

import pytest
from lead_app.growth.contracts import DecisionProposal, ExperimentSpec, TaskSpec, utcnow


@pytest.fixture
def evaluation(monkeypatch):
    scripts=Path(__file__).parents[1]/'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec=importlib.util.spec_from_file_location('counterevidence_evaluation',scripts/'evaluate_growth_model.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_selection_keeps_original_nine_cases_three_times(evaluation):
    selected=evaluation.selected_cases()
    assert len(selected)==27
    assert {(product,branch) for product,branch,_ in selected}==set(evaluation.CASES)
    assert not any(branch=='memory_counterevidence' for _,branch,_ in selected)
    assert evaluation.selected_cases('memory_counterevidence')==[('quote','memory_counterevidence',1)]
    assert evaluation.selected_cases('quote:demand',1)==[('quote','demand',1)]


@pytest.mark.parametrize('case,repetitions',[('memory_counterevidence',2),('all',0),('all',4),('unknown',1)])
def test_invalid_selection_stops_before_model_work(evaluation,case,repetitions):
    with pytest.raises(ValueError):
        evaluation.selected_cases(case,repetitions)


@pytest.mark.parametrize('test_flag,database',[(None,'lead_ci'),('1','lead')])
def test_memory_fixture_rejects_missing_opt_in_or_production_database(evaluation,monkeypatch,test_flag,database):
    from lead_app import config
    from lead_app.growth import tools

    if test_flag is None:
        monkeypatch.delenv('LEAD_GROWTH_TEST_ENV',raising=False)
    else:
        monkeypatch.setenv('LEAD_GROWTH_TEST_ENV',test_flag)
    monkeypatch.setenv('LEAD_DB_NAME',database)
    assert config.database_kwargs(owner=True)['dbname']==database

    def no_fixture_write(*args,**kwargs):
        pytest.fail('A rejected evaluation must not write synthetic fixture data')

    monkeypatch.setattr(tools,'_remember',no_fixture_write)
    with pytest.raises(ValueError,match='requires an isolated verification database'):
        evaluation.seed_memory_counterevidence('00000000-0000-0000-0000-000000000000','quote')


async def test_memory_case_retrieves_old_experience_and_records_real_context_and_receipts(evaluation,tmp_path,monkeypatch):
    from datetime import timedelta

    from lead_app import model
    calls=[]
    async def no_real_model(*args,**kwargs):
        pytest.fail('No real model adapter may be used by this test')
    monkeypatch.setattr(model,'run_model',no_real_model)
    async def fake(schema,context,**kwargs):
        calls.append((context,kwargs))
        assert len(context['experiences'])==1
        experience=context['experiences'][0]
        assert experience['problem']==context['goal']['objective']
        assert experience['conditions']=={'product':'https://quote.example/'}
        assert 'every day' in experience['conclusion']
        assert '7 total impressions' in str(context['documents'])
        assert context['outcomes']['new_units'] is None
        assert any(capability.get('platform')=='facebook' for capability in context['capabilities'])
        assert any(target.get('formats')==['text'] and target.get('operations')==['publish']
                   and target.get('role')=='owner' for target in context['targets'])
        assert any(capability.get('operations')==['publish'] for capability in context['capabilities'])
        assert all(key not in context for key in ('rubric','rubric_question_type_matches','review_expectations'))
        experiment=ExperimentSpec(question_type='distribution',question='Which public venue reaches makers?',
            hypothesis='Independent maker venues may expose concrete needs',learning_signal='A relevant original request',
            token_limit=1000,review_at=utcnow()+timedelta(hours=1))
        research=TaskSpec(kind='research',purpose='Inspect a reachable alternative',
            payload={'question':'Which public maker venues have recent relevant requests?','queries':['quote comparison maker workflow']},
            review_at=utcnow()+timedelta(hours=1))
        return DecisionProposal(context_key=context['context_key'],rationale='Current source evidence motivates an independent distribution investigation.',commands=[
            {'command_key':'experiment','group_key':'alternative','kind':'experiment.create','goal_ref':{'revision':context['goal']['revision']},'payload':experiment.model_dump(mode='json')},
            {'command_key':'research','group_key':'alternative','kind':'task.create','goal_ref':{'revision':context['goal']['revision']},'payload':research.model_dump(mode='json')}])
    output=tmp_path/'reports/result.json'
    await evaluation.evaluate(tmp_path,output,160000,case='memory_counterevidence',repetitions=1,model_runner=fake)
    result=json.loads(output.read_text())
    assert len(calls)==1 and calls[0][1]['call_key']=='primary'
    assert result['model_mode']=='test_double'
    assert len(result['results'])==1 and result['admitted_upper_bound']==80000
    record=result['results'][0]
    assert record['case']=='memory_counterevidence' and record['memory_retrieval_verified']
    assert record['input_hashes']==[evaluation.input_hash(calls[0][0])]
    assert record['model_inputs'][0]==calls[0][0]
    assert len(record['proposals'])==1 and len(record['result']['accepted'])==1
    assert len(record['result']['accepted'][0]['task_refs'])==1
    assert [item['type'] for item in record['work']]==['growth.research']
    assert record['usage']==[]
    assert record['semantic_review']=='pending_raw_review'
    assert 'rubric_question_type_matches' not in record
    assert output.stat().st_mode & 0o077==0


async def test_memory_case_admits_only_primary_and_existing_repair(evaluation,tmp_path):
    calls=[]
    async def fake(schema,context,**kwargs):
        calls.append(kwargs['call_key'])
        key='wrong-context' if len(calls)==1 else context['context_key']
        return DecisionProposal(context_key=key,rationale='A bounded synthetic proposal for mechanical validation.',commands=[])
    output=tmp_path/'result.json'
    await evaluation.evaluate(tmp_path,output,160000,case='memory_counterevidence',model_runner=fake)
    record=json.loads(output.read_text())['results'][0]
    assert calls==['primary','repair']
    assert len(record['input_hashes'])==len(record['proposals'])==2
    assert record['semantic_review']=='pending_raw_review'


async def test_budget_stops_repair_without_an_additional_request(evaluation,tmp_path):
    calls=[]
    async def fake(schema,context,**kwargs):
        calls.append(kwargs['call_key'])
        return DecisionProposal(context_key='wrong-context',rationale='A deliberately rejected synthetic proposal.',commands=[])
    output=tmp_path/'result.json'
    with pytest.raises(SystemExit):
        await evaluation.evaluate(tmp_path,output,80000,case='memory_counterevidence',model_runner=fake)
    report=json.loads(output.read_text())
    assert calls==['primary'] and report['admitted_upper_bound']==80000
    assert report['results'][0]['budget_stop']=='evaluation_budget_exhausted'
    assert report['results'][0]['semantic_review']=='not_evaluated'


async def test_failed_fake_model_retains_unknown_cost_evidence(evaluation,tmp_path):
    from lead_app.budget import reserve
    from lead_app.db import uid
    calls=[]
    async def fake(schema,context,**kwargs):
        calls.append(kwargs['call_key'])
        reserve(kwargs['agent_id'],'synthetic-interrupted-model:'+uid(),1000)
        raise RuntimeError('Synthetic interruption before usage was returned')
    output=tmp_path/'result.json'
    with pytest.raises(SystemExit):
        await evaluation.evaluate(tmp_path,output,160000,case='memory_counterevidence',model_runner=fake)
    record=json.loads(output.read_text())['results'][0]
    assert calls==['primary']
    assert record['usage']==[]
    assert len(record['reservations'])==1 and record['reservations'][0]['state']=='reserved'
    assert record['reservations'][0]['units']==1000
    assert record['semantic_review']=='not_evaluated'
