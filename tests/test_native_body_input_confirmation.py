"""Only a boolean escapes a live, already-authorized body-node read."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app import browser_execution


def session(*,matches=True,changed=False):
    runtime=SimpleNamespace(
        callFunctionOn=AsyncMock(return_value={'result':{'value':matches}}),
        releaseObject=AsyncMock(return_value={}))
    client=SimpleNamespace(send=SimpleNamespace(
        DOM=SimpleNamespace(resolveNode=AsyncMock(return_value={'object':{'objectId':'owned-body'}})),
        Runtime=runtime))
    page=SimpleNamespace(cdp_client=client,session_id='owned-session')
    value=SimpleNamespace(live_scope=AsyncMock(side_effect=[('original',),('changed' if changed else 'original',)]),
        cdp_client_for_node=AsyncMock(return_value=page))
    return value,runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('matches',(True,False))
async def test_confirmation_returns_only_real_boolean_without_current_field_value(matches):
    browser,runtime=session(matches=matches)
    result=await browser_execution.authorized_body_matches(browser,SimpleNamespace(backend_node_id=7),'approved-body')
    assert result is matches
    assert runtime.callFunctionOn.await_args.kwargs['params']['arguments']==[{'value':'approved-body'}]
    script=runtime.callFunctionOn.await_args.kwargs['params']['functionDeclaration']
    assert "this.tagName==='TEXTAREA'" in script and 'this.isConnected' in script and 'this.value===expected' in script
    runtime.releaseObject.assert_awaited_once()
    assert browser.live_scope.await_count==2


@pytest.mark.asyncio
async def test_changed_owner_or_document_cannot_return_successful_confirmation():
    browser,_=session(changed=True)
    with pytest.raises(browser_execution.ScopeViolation,match='scope_changed_during_observation'):
        await browser_execution.authorized_body_matches(browser,SimpleNamespace(backend_node_id=7),'approved-body')


@pytest.mark.asyncio
@pytest.mark.parametrize('value',(None,'private-current-value',{},1))
async def test_unproved_or_nonboolean_result_is_fixed_content_free_failure(value):
    browser,_=session(matches=value)
    with pytest.raises(browser_execution.ScopeViolation) as caught:
        await browser_execution.authorized_body_matches(browser,SimpleNamespace(backend_node_id=7),'approved-body')
    assert str(caught.value)=='authorized_body_confirmation_unavailable'


@pytest.mark.asyncio
async def test_protocol_error_does_not_echo_a_field_value():
    browser,runtime=session()
    runtime.callFunctionOn.side_effect=RuntimeError('private-current-value')
    with pytest.raises(browser_execution.ScopeViolation) as caught:
        await browser_execution.authorized_body_matches(browser,SimpleNamespace(backend_node_id=7),'approved-body')
    assert str(caught.value)=='authorized_body_confirmation_unavailable'
    runtime.releaseObject.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirmation_cancellation_is_not_converted_to_a_success_or_tool_error():
    import asyncio
    browser,runtime=session()
    runtime.callFunctionOn.side_effect=asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await browser_execution.authorized_body_matches(browser,SimpleNamespace(backend_node_id=7),'approved-body')


def test_negative_confirmation_has_a_fixed_trace_code_without_field_content():
    from lead_app.native_sdk_trace import _outcome
    assert _outcome({'error':'body_matches_authorized=false. The current editor does not contain the complete authorized body.'})=={
        'status':'error','code':'authorized_body_not_present'}
    assert _outcome({'error':'authorized_body_confirmation_unavailable'})=={
        'status':'error','code':'authorized_body_confirmation_unavailable'}
