"""The legacy source recorder must honor the native page observation boundary."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app.native_research import NativeResearchPage


def reader(tree, frame_urls):
    runtime=SimpleNamespace(scope_view=AsyncMock(return_value={
        'task_id':'task','generation':1,'target_id':'page','origins':['https://a.example'],
        'targets':{'page':'page'}}))
    actor=SimpleNamespace(lock=asyncio.Lock(),native_runtime=runtime)
    result=NativeResearchPage(actor,'run')
    result.lease={'target_id':'page'}
    session=SimpleNamespace(send=AsyncMock(return_value={'frameTree':tree}),detach=AsyncMock())
    result.context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session))
    result.page=SimpleNamespace(frames=[SimpleNamespace(url=url) for url in frame_urls])
    return result,session


async def test_loaded_foreign_frame_is_rejected_before_source_capture():
    tree={'frame':{'id':'main','loaderId':'document-1','url':'https://a.example/'}}
    page,session=reader(tree,['https://a.example/','https://private-b.example/account'])
    with pytest.raises(ValueError,match='observation_frame_out_of_scope'):
        await page.observation_scope()
    session.detach.assert_awaited_once()


async def test_same_url_reload_changes_observation_and_native_pages_do_not_share_lock():
    tree={'frame':{'id':'main','loaderId':'document-1','url':'https://a.example/'}}
    page,session=reader(tree,['https://a.example/'])
    before=await page.observation_scope()
    tree['frame']['loaderId']='document-2'
    assert before != await page.observation_scope()
    assert page.command_lock is not page.actor.lock
    assert session.detach.await_count==2
