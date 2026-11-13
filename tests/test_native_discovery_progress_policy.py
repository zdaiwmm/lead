"""Seen DOM remains diagnostic during discovery
real hard bounds still apply."""
import ast
import asyncio
import hashlib
import json
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from lead_app.native_method_errors import SELECTOR_ERROR_CODES, MethodFactError

ROOT=Path(__file__).resolve().parents[1]


def method(name,task):
    tree=ast.parse((ROOT/'services/api/lead_app/native_task.py').read_text())
    function=next(node for node in ast.walk(tree) if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name==name)
    # Test the real methods without importing process-owned DB configuration.
    function.body=[node for node in function.body if not isinstance(node,ast.ImportFrom)]
    helpers=[node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_discovery_task']
    @contextmanager
    def tx(*args):
        def execute(sql,values):
            task['result']['runtime']['native_progress']=values[0]
        yield SimpleNamespace(execute=execute)
    scope={'tx':tx,'one':lambda *args:task,'j':lambda value:value,
        'asyncio':asyncio,'MethodFactError':MethodFactError,'SELECTOR_ERROR_CODES':SELECTOR_ERROR_CODES}
    exec(compile(ast.fix_missing_locations(ast.Module(body=helpers+[function],type_ignores=[])),'<current-native-task>','exec'),scope)
    return scope[name]


def task(discovery=True,*,no_progress=3,fragments=0,corrections=0):
    return {'type':'growth.account' if discovery else 'growth.channel',
        'input':{'spec':{'purpose':'discover' if discovery else 'publish'}},
        'result':{'runtime':{'native_progress':{'no_progress':no_progress,'fragments':fragments,'corrections':corrections}}}}


def controller():
    return SimpleNamespace(actor=SimpleNamespace(aid='fixture'),task_id='original',steps=0,
        fragment_failures=0,total_corrections=0,corrections=0)


def test_discovery_restarts_bounded_fragment_without_erasing_seen_state_diagnostic():
    row=task()
    value=controller()
    method('start_fragment',row)(value)
    assert row['result']['runtime']['native_progress']['no_progress']==3
    assert value.corrections==0 and value.fragment_failures==0


@pytest.mark.parametrize('discovery,fragments,corrections',[(False,0,0),(True,3,0),(True,0,8)])
def test_other_stage_and_existing_fragment_correction_limits_still_reject(discovery,fragments,corrections):
    row=task(discovery,fragments=fragments,corrections=corrections)
    with pytest.raises(ValueError,match='native_task_progress_limit'):
        method('start_fragment',row)(controller())


@pytest.mark.asyncio
@pytest.mark.parametrize('discovery',(True,False))
async def test_repeated_dom_is_diagnostic_only_for_canonical_discovery(discovery):
    row=task(discovery,no_progress=2)
    value=controller()
    facts={'url':'https://board.test/editor','text':'unchanged','fields':[],'viewport':[0,0]}
    progress=row['result']['runtime']['native_progress']
    progress['seen_states']=[hashlib.sha256(json.dumps(facts,sort_keys=True).encode()).hexdigest()]
    value.remaining_seconds=lambda:180
    value.descriptor=AsyncMock()
    value.verify_private_self=AsyncMock()
    value.document=AsyncMock(return_value='current-document')
    value.page=SimpleNamespace(evaluate=AsyncMock(return_value=facts))
    result=await method('observe_progress',row)(value)
    assert progress['no_progress']==3
    assert result['stop'] is (not discovery)
    assert value.corrections==(0 if discovery else 3)


@pytest.mark.asyncio
@pytest.mark.parametrize('steps,failures,total',[(20,0,0),(0,2,0),(0,0,8)])
async def test_discovery_cannot_bypass_real_active_step_or_correction_limits(steps,failures,total):
    row=task()
    value=controller()
    value.steps=steps
    value.fragment_failures=failures
    value.total_corrections=total
    value.remaining_seconds=lambda:180
    value.descriptor=AsyncMock()
    value.verify_private_self=AsyncMock()
    value.document=AsyncMock(return_value='current-document')
    value.page=SimpleNamespace(evaluate=AsyncMock(return_value={'url':'https://board.test','text':'stable','fields':[]}))
    assert (await method('observe_progress',row)(value))['stop'] is True


def test_original_fragment_deadline_still_rejects():
    row=task()
    value=controller()
    value.deadline=1
    function=method('remaining_seconds',row)
    function.__globals__['time']=SimpleNamespace(monotonic=lambda:2)
    with pytest.raises(ValueError,match='native_fragment_deadline'):
        function(value)


def commands(row,errors):
    value=controller()
    value.remaining_seconds=lambda:180
    value.record_method=AsyncMock(side_effect=[*errors,{'state':'method_observed'}])
    value._command=MethodType(method('_command',row),value)
    value.note_correction=partial(method('note_correction',row),value)
    return value,partial(method('command',row),value)


@pytest.mark.asyncio
@pytest.mark.parametrize('code',('browser_method_selector_missing','browser_method_element_ambiguous'))
async def test_discovery_three_typed_selector_errors_preserve_fourth_valid_proposal(code):
    row=task(no_progress=0)
    value,command=commands(row,[MethodFactError(code) for _ in range(3)])
    for _ in range(3):
        with pytest.raises(MethodFactError,match=code):
            await command('record_method',{})
    assert value.fragment_failures==0
    assert row['result']['runtime']['native_progress']['corrections']==3
    assert await command('record_method',{})=={'state':'method_observed'}
    assert value.steps==4


@pytest.mark.asyncio
@pytest.mark.parametrize('discovery,error',[
    (False,MethodFactError('browser_method_element_ambiguous')),
    (True,MethodFactError('browser_method_permission_denied')),
    (True,ValueError('browser_method_element_ambiguous')),
])
async def test_other_stage_or_nonselector_error_still_stops_after_two(discovery,error):
    row=task(discovery,no_progress=0)
    value,command=commands(row,[error,error])
    for _ in range(2):
        with pytest.raises(ValueError):
            await command('record_method',{})
    assert value.fragment_failures==2 and value.total_corrections==2
    with pytest.raises(ValueError,match='native_one_correction_exhausted'):
        await command('record_method',{})
    assert row['result']['runtime']['native_progress']['stopped_reason']=='one_correction_exhausted'
    assert value.steps==2


@pytest.mark.asyncio
async def test_discovery_selector_errors_still_reach_eight_total_and_stop_observation():
    row=task(no_progress=0)
    value,command=commands(row,[MethodFactError('browser_method_element_ambiguous') for _ in range(8)])
    for _ in range(8):
        with pytest.raises(MethodFactError):
            await command('record_method',{})
    assert value.fragment_failures==0 and value.total_corrections==8
    assert row['result']['runtime']['native_progress']['corrections']==8
    value.descriptor=AsyncMock()
    value.verify_private_self=AsyncMock()
    value.document=AsyncMock(return_value='current-document')
    value.page=SimpleNamespace(evaluate=AsyncMock(return_value={'url':'https://board.test','text':'stable','fields':[]}))
    assert (await method('observe_progress',row)(value))['stop'] is True
    with pytest.raises(ValueError,match='native_task_progress_limit'):
        method('start_fragment',row)(controller())


@pytest.mark.asyncio
async def test_discovery_authorization_denial_still_uses_two_error_budget():
    row=task(no_progress=0)
    value,command=commands(row,[])
    value.authorize=AsyncMock(return_value={'allowed':False})
    assert await command('tool_authorize',{})=={'allowed':False}
    assert await command('tool_authorize',{})=={'allowed':False}
    with pytest.raises(ValueError,match='native_one_correction_exhausted'):
        await command('tool_authorize',{})
    assert value.fragment_failures==2 and value.total_corrections==2
