"""Native SDK settings and discovery data stay inside existing authority."""
import ast
from pathlib import Path
from typing import Literal, get_args, get_type_hints

import pytest
from pydantic import TypeAdapter, ValidationError

ROOT=Path(__file__).resolve().parents[1]


def runner_tree():
    return ast.parse((ROOT/'scripts/browser_use_runner.py').read_text())


def test_native_agent_enables_sdk_reasoning_planning_and_two_action_batches():
    call=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='Agent')
    values={arg.arg:eval(compile(ast.Expression(arg.value),'<config>','eval'),
        {'native_scope':{'target_id':'owned'}}) for arg in call.keywords
        if arg.arg in ('flash_mode','use_thinking','enable_planning','max_actions_per_step','use_vision')}
    assert values=={'flash_mode':False,'use_thinking':True,'enable_planning':True,
        'max_actions_per_step':2,'use_vision':False}


def test_method_operation_schema_is_the_existing_explicit_enum():
    node=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.AsyncFunctionDef) and node.name=='propose_method')
    node.decorator_list=[]
    node.body=[ast.Pass()]
    namespace={'Literal':Literal}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'<tool>','exec'),namespace)
    annotation=get_type_hints(namespace['propose_method'])['operation']
    assert get_args(annotation)==('publish','reply')
    adapter=TypeAdapter(annotation)
    assert adapter.validate_python('publish')=='publish' and adapter.validate_python('reply')=='reply'
    for value in ('post','Publish','reply_post',''):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_legacy_agent_configuration_remains_unchanged():
    call=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='Agent')
    values={arg.arg:eval(compile(ast.Expression(arg.value),'<config>','eval'),{'native_scope':None})
        for arg in call.keywords if arg.arg in ('flash_mode','use_thinking','enable_planning','max_actions_per_step','use_vision')}
    assert values=={'flash_mode':True,'use_thinking':False,'enable_planning':False,
        'max_actions_per_step':1,'use_vision':False}


def test_discovery_context_exposes_starting_pages_and_original_scope_as_data():
    tree=ast.parse((ROOT/'services/api/lead_app/native_runner.py').read_text())
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='discovery_task_context']
    assert len(functions)==1
    namespace={}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<context>','exec'),namespace)
    task={'input':{'spec':{'payload':{'target_urls':['https://board.test/posts/17']}}}}
    account={'connection_origin':'https://board.test','password':'never-export'}
    intent={'scope':{'target_scope':'authenticated_permitted_targets','target_ids':['https://board.test/'],
        'excluded_targets':['https://board.test/private']}}
    result=namespace['discovery_task_context'](task,account,intent)
    assert result=={'starting_urls':['https://board.test/posts/17'],'account_origin':'https://board.test',
        'intent_scope':intent['scope']}
    assert 'password' not in str(result) and 'never-export' not in str(result)
