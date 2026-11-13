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
