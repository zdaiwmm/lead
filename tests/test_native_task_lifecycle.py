"""Late close owns its original runtime and cannot close a new generation page."""
from types import SimpleNamespace

import pytest
from lead_app.native_task import NativeTask


@pytest.mark.parametrize('current',['none','replacement'])
@pytest.mark.asyncio
async def test_task_close_is_idempotent_on_the_original_runtime_only(current):
    released=[]
    replacement=[]
    async def release(target):
        released.append(target)
    async def other_release(target):
        replacement.append(target)
    actor=SimpleNamespace(native_runtime=SimpleNamespace(release_page=release))
    task=NativeTask(actor,'original-task')
    task.lease={'target_id':'original-target'}
    task.page=object()
    actor.native_runtime=None if current=='none' else SimpleNamespace(release_page=other_release)
    await task.close()
    await task.close()
    assert released==['original-target'] and replacement==[]
    assert task.lease is None and task.page is None
