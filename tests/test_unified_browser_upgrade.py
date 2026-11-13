"""No production access: release proof, hold and data-preservation contracts."""
import hashlib
import importlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def upgrade(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'scripts'))
    return importlib.import_module('unified_browser_upgrade')


@pytest.fixture
def approval(upgrade,tmp_path,monkeypatch):
    sha='b'*40
    receipt={'deployment':11,'sha':sha,'ci_run':22,'ci_attempt':1,'approval_run':33}
    archive=tmp_path/'home-approval.zip'
    with zipfile.ZipFile(archive,'w') as output:
        output.writestr('lead-home-request.json',json.dumps(receipt))
    request={'id':11,'sha':sha,'environment':'home-production','task':'lead-home-release',
        'created_at':datetime.now(timezone.utc).isoformat(),
        'payload':{'schema':1,'repository':'zdaiwmm/lead','ci_run':22,'ci_attempt':1,'approval_run':33}}
    workflow={'path':'.github/workflows/home-release.yml','event':'workflow_dispatch','head_branch':'main',
        'run_attempt':1,'display_title':f'Home release {sha}','status':'completed','conclusion':'success'}
    artifact={'name':'lead-home-request','expired':False,'digest':'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest(),'size_in_bytes':archive.stat().st_size}
    data={'deployments/11':request,'actions/runs/33':workflow,
        'actions/runs/33/artifacts?name=lead-home-request&per_page=2':{'total_count':1,'artifacts':[artifact]},
        'deployments?environment=home-production&task=lead-home-release&per_page=1':[request]}
    policy=importlib.import_module('request_home_release')
    checked=[]
    def verified(revision,*,get):
        checked.append(revision)
        return {'id':22,'run_attempt':1}
    monkeypatch.setattr(policy,'verified_run',verified)
    return sha,archive,data,checked


def test_exact_approved_application_receipt(upgrade,approval):
    sha,archive,data,checked=approval
    result=upgrade.verify_approval(sha,archive,get=data.__getitem__)
    assert result['deployment']==11 and checked==[sha]


@pytest.mark.parametrize('failure',['sha','environment','workflow','digest','superseded','expired','attempt'])
def test_reject_wrong_or_unapproved_release(upgrade,approval,failure):
    sha,archive,data,checked=approval
    if failure=='sha':
        data['deployments/11']['sha']='a'*40
    elif failure=='environment':
        data['deployments/11']['environment']='staging'
    elif failure=='workflow':
        data['actions/runs/33']['conclusion']='failure'
    elif failure=='digest':
        data['actions/runs/33/artifacts?name=lead-home-request&per_page=2']['artifacts'][0]['digest']='sha256:'+'0'*64
    elif failure=='superseded':
        data['deployments?environment=home-production&task=lead-home-release&per_page=1']=[{'id':99}]
    elif failure=='expired':
        data['deployments/11']['created_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    elif failure=='attempt':
        data['actions/runs/33']['run_attempt']=2
    with pytest.raises(ValueError):
        upgrade.verify_approval(sha,archive,get=data.__getitem__)


def test_expired_original_request_can_only_resume_existing_journal(upgrade,approval):
    sha,archive,data,_=approval
    data['deployments/11']['created_at']=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
    assert upgrade.verify_approval(sha,archive,get=data.__getitem__,started=True)['sha']==sha


def test_original_action_controls_and_routes_are_not_excluded(upgrade):
    assert {'actions','action_attempts','agents','tasks','account_operating_intents','profile_execution_routes'}<=set(upgrade.PRESERVED_TABLES)
    before={'actions':[1,'prepared-hash'],'agents':[1,'control-v1']}
    upgrade.assert_unchanged(before,dict(before))
    with pytest.raises(ValueError,match='Original accounts'):
        upgrade.assert_unchanged(before,{**before,'actions':[1,'unknown-hash']})


def test_codex_upgrade_only_admits_reviewed_additive_baselines(upgrade):
    assert upgrade.GOAL == '0034_local_executor'
    assert upgrade.validate_baseline(['0031_growth_runtime']) == '0031_growth_runtime'
    assert upgrade.validate_baseline(['0033_unified_browser']) == '0033_unified_browser'
    for versions in ([], ['0030_fault_isolation'], ['0034_local_executor'], ['unknown'],
                     ['0031_growth_runtime', '0033_unified_browser']):
        with pytest.raises(ValueError, match='starting schema'):
            upgrade.validate_baseline(versions)


def test_finish_retains_hold_until_independent_readback(upgrade,tmp_path,monkeypatch):
    sha='b'*40
    pending=tmp_path/'unified-browser-cutover.pending'
    pending.write_text(sha+'\n')
    calls=[]
    def run(*args,**kwargs):
        calls.append(tuple(map(str,args)))
        if args[:2]==('systemctl','show'):
            return 'inactive'
        if str(args[0]).endswith('home_systemd_release.py'):
            raise ValueError('independent readback unavailable')
        return ''
    monkeypatch.setattr(upgrade,'run',run)
    monkeypatch.setattr(upgrade,'resume_facts',lambda *a:{'held':1,'other':0,'running':0,'ingress':'inactive'})
    verifier={'verify':lambda *a,**kw:calls.append(('app',)),
        'verify_executors':lambda *a,**kw:calls.append(('executors',)),
        'unified_schema_query':lambda:'schema-query'}
    with pytest.raises(ValueError,match='independent readback'):
        upgrade.finish_cutover(tmp_path,sha,verifier,{'sql':lambda q:'t'},lambda p,d:p.write_bytes(d))
    assert pending.exists() and not (tmp_path/'unified-browser-cutover.complete').exists()
    assert ('app',) in calls and ('executors',) in calls

    assert not any('restore' in call or 'start' in call for call in calls)
    assert not (tmp_path/'unified-browser-resumption.json').exists()


@pytest.mark.parametrize('failure',['restore-response','start'])
def test_partial_resume_records_truth_and_reconciles_without_rehold(upgrade,tmp_path,monkeypatch,failure):
    sha='b'*40
    pending=tmp_path/'unified-browser-cutover.pending'
    pending.write_text(sha+'\n')
    facts={'held':1,'other':0,'running':0,'ingress':'inactive'}
    calls=[]
    fail=[True]
    def run(*args,**kwargs):
        call=tuple(map(str,args))
        calls.append(call)
        if 'restore' in call:
            facts.update(held=0,running=1)
            if fail[0] and failure=='restore-response':
                fail[0]=False
                raise ValueError('lost restore response')
        if 'start' in call:
            if fail[0] and failure=='start':
                fail[0]=False
                raise ValueError('start failed')
            facts['ingress']='active'
        return ''
    monkeypatch.setattr(upgrade,'run',run)
    monkeypatch.setattr(upgrade,'resume_facts',lambda *a:dict(facts))
    verifier={'verify':lambda *a,**kw:None,'verify_executors':lambda *a:None,
              'unified_schema_query':lambda:'schema'}
    def write(p,d):
        return p.write_bytes(d)
    with pytest.raises(ValueError):
        upgrade.finish_cutover(tmp_path,sha,verifier,{'sql':lambda q:'t'},write)
    record=json.loads((tmp_path/'unified-browser-resumption.json').read_text())
    assert record['state']=='reconciliation-required'
    assert record['observed']=={'held':0,'other':0,'running':1,'ingress':'inactive'}
    assert pending.exists()
    upgrade.finish_cutover(tmp_path,sha,verifier,{'sql':lambda q:'t'},write)
    assert not pending.exists()
    assert sum('restore' in c for c in calls)==1
    assert not any('hold' in c for c in calls)
    assert calls.index((str(upgrade.TOOLS/'home_systemd_release.py'),'verify-cutover',sha)) < next(i for i,c in enumerate(calls) if 'restore' in c)
