"""The model sees the same explicit method proposal contract as the manager."""
import ast
import inspect
from pathlib import Path
from typing import Annotated, Literal, get_type_hints

import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic.json_schema import SkipJsonSchema

ROOT=Path(__file__).resolve().parents[1]
PUBLISH={'self_subject','editor_target','editor_body','submit'}


def proposal_tool(**namespace):
    tree=ast.parse((ROOT/'scripts/browser_use_runner.py').read_text())
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.AsyncFunctionDef) and n.name=='propose_method')
    description=node.decorator_list[0].args[0].value
    node.decorator_list=[]
    classes=[n for n in tree.body if isinstance(n,ast.ClassDef)
        and n.name in ('PublishMethodSelectors','ReplyMethodSelectors')]
    scope={'Annotated':Annotated,'Literal':Literal,'BaseModel':BaseModel,'ConfigDict':ConfigDict,'Field':Field,'SkipJsonSchema':SkipJsonSchema,**namespace}
    exec(compile(ast.fix_missing_locations(ast.Module(body=classes+[node],type_ignores=[])),'<actual-runner-tool>','exec'),scope)
    return scope['propose_method'],description


def test_actual_tool_annotation_exposes_only_required_observed_editor_fields():
    function,_=proposal_tool()
    schema=TypeAdapter(get_type_hints(function,include_extras=True)['selectors']).json_schema()
    models=[schema['$defs'][branch['$ref'].rsplit('/',1)[-1]] for branch in schema['anyOf']]
    assert {frozenset(model['required']) for model in models}=={frozenset(PUBLISH-{'self_subject'}),frozenset((PUBLISH-{'self_subject'})|{'editor_parent'})}
    for model in models:
        assert model['additionalProperties'] is False
        assert set(model['properties'])==set(model['required'])
        assert all(field['description'] and field['type']=='string' for field in model['properties'].values())


@pytest.mark.parametrize('reply',(False,True))
def test_complete_selector_dto_keeps_exact_fields(reply):
    function,_=proposal_tool()
    values={key:'observed-css' for key in PUBLISH|({'editor_parent'} if reply else set())}
    parsed=TypeAdapter(get_type_hints(function,include_extras=True)['selectors']).validate_python(values)
    assert parsed.model_dump()==values


@pytest.mark.parametrize('reply',(False,True))
@pytest.mark.parametrize('change',('missing','unknown'))
def test_missing_or_unknown_selector_fields_are_not_silently_accepted(reply,change):
    function,_=proposal_tool()
    values={key:'observed-css' for key in PUBLISH|({'editor_parent'} if reply else set())}
    if change=='missing':
        del values['editor_body']
    else:
        values['guessed_field']='observed-css'
    with pytest.raises(ValueError):
        TypeAdapter(get_type_hints(function,include_extras=True)['selectors']).validate_python(values)


def test_fixed_contract_hint_lists_exact_keys_without_private_data():
    from lead_app.native_method_errors import method_rpc_feedback
    hint=method_rpc_feedback('record_method',409,{'code':'browser_method_selector_contract_missing','detail':'not-for-model'})
    assert all(key in hint for key in PUBLISH|{'editor_parent'})
    assert 'publish' in hint and 'reply' in hint and 'not-for-model' not in hint


def test_only_observable_parameters_are_shown_and_legacy_bounds_remain_runtime_checked():
    from pydantic import create_model
    function,description=proposal_tool()
    fields=get_type_hints(function,include_extras=True)
    signature=inspect.signature(function)
    model=create_model('ObservedMethodProposal',**{key:(value,... if signature.parameters[key].default is inspect.Parameter.empty
        else signature.parameters[key].default) for key,value in fields.items()})
    schema=model.model_json_schema()
    assert set(schema['required'])=={'target_url','operation','selectors','permission_selector','permission_quote'}
    assert set(schema['properties'])==set(schema['required'])
    properties=schema['properties']
    assert properties['target_url']['maxLength']==2000
    assert 'destination' in properties['target_url']['description']
    for name in ('self_subject_prefix','self_subject_suffix'):
        assert TypeAdapter(fields[name]).validate_python('')==''
        with pytest.raises(ValueError):
            TypeAdapter(fields[name]).validate_python('x'*151)
    assert 'target_url page' in properties['permission_selector']['description']
    assert (properties['permission_quote']['minLength'],properties['permission_quote']['maxLength'])==(20,3000)
    assert all(key in description for key in (PUBLISH-{'self_subject'})|{'editor_parent'})


async def _sdk_registry_probe():
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import httpx
    from browser_use import ActionResult, Tools

    # Read the existing manager extractor without importing its pytest module
    # into the separately locked SDK environment.
    manager_tree=ast.parse((ROOT/'tests/test_native_identity_error_feedback.py').read_text())
    manager_node=next(node for node in manager_tree.body
        if isinstance(node,ast.FunctionDef) and node.name=='manager')
    manager_scope={'ast':ast,'Path':Path,'__file__':str(ROOT/'tests/test_native_identity_error_feedback.py')}
    exec(compile(ast.Module(body=[manager_node],type_ignores=[]),'<original-manager-extractor>','exec'),manager_scope)
    manager=manager_scope['manager']

    received=[]
    async def rpc(request):
        received.append(json.loads(request.content))
        return httpx.Response(200,json={'recorded':True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(rpc)) as client:
        browser=SimpleNamespace(assert_fresh_observation=AsyncMock())
        function,description=proposal_tool(browser=browser,manager=manager(client),ActionResult=ActionResult,json=json)
        tools=Tools()
        tools.action(description)(function)
        action=tools.registry.registry.actions['propose_method']
        schema=action.param_model.model_json_schema()
        for operation in ('publish','reply'):
            selectors={key:'observed-css' for key in PUBLISH|({'editor_parent'} if operation=='reply' else set())}
            values={'target_url':'https://board.test/home','operation':operation,'selectors':selectors,
                'self_subject_prefix':'Signed in as ','self_subject_suffix':'',
                'permission_selector':'section.rules','permission_quote':'Ordinary members may publish and reply using authorized browser automation.'}
            result=await tools.registry.execute_action('propose_method',values)
            assert result.success is True
            assert received[-1]['operation']=='record_method' and received[-1]['args']==values
            for change in ('missing','unknown'):
                bad={**values,'selectors':dict(selectors)}
                if change=='missing':
                    del bad['selectors']['editor_body']
                else:
                    bad['selectors']['guessed_field']='observed-css'
                count=len(received)
                try:
                    await tools.registry.execute_action('propose_method',bad)
                except (RuntimeError,ValueError):
                    pass
                else:
                    raise AssertionError('Invalid selectors reached the actual SDK tool')
                assert len(received)==count
        assert len(received)==2 and browser.assert_fresh_observation.await_count==2
        return {'schema':schema,
            'manager_operation_names':[row['operation'] for row in received],
            'manager_selector_keys':[sorted(row['args']['selectors']) for row in received],
            'missing_unknown_denied_before_rpc':4}


@pytest.mark.skipif(__import__('os').environ.get('LEAD_NATIVE_PRODUCT_TESTS')!='1',
    reason='Explicit pinned SDK environment required')
def test_actual_sdk_registry_routes_exact_dtos_through_original_manager():
    import json
    import os
    import subprocess

    from lead_app.config import STATE

    runtime=Path(os.environ['LEAD_BROWSER_RUNTIME_DIR'])
    # Only the SDK subprocess loads browser_use and its Python 3.12 dependencies.
    # The application pytest process retains its independent dependency lock.
    probe='''import ast,asyncio,importlib.metadata,json,sys
from pathlib import Path
from typing import Annotated,Literal
from pydantic import BaseModel,ConfigDict,Field
from pydantic.json_schema import SkipJsonSchema
ROOT=Path(sys.argv[2])
source=ast.parse(Path(sys.argv[1]).read_text())
definitions=[node for node in source.body if
    isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in ('proposal_tool','_sdk_registry_probe')
    or isinstance(node,ast.Assign) and any(isinstance(target,ast.Name) and target.id=='PUBLISH' for target in node.targets)]
assert len(definitions)==3
exec(compile(ast.Module(body=definitions,type_ignores=[]),sys.argv[1],'exec'))
result=asyncio.run(_sdk_registry_probe())
result.update(sdk_version=importlib.metadata.version('browser-use'),python_version=list(sys.version_info[:2]))
print(json.dumps(result,sort_keys=True))
'''
    environment={key:value for key,value in os.environ.items()
        if not key.startswith(('LEAD_','OPENAI_','DEEPSEEK_','ANTHROPIC_','BROWSER_USE_'))}
    environment.update(PYTHONPATH=os.pathsep.join((str(runtime/'site-packages'),str(ROOT/'services/api'))),
        ANONYMIZED_TELEMETRY='false',BROWSER_USE_CLOUD_SYNC='false',BROWSER_USE_VERSION_CHECK='false',
        BROWSER_USE_SETUP_LOGGING='false')
    result=subprocess.run([os.environ['LEAD_BROWSER_RUNTIME_PYTHON'],'-B','-S','-c',probe,str(Path(__file__)),str(ROOT)],
        cwd=runtime,env=environment,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr[-4000:]
    records=[line for line in result.stdout.splitlines() if line.startswith('{')]
    assert records,result.stdout[-2000:]
    observed=json.loads(records[-1])
    assert observed['sdk_version']=='0.13.14' and observed['python_version']==[3,12]
    assert observed['manager_operation_names']==['record_method','record_method']
    assert observed['missing_unknown_denied_before_rpc']==4
    (STATE/'method-tool-sdk-schema.json').write_text(json.dumps(observed,indent=2))


def test_proposal_tool_explains_separate_rules_page_without_hidden_identity_hints():
    _,description=proposal_tool()
    assert 'rules may be on a different page' in description
    assert 'return to the editor before proposing' in description
    assert all(hidden not in description for hidden in ('self_subject','prefix','suffix'))
