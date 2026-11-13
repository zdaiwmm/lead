"""Native SDK settings and discovery data stay inside existing authority."""
import ast
from pathlib import Path
from typing import Literal, get_args
from urllib.parse import urlsplit, urlunsplit

import pytest
from pydantic import TypeAdapter, ValidationError

ROOT=Path(__file__).resolve().parents[1]


def runner_tree():
    return ast.parse((ROOT/'scripts/browser_use_runner.py').read_text())


@pytest.mark.parametrize('image_model',(None,'deepseek-flash'))
def test_native_agent_uses_full_decisions_with_optional_vision_and_two_action_batches(image_model):
    call=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='Agent')
    values={arg.arg:eval(compile(ast.Expression(arg.value),'<config>','eval'),
        {'native_scope':{'target_id':'owned'},'image_model':image_model}) for arg in call.keywords
        if arg.arg in ('flash_mode','use_thinking','enable_planning','max_actions_per_step','use_vision')}
    assert values=={'flash_mode':False,'use_thinking':True,'enable_planning':True,
        'max_actions_per_step':2,'use_vision':bool(image_model)}


def test_method_operation_schema_is_the_existing_explicit_enum():
    node=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.AsyncFunctionDef) and node.name=='propose_method')
    namespace={'Literal':Literal}
    annotation=eval(compile(ast.Expression(next(arg.annotation for arg in node.args.args if arg.arg=='operation')),
        '<operation-type>','eval'),namespace)
    assert get_args(annotation)==('publish','reply')
    adapter=TypeAdapter(annotation)
    assert adapter.validate_python('publish')=='publish' and adapter.validate_python('reply')=='reply'
    for value in ('post','Publish','reply_post',''):
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_legacy_agent_configuration_remains_unchanged():
    call=next(node for node in ast.walk(runner_tree()) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='Agent')
    values={arg.arg:eval(compile(ast.Expression(arg.value),'<config>','eval'),{'native_scope':None,'image_model':None})
        for arg in call.keywords if arg.arg in ('flash_mode','use_thinking','enable_planning','max_actions_per_step','use_vision')}
    assert values=={'flash_mode':True,'use_thinking':False,'enable_planning':False,
        'max_actions_per_step':1,'use_vision':False}


def test_discovery_context_exposes_starting_pages_and_original_scope_as_data():
    tree=ast.parse((ROOT/'services/api/lead_app/native_runner.py').read_text())
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='discovery_task_context']
    assert len(functions)==1
    namespace={'urlsplit':urlsplit,'urlunsplit':urlunsplit}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<context>','exec'),namespace)
    task={'input':{'spec':{'payload':{'target_urls':['https://board.test/posts/17']}}}}
    account={'connection_origin':'https://board.test','password':'never-export'}
    intent={'scope':{'target_scope':'authenticated_permitted_targets','target_ids':['https://board.test/'],
        'excluded_targets':['https://board.test/private']}}
    result=namespace['discovery_task_context'](task,account,intent)
    assert result=={'starting_urls':['https://board.test/posts/17'],'account_origin':'https://board.test',
        'intent_scope':intent['scope']}
    assert 'password' not in str(result) and 'never-export' not in str(result)


def test_discovery_prompt_url_projection_omits_url_secrets_and_malformed_scope_values():
    tree=ast.parse((ROOT/'services/api/lead_app/native_runner.py').read_text())
    functions=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='discovery_task_context']
    namespace={'urlsplit':urlsplit,'urlunsplit':urlunsplit}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<context>','exec'),namespace)
    private='https://user:private@board.test/home?token=private#private'
    result=namespace['discovery_task_context']({'input':{'spec':{'payload':{'target_urls':[private]*20}}}},
        {'connection_origin':'https://user:private@board.test?token=private'},
        {'scope':{'target_scope':{'private':'value'},'target_ids':[private,{},'invalid'],'excluded_targets':'private'}})
    assert result=={'starting_urls':['https://board.test/home'],'account_origin':'https://board.test',
        'intent_scope':{'target_scope':None,'target_ids':['https://board.test/home'],'excluded_targets':[]}}
    assert 'private' not in str(result)
