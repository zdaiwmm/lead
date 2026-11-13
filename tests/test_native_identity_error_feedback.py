"""Only fixed identity error codes may reach native SDK tools over manager RPC."""
import ast
from pathlib import Path

import httpx
import pytest
from lead_app import native_identity_facts


def manager(client):
    path=Path(__file__).resolve().parents[1]/'scripts/browser_use_runner.py'
    tree=ast.parse(path.read_text())
    methods=[node for node in ast.walk(tree) if isinstance(node,ast.AsyncFunctionDef) and node.name=='manager']
    assert len(methods)==1
    namespace={'client':client,'data':{'gateway_endpoint':'http://manager.test','agent_id':'fixture',
        'gateway_token':'synthetic-only','run_id':'fixture','command_path':'native/command'}}
    exec(compile(ast.Module(body=methods,type_ignores=[]),str(path),'exec'),namespace)
    return namespace['manager']


def test_original_identity_affix_failure_keeps_its_code_and_valueerror_contract():
    with pytest.raises(ValueError) as caught:
        native_identity_facts._extract('Signed in as fixture-member · Settings','Signed in as ',' (absent)')
    assert str(caught.value)=='identity_value_missing'
    assert getattr(caught.value,'code',None)=='identity_value_missing'


@pytest.mark.asyncio
@pytest.mark.parametrize('operation',['inspect_self','report_connection'])
async def test_actual_manager_passes_only_allowlisted_identity_feedback(operation):
    secret='synthetic-private-body-must-not-be-forwarded'
    async def server(request):
        return httpx.Response(409,json={'code':'identity_value_missing','detail':secret,'password':secret})
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(Exception) as caught:
            await manager(client)(operation,{'self_subject_suffix':' (absent)'})
    assert 'identity_value_missing' in str(caught.value)
    assert secret not in str(caught.value)


def test_typed_identity_error_cannot_attach_an_unknown_private_code():
    from lead_app.native_identity_errors import IdentityFactError
    with pytest.raises(ValueError) as caught:
        IdentityFactError('synthetic-private-value')
    assert 'synthetic-private-value' not in str(caught.value)


@pytest.mark.asyncio
async def test_non_json_manager_body_is_not_exposed_as_feedback():
    async def server(request):
        return httpx.Response(409,content=b'synthetic-private-response-body')
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await manager(client)('inspect_self')
    assert 'synthetic-private-response-body' not in str(caught.value)


@pytest.mark.parametrize('operation,status,payload',[
    ('inspect_self',500,{'code':'identity_value_missing'}),
    ('inspect_self',403,{'code':'identity_value_missing'}),
    ('scope',409,{'code':'identity_value_missing'}),
    ('inspect_self',409,{'code':'private-subject-secret','detail':'private-subject-secret'}),
    ('inspect_self',409,{'code':['identity_value_missing'],'detail':'private-subject-secret'}),
    ('inspect_self',409,['identity_value_missing']),
])
@pytest.mark.asyncio
async def test_unknown_manager_errors_keep_generic_http_failure(operation,status,payload):
    async def server(request):
        return httpx.Response(status,json=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as client:
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await manager(client)(operation)
    assert 'private-subject-secret' not in str(caught.value)
    assert 'identity_value_missing' not in str(caught.value)
