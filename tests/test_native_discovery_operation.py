"""Discovery outcome is an original task constraint, never a page-derived grant."""
import ast
import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pytest
from lead_app.browser_execution import origin
from lead_app.native_method_errors import MethodFactError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

ROOT=Path(__file__).resolve().parents[1]


def definition(path,name,*,namespace):
    tree=ast.parse((ROOT/path).read_text())
    node=next(n for n in ast.walk(tree) if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
    node.decorator_list=[]
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),str(path),'exec'),namespace)
    return namespace[name]


class Contract(BaseModel):
    model_config=ConfigDict(extra='forbid')


@pytest.mark.parametrize('path,name,parent',[
    ('services/api/lead_app/growth/tools.py','AccountInput','Contract'),
    ('services/api/lead_app/browser.py','DiscoverRequest','OpenRequest')])
def test_operation_is_optional_and_only_the_existing_two_operations(path,name,parent):
    model=definition(path,name,namespace={parent:Contract,'Field':Field,'Literal':Literal,'UUID':UUID})
    assert model().desired_operation is None
    for value in ('publish','reply'):
        assert model(desired_operation=value).desired_operation==value
    for value in ('post','Publish','delete',''):
        with pytest.raises(ValidationError):
            model(desired_operation=value)


def test_original_task_operation_is_visible_in_context_without_changing_url_hints():
    context=definition('services/api/lead_app/native_runner.py','discovery_task_context',
        namespace={'urlsplit':urlsplit,'urlunsplit':urlunsplit})
    task={'input':{'spec':{'payload':{'target_urls':['https://board.test/thread/17'],'desired_operation':'reply'}}}}
    value=context(task,{'connection_origin':'https://board.test'},{'scope':{}})
    assert value['desired_operation']=='reply'
    assert value['starting_urls']==['https://board.test/thread/17']
    assert value['account_origin']=='https://board.test'
    assert 'desired_operation' not in context({}, {'connection_origin':'https://board.test'}, {'scope':{}})


def record_method():
    async def inspect(*args,**kwargs):
        return {'operations':[kwargs['operation']],'platform_permission':{}}
    namespace={'asyncio':asyncio,'origin':origin,'MethodFactError':MethodFactError,
        'inspect_editor':AsyncMock(side_effect=inspect),'validate_method':lambda *args,**kwargs:None}
    function=definition('services/api/lead_app/native_task.py','record_method',namespace=namespace)
    return function,namespace['inspect_editor']


def controller(desired):
    task={'type':'growth.account','input':{'spec':{'purpose':'discover','payload':
        {} if desired is None else {'desired_operation':desired}},'task_generation':1},
        'current_control':1,'result':{'runtime':{'attempt_token':'original'}}}
    account={'id':'account','binding_version':1,'external_account_id':'current-member',
        'connection_origin':'https://board.test','private_self':{'self_url':'https://board.test/account'}}
    return SimpleNamespace(snapshot=lambda:(task,account,{'revision':1,'scope':{}}),
        verify_private_self=AsyncMock(),lease={'origins':['https://board.test']},
        page=SimpleNamespace(url='https://board.test/home',title=AsyncMock(return_value='Board')),
        document=AsyncMock(return_value='current-doc'),observations=[],task_id='original-task',
        actor=SimpleNamespace(session='original-session',epoch=1,native_runtime=SimpleNamespace(execution_ref={})))


@pytest.mark.asyncio
@pytest.mark.parametrize('desired,proposed',[('reply','publish'),('publish','reply')])
async def test_wrong_operation_cannot_record_or_report_a_successful_method(desired,proposed):
    function,inspect=record_method()
    value=controller(desired)
    with pytest.raises(MethodFactError) as caught:
        await function(value,{'operation':proposed,'target_url':'https://board.test/home','permission_selector':'rules'})
    assert str(caught.value)=='method_discovery_operation_mismatch'
    assert not value.observations and inspect.await_count==0


@pytest.mark.asyncio
@pytest.mark.parametrize('desired,proposed',[(None,'publish'),(None,'reply'),('publish','publish'),('reply','reply')])
async def test_matching_and_unspecified_operation_retain_the_original_method_path(desired,proposed):
    function,inspect=record_method()
    value=controller(desired)
    result=await function(value,{'operation':proposed,'target_url':'https://board.test/home','permission_selector':'rules'})
    assert result['operation']==proposed and inspect.await_count==1
    assert value.observations[0]['browser_method']['operations']==[proposed]


@pytest.mark.asyncio
async def test_account_adapter_passes_the_canonical_task_operation_to_gateway():
    @contextmanager
    def tx(*args):
        yield object()
    rpc=AsyncMock(return_value={'targets':[]})
    function=definition('services/api/lead_app/account_connection.py','discover',namespace={
        'tx':tx,'one':lambda *args:{'profile_id':'profile','platform':'web'},'rpc':rpc,
        'asyncio':asyncio,'derive_targets':lambda *args:[],'WorkResult':lambda **kwargs:kwargs})
    row={'id':'task','input':{'spec':{'account_id':'account','payload':{
        'target_urls':['https://board.test/thread/17'],'desired_operation':'reply'}}}}
    await function('agent',row)
    sent=rpc.await_args.args[3]
    assert sent['desired_operation']=='reply'
    assert sent['target_urls']==['https://board.test/thread/17']


def test_evaluation_requests_exact_outcomes_for_both_discovery_stages():
    tree=ast.parse((ROOT/'scripts/evaluate_native_browser.py').read_text())
    payloads=[node.args[2] for node in ast.walk(tree) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Attribute) and node.func.attr=='submit'
        and len(node.args)>2 and isinstance(node.args[0],ast.Constant) and node.args[0].value=='account'
        and isinstance(node.args[1],ast.Constant) and node.args[1].value=='discover']
    desired=[]
    for payload in payloads:
        fields={key.value:value for key,value in zip(payload.keys,payload.values,strict=True)}
        desired.append(fields.get('desired_operation'))
        assert 'target_urls' in fields
    assert sorted(value.value for value in desired if isinstance(value,ast.Constant))==['publish','reply']


def proposed_task(payload):
    from datetime import datetime, timedelta, timezone
    model=definition('services/api/lead_app/growth/tools.py','AccountInput',
        namespace={'Contract':Contract,'Field':Field,'Literal':Literal,'UUID':UUID})
    tree=ast.parse((ROOT/'services/api/lead_app/growth/store.py').read_text())
    branch=next(node for node in ast.walk(tree) if isinstance(node,ast.If)
        and isinstance(node.test,ast.Compare) and isinstance(node.test.comparators[0],ast.Constant)
        and node.test.comparators[0].value=='target.propose')
    values={'__name__':'lead_app.growth.store','__package__':'lead_app.growth',
        'command':SimpleNamespace(account_id='account',payload=payload),'c':object(),'aid':'agent',
        'refs':[],'one':lambda *args:{'platform':'web','provider':'chromium','connection_origin':'https://board.test'},
        'AccountInput':model,'target_url':lambda platform,url:url,'origin':origin,
        'TaskSpec':lambda **kwargs:kwargs,'submit_work':lambda c,aid,spec:spec,'audit':lambda *args:None,
        'utcnow':lambda:datetime.now(timezone.utc),'timedelta':timedelta}
    code=ast.Module(body=[node for node in branch.body if not isinstance(node,(ast.Import,ast.ImportFrom))],type_ignores=[])
    exec(compile(ast.fix_missing_locations(code),'<actual-target-propose-branch>','exec'),values)
    return values['refs'][0]


@pytest.mark.parametrize('desired',(None,'reply'))
def test_target_propose_retains_the_validated_optional_task_operation(desired):
    payload={'target_urls':['https://board.test/thread/17'],**({'desired_operation':desired} if desired else {})}
    assert proposed_task(payload)['payload']==payload


@pytest.mark.parametrize('url',('https://other.test/thread/17','javascript:alert(1)',
    'https://user:secret@board.test/thread/17','https://board.test/'+('x'*2000)))
def test_native_web_candidate_cannot_expand_the_account_navigation_origin(url):
    with pytest.raises(ValueError):
        proposed_task({'target_urls':[url],'desired_operation':'reply'})


@pytest.mark.asyncio
async def test_old_task_without_a_payload_keeps_generic_discovery():
    function,inspect=record_method()
    value=controller(None)
    task,account,intent=value.snapshot()
    del task['input']['spec']['payload']
    value.snapshot=lambda:(task,account,intent)
    result=await function(value,{'operation':'publish','target_url':'https://board.test/home','permission_selector':'rules'})
    assert result['operation']=='publish' and inspect.await_count==1


@pytest.mark.parametrize('desired',(None,'publish','reply'))
def test_discovery_goal_distinguishes_editor_and_rules_without_private_account_detour(desired):
    tree=ast.parse((ROOT/'services/api/lead_app/browser.py').read_text())
    function=next(node for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=='discover_account_targets')
    call=next(node for node in ast.walk(function) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='run_task')
    expression=next(keyword.value for keyword in call.keywords if keyword.arg=='goal')
    goal=eval(compile(ast.Expression(expression),'<actual-discovery-goal>','eval'),{'desired':desired})
    assert 'The editor page and rules page may differ.' in goal
    assert 'return to the required editor page before calling propose_method' in goal
    assert 'server independently opens target_url to recheck its rules' in goal
    assert 'server independently verifies the current acting identity' in goal
    assert 'Read your current private account' not in goal
    assert 'If the page does not provide' not in goal
    assert all(hidden not in goal for hidden in ('self_subject','prefix','suffix'))
    assert 'Never type into forms, publish, reply, log in, join, follow or change settings.' in goal
