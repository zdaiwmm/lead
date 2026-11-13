"""Only fixed method facts, never manager response bodies, become SDK feedback."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from lead_app import native_method_facts
from test_native_identity_error_feedback import manager


def test_explicit_method_failure_keeps_original_valueerror_message_and_safe_code():
    with pytest.raises(ValueError) as caught:
        native_method_facts._url(None)
    assert str(caught.value)=='browser_method_url_missing'
    assert getattr(caught.value,'code',None)=='browser_method_url_missing'


@pytest.mark.asyncio
@pytest.mark.parametrize('code',['browser_method_element_ambiguous','browser_method_permission_denied',
    'method_document_changed','identity_current_self_unestablished'])
async def test_record_method_receives_only_fixed_method_or_identity_feedback(code):
    private='synthetic-private-manager-body'
    async def response(request):
        return httpx.Response(409,json={'code':code,'detail':private,'selector':private})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ValueError) as caught:
            await manager(client)('record_method')
    assert code in str(caught.value) and private not in str(caught.value)


@pytest.mark.parametrize('operation,status,payload',[
    ('scope',409,{'code':'browser_method_element_ambiguous'}),
    ('inspect_self',409,{'code':'browser_method_element_ambiguous'}),
    ('record_method',500,{'code':'browser_method_element_ambiguous'}),
    ('record_method',403,{'code':'browser_method_element_ambiguous'}),
    ('record_method',409,{'code':'synthetic-private-manager-body'}),
    ('record_method',409,{'code':['browser_method_element_ambiguous']}),
    ('record_method',409,['browser_method_element_ambiguous']),
    ('record_method',409,None),
    ('record_method',409,{'detail':'browser_method_element_ambiguous'}),
])
@pytest.mark.asyncio
async def test_unrecognized_operation_status_or_payload_keeps_generic_http_failure(operation,status,payload):
    async def response(request):
        return httpx.Response(status,json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await manager(client)(operation)
    assert 'synthetic-private-manager-body' not in str(caught.value)
    assert 'browser_method_element_ambiguous' not in str(caught.value)


def test_untyped_dynamic_url_errors_are_not_promoted_to_safe_method_codes():
    with pytest.raises(ValueError) as caught:
        native_method_facts._url('http://[invalid')
    assert getattr(caught.value,'code',None) is None


def test_catalog_covers_only_explicit_constant_errors_and_rejects_unknown_codes():
    from lead_app.native_method_errors import METHOD_ERROR_HINTS, MethodFactError
    path=Path(native_method_facts.__file__)
    tree=ast.parse(path.read_text())
    codes={node.args[0].value for node in ast.walk(tree) if isinstance(node,ast.Call)
        and isinstance(node.func,ast.Name) and node.func.id=='MethodFactError'
        and node.args and isinstance(node.args[0],ast.Constant)}
    assert codes and codes<=METHOD_ERROR_HINTS.keys()
    with pytest.raises(ValueError) as caught:
        MethodFactError('synthetic-private-value')
    assert 'synthetic-private-value' not in str(caught.value)


def test_trace_projects_only_fixed_method_code_not_error_content(tmp_path):
    from lead_app.native_sdk_trace import SafeStepTrace
    trace=SafeStepTrace(tmp_path/'trace.json')
    trace.capture(SimpleNamespace(history=[{'model_output':{'action':[{'propose_method':{}}]},
        'result':[{'error':'Error executing action propose_method: browser_method_element_ambiguous: synthetic-private-detail'}]}]))
    assert trace.steps[0]['outcomes']==[{'status':'error','code':'browser_method_element_ambiguous'}]
    assert 'synthetic-private-detail' not in json.dumps(trace.steps)


@pytest.mark.asyncio
@pytest.mark.parametrize('count,visible,issue',[(0,True,'not_found'),(2,True,'not_unique'),(1,False,'not_visible')])
async def test_known_selector_field_has_only_fixed_issue_metadata(count,visible,issue):
    from unittest.mock import AsyncMock
    page=SimpleNamespace(locator=lambda _:SimpleNamespace(count=AsyncMock(return_value=count),is_visible=AsyncMock(return_value=visible)))
    with pytest.raises(ValueError) as caught:
        await native_method_facts._node(page,'private-css-selector',field='editor_body')
    assert str(caught.value)=='browser_method_element_ambiguous'
    assert caught.value.selector_issues==[{'field':'editor_body','issue':issue}]
    assert 'private-css-selector' not in str(caught.value.selector_issues)


@pytest.mark.asyncio
async def test_required_selector_failures_are_reported_together_without_relaxing_validation():
    from unittest.mock import AsyncMock
    page=SimpleNamespace(locator=lambda _:SimpleNamespace(count=AsyncMock(return_value=0),is_visible=AsyncMock(return_value=True)))
    with pytest.raises(ValueError) as caught:
        await native_method_facts._nodes(page,{'self_subject':'private-a','submit':'private-b'},['self_subject','submit'])
    assert str(caught.value)=='browser_method_element_ambiguous'
    assert caught.value.selector_issues==[{'field':'self_subject','issue':'not_found'},{'field':'submit','issue':'not_found'}]


def test_only_typed_allowlisted_selector_metadata_reaches_gateway_payload():
    from lead_app.native_method_errors import MethodFactError, method_error_metadata
    issue={'field':'self_subject','issue':'not_found','selector':'private-css'}
    error=MethodFactError('browser_method_element_ambiguous',selector_issues=[issue,
        {'field':'private-field','issue':'not_visible'},{'field':'submit','issue':'private-issue'}])
    assert str(error)=='browser_method_element_ambiguous'
    assert method_error_metadata(error)=={'selector_issues':[{'field':'self_subject','issue':'not_found'}]}
    untyped=ValueError('private-detail')
    untyped.selector_issues=[issue]
    assert method_error_metadata(untyped)=={}
    assert method_error_metadata(MethodFactError('browser_method_permission_denied',selector_issues=[issue]))=={}


@pytest.mark.asyncio
async def test_actual_manager_feedback_uses_only_fixed_field_issue_values():
    async def response(request):
        return httpx.Response(409,json={'code':'browser_method_element_ambiguous','detail':'private-detail',
            'selector_issues':[{'field':'self_subject','issue':'not_found','selector':'private-css'},
                {'field':'submit','issue':'not_visible'}, {'field':'private-field','issue':'not_unique'},
                {'field':'editor_body','issue':'private-issue'}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(ValueError) as caught:
            await manager(client)('record_method')
    feedback=str(caught.value)
    assert 'self_subject=not_found' in feedback and 'submit=not_visible' in feedback
    assert 'private' not in feedback


@pytest.mark.parametrize('value',(None,'private',{'field':'submit','issue':'not_found'},[{'field':'private','issue':'not_found'}]))
def test_unknown_or_malformed_issue_fields_do_not_change_fixed_feedback(value):
    from lead_app.native_method_errors import method_rpc_feedback
    normal=method_rpc_feedback('record_method',409,{'code':'browser_method_element_ambiguous'})
    assert method_rpc_feedback('record_method',409,{'code':'browser_method_element_ambiguous','selector_issues':value})==normal
