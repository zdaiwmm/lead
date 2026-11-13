"""Failed receipt proposals get fixed, private-data-free corrective feedback."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app.native_action import NativeAction


@pytest.mark.parametrize('count,text,code',[(0,'','receipt_body_selector_unavailable'),
    (1,'Timestamp and permalink','receipt_body_value_mismatch')])
@pytest.mark.asyncio
async def test_original_failed_receipt_observer_keeps_typed_body_diagnosis(count,text,code):
    node=SimpleNamespace(count=AsyncMock(return_value=count),is_visible=AsyncMock(return_value=True),
        inner_text=AsyncMock(return_value=text))
    controller=NativeAction.__new__(NativeAction)
    controller.action={'type':'publish','intent':{'body':'Private immutable authorized body'}}
    controller.page=SimpleNamespace(url='https://board.test/posts/7',locator=lambda _:node)
    controller.descriptor=AsyncMock()
    controller.document=AsyncMock(return_value='original-document')
    controller.read_receipt=AsyncMock(return_value=None)
    controller.snapshot=lambda:({}, {'external_account_id':'private.current.subject'}, {})
    with pytest.raises(ValueError) as caught:
        await controller.report_receipt({'selectors':{'author':'#actual-author','body':'#wrong-body','url':'#actual-url'}})
    assert getattr(caught.value,'code',None)==code
    assert 'Private immutable' not in str(caught.value)
    assert 'private.current.subject' not in str(caught.value)
    assert '#wrong-body' not in str(caught.value)


def test_receipt_tool_explains_value_nodes_and_exact_current_url():
    from lead_app.local_executor_browser import browser_tools
    tool=next(row for row in browser_tools('action') if row['name']=='report_receipt')
    properties=tool['inputSchema']['properties']['selectors']['properties']
    assert 'value' in properties['author'].get('description','')
    assert 'exact authorized body' in properties['body'].get('description','')
    assert 'current page URL' in properties['url'].get('description','')
    assert 'original parent' in properties['parent'].get('description','')


@pytest.mark.parametrize('operation,status,payload',[
    ('report_receipt',500,{'code':'receipt_body_value_mismatch'}),
    ('browser_observe',409,{'code':'receipt_body_value_mismatch'}),
    ('report_receipt',409,{'code':'unclassified private detail'}),
    ('report_receipt',409,{'code':['receipt_body_value_mismatch']}),
    ('report_receipt',409,['receipt_body_value_mismatch']),
])
def test_receipt_feedback_never_promotes_unclassified_detail(operation,status,payload):
    from lead_app.native_receipt_errors import receipt_rpc_feedback
    assert receipt_rpc_feedback(operation,status,payload) is None


def test_fixed_receipt_feedback_and_owner_protocol_contain_no_observed_values():
    from lead_app.native_receipt_errors import (
        ReceiptFactError,
        receipt_protocol_instructions,
        receipt_rpc_feedback,
    )
    feedback=receipt_rpc_feedback('report_receipt',409,{'code':'receipt_body_value_mismatch',
        'detail':'private-body-text','selector':'#private-body-selector'})
    assert feedback.startswith('receipt_body_value_mismatch:')
    assert 'private-body' not in feedback
    with pytest.raises(ValueError,match='unrecognized_receipt_error_code'):
        ReceiptFactError('private-body-text')
    protocol=receipt_protocol_instructions()
    assert 'exact authorized body' in protocol and 'Never resubmit' in protocol
    assert not any(value in protocol for value in ('codex-beta','7001','note-','marin'))


@pytest.mark.asyncio
async def test_receipt_feedback_crosses_actual_worker_manager_allowlist_without_response_detail():
    import ast
    from pathlib import Path
    from uuid import uuid4

    import httpx
    from lead_app.native_identity_errors import identity_rpc_feedback
    from lead_app.native_method_errors import method_rpc_feedback
    from lead_app.native_receipt_errors import receipt_rpc_feedback

    tree=ast.parse((Path(__file__).parents[1]/'services/api/lead_app/local_executor_browser.py').read_text())
    worker=next(node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_worker')
    manager=next(node for node in ast.walk(worker) if isinstance(node,ast.AsyncFunctionDef) and node.name=='manager')
    async def response(request):
        return httpx.Response(409,json={'code':'receipt_body_value_mismatch','detail':'private receipt text'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        namespace={'client':client,'setup':{'gateway_endpoint':'https://manager.test','agent_id':'fixture',
            'gateway_token':'synthetic','command_path':'native/task-command','run_id':'original'},
            'uuid4':uuid4,'identity_rpc_feedback':identity_rpc_feedback,'method_rpc_feedback':method_rpc_feedback,
            'receipt_rpc_feedback':receipt_rpc_feedback,'ToolFeedbackError':ValueError}
        exec(compile(ast.Module(body=[manager],type_ignores=[]),'<actual-worker-manager>','exec'),namespace)
        with pytest.raises(ValueError,match='receipt_body_value_mismatch') as caught:
            await namespace['manager']('report_receipt',{'selectors':{'body':'#private-selector'}})
    assert 'private receipt text' not in str(caught.value) and '#private-selector' not in str(caught.value)
