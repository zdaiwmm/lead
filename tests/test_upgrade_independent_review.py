"""Independent finalizer faults: only local mocks, never production commands."""
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def helper():
    path=Path(__file__).parents[1]/'scripts/unified_browser_upgrade.py'
    spec=importlib.util.spec_from_file_location('independent_upgrade_review',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('failure',['app','executors','schema','installed'])
def test_every_independent_check_precedes_actual_restore(helper,tmp_path,monkeypatch,failure):
    sha='b'*40
    pending=tmp_path/'unified-browser-cutover.pending'
    pending.write_text(sha+'\n')
    facts={'held':1,'other':0,'running':0,'ingress':'inactive'}
    calls=[]
    monkeypatch.setattr(helper,'resume_facts',lambda *args:dict(facts))
    def check(name):
        calls.append(name)
        if name==failure:
            raise ValueError('injected readback failure')
    def run(*args,**kwargs):
        args=tuple(map(str,args))
        calls.append(args)
        if args[0].endswith('home_systemd_release.py'):
            check('installed')
        elif 'restore' in args:
            facts.update(held=0,running=1)
        elif 'start' in args:
            facts['ingress']='active'
        return ''
    def sql(query):
        check('schema')
        return 't'
    monkeypatch.setattr(helper,'run',run)
    verifier={'verify':lambda *args,**kwargs:check('app'),
              'verify_executors':lambda *args:check('executors'),
              'unified_schema_query':lambda:'query'}
    with pytest.raises(ValueError,match='injected'):
        helper.finish_cutover(tmp_path,sha,verifier,{'sql':sql},lambda path,data:path.write_bytes(data))
    assert facts=={'held':1,'other':0,'running':0,'ingress':'inactive'}
    assert pending.exists()
    assert not (tmp_path/'unified-browser-resumption.json').exists()
    assert not any(isinstance(call,tuple) and ('restore' in call or 'start' in call) for call in calls)


@pytest.mark.parametrize('failure',['start-response','complete-journal'])
def test_late_failure_reconciles_open_state_without_rehold_or_second_restore(helper,tmp_path,monkeypatch,failure):
    sha='b'*40
    pending=tmp_path/'unified-browser-cutover.pending'
    pending.write_text(sha+'\n')
    facts={'held':1,'other':0,'running':0,'ingress':'inactive'}
    injected=False
    calls=[]
    monkeypatch.setattr(helper,'resume_facts',lambda *args:dict(facts))
    def run(*args,**kwargs):
        nonlocal injected
        args=tuple(map(str,args))
        calls.append(args)
        if 'restore' in args:
            facts.update(held=0,running=1)
        elif 'start' in args:
            facts['ingress']='active'
            if failure=='start-response' and not injected:
                injected=True
                raise ValueError('response lost after start succeeded')
        return ''
    def write(path,data):
        nonlocal injected
        if path.name=='unified-browser-cutover.complete' and failure=='complete-journal' and not injected:
            injected=True
            raise ValueError('completion journal unavailable')
        path.write_bytes(data)
    monkeypatch.setattr(helper,'run',run)
    verifier={'verify':lambda *args,**kwargs:None,'verify_executors':lambda *args:None,
              'unified_schema_query':lambda:'query'}
    with pytest.raises(ValueError):
        helper.finish_cutover(tmp_path,sha,verifier,{'sql':lambda query:'t'},write)
    record=json.loads((tmp_path/'unified-browser-resumption.json').read_text())
    assert facts=={'held':0,'other':0,'running':1,'ingress':'active'}
    assert record['state']==('reconciliation-required' if failure=='start-response' else 'ingress-opened')
    assert record['observed']==facts
    assert pending.exists()
    helper.finish_cutover(tmp_path,sha,verifier,{'sql':lambda query:'t'},write)
    assert not pending.exists()
    assert sum('restore' in call for call in calls)==1
    assert not any('hold' in call for call in calls)
