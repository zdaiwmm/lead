"""Synthetic release admission and shared-upgrade recovery contracts only."""
import hashlib
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    return (importlib.import_module('home_systemd_release'),
            importlib.import_module('unified_browser_upgrade'))


def test_shared_entry_uses_fixed_plan_and_keeps_old_goal(modules, monkeypatch):
    _, old = modules
    new = importlib.import_module('shared_executor_upgrade')
    calls = []
    monkeypatch.setattr(new, 'attested_engine', lambda: Path('/synthetic/unified_browser_upgrade.py'))
    monkeypatch.setattr(new.runpy, 'run_path', lambda path: {'main': lambda **kw: calls.append(kw)})
    new.main()
    assert calls == [{'shared': True}]
    assert old.GOAL == '0034_local_executor'
    assert old.validate_baseline(['0034_local_executor'], shared=True) == '0034_local_executor'
    for versions in (['0031_growth_runtime'], ['0033_unified_browser'], ['0035_shared_executor'], []):
        with pytest.raises(ValueError, match='starting schema'):
            old.validate_baseline(versions, shared=True)
    assert 'scripts/shared_executor_upgrade.py' in old.source_requirements(shared=True)
    assert 'scripts/unified_browser_upgrade.py' in old.source_requirements(shared=True)
    assert 'infra/migrations/sql/0035_shared_executor.sql' in old.source_requirements(shared=True)


def test_shared_bootstrap_attests_engine_before_loading_any_candidate_code(modules, tmp_path, monkeypatch):
    new=importlib.import_module('shared_executor_upgrade')
    sha='b'*40
    stage=tmp_path/('maintenance-'+sha)
    scripts=stage/'source/scripts'
    scripts.mkdir(parents=True)
    stage.chmod(0o700)
    entry=scripts/'shared_executor_upgrade.py'
    engine=scripts/'unified_browser_upgrade.py'
    entry.write_text('# fixture entry\n')
    engine.write_text('# fixture engine\n')
    monkeypatch.setattr(new,'__file__',str(entry))
    monkeypatch.setattr(new,'STORE',tmp_path)
    monkeypatch.setattr(new.os,'geteuid',lambda:0)
    real=Path.lstat
    def info(path):
        s=real(path)
        return SimpleNamespace(st_uid=0,st_mode=s.st_mode)
    monkeypatch.setattr(Path,'lstat',info)
    blobs={}
    for path in (entry,engine):
        data=path.read_bytes()
        blobs[path.name]=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    def git(argv,**kw):
        return blobs[argv[-1].split('/')[-1]] if 'rev-parse' in argv else ''
    monkeypatch.setattr(new.subprocess,'check_output',git)
    assert new.attested_engine()==engine
    engine.write_text('raise RuntimeError("must never execute")\n')
    loaded=[]
    monkeypatch.setattr(new.runpy,'run_path',lambda path:loaded.append(path))
    with pytest.raises(ValueError,match='source drift'):
        new.main()
    assert loaded==[]
    engine.chmod(0o666)
    with pytest.raises(ValueError,match='source drift'):
        new.attested_engine()


def test_shared_catalog_is_unfrozen_and_old_transport_checks_stay_strict(modules, monkeypatch):
    hook, _ = modules
    monkeypatch.setattr(hook, 'SHARED_EXECUTOR_CATALOG_MD5', 'UNFROZEN')
    with pytest.raises(hook.Refused, match='catalog.*not frozen'):
        hook.shared_executor_schema_query()
    monkeypatch.setattr(hook, 'SHARED_EXECUTOR_CATALOG_MD5', 'a' * 32)
    query = hook.shared_executor_schema_query()
    assert "ARRAY['0035_shared_executor']" in query
    assert "ARRAY['0034_local_executor']" in hook.local_executor_schema_query()
    assert "count(*)=22" in query and "convalidated" in query
    assert "pg_get_constraintdef" in query and "pg_get_indexdef" in query
    assert 'relforcerowsecurity' in query and 'polpermissive' in query
    assert 'prosecdef' in query and 'proconfig' in query and 'proowner' in query
    assert 'rolsuper' in query and 'rolbypassrls' in query
    assert 'has_column_privilege' in query and 'has_function_privilege' in query


def test_shared_exact_blobs_catalog_and_hold_all_required(modules, tmp_path, monkeypatch):
    hook, _ = modules
    monkeypatch.setattr(hook, 'TOOLS', tmp_path)
    (tmp_path / 'home_live_maintenance.py').write_text('# fixture')
    files = {'services/local-executor/src/runner.ts': 'a' * 40}
    monkeypatch.setattr(hook, 'SHARED_EXECUTOR_FILES', files)
    monkeypatch.setattr(hook, 'shared_executor_schema_query', lambda: 'SELECT true')
    pending = [False]
    monkeypatch.setattr(hook, 'shared_cutover_pending', lambda sha: pending[0])
    package = []
    monkeypatch.setattr(hook, 'verify_local_executor_package', package.append)
    blob = ['a' * 40]
    def run(*args, **kw):
        if args[0] == 'git':
            return blob[0]
        return 't'
    monkeypatch.setattr(hook, 'run', run)
    extra = set(files) | {'infra/unreviewed.conf'}
    # No new hold leaves paths for the old exact-blob gate, which rejects new
    # worker bytes. It must not break an original 0034 upgrade's old bytes.
    assert hook.reviewed_shared_executor('/mirror', 'b' * 40, extra) == set()
    assert package == []
    pending[0] = True
    assert hook.reviewed_shared_executor('/mirror', 'b' * 40, extra) == set(files)
    assert package == ['b' * 40]
    blob[0] = 'c' * 40
    with pytest.raises(hook.Refused, match='compatibility review'):
        hook.reviewed_shared_executor('/mirror', 'b' * 40, extra)


def test_shared_finish_never_uses_old_marker_or_releases_before_readback(modules, tmp_path, monkeypatch):
    _, helper = modules
    sha = 'b' * 40
    pending = tmp_path / 'shared-executor-cutover.pending'
    pending.write_text(sha + '\n')
    calls = []
    monkeypatch.setattr(helper, 'resume_facts', lambda *a: {'held': 1, 'other': 0, 'running': 0, 'ingress': 'inactive'})
    def run(*args, **kw):
        calls.append(tuple(map(str, args)))
        raise ValueError('independent readback unavailable')
    monkeypatch.setattr(helper, 'run', run)
    verifier = {'verify': lambda *a, **kw: None, 'verify_executors': lambda *a: None,
                'shared_executor_schema_query': lambda: 'shared-query'}
    queries = []
    with pytest.raises(ValueError, match='independent readback'):
        helper.finish_cutover(tmp_path, sha, verifier, {'sql': lambda q: queries.append(q) or 't'},
                              lambda p, d: p.write_bytes(d), shared=True)
    assert queries == ['shared-query'] and pending.exists()
    assert not (tmp_path / 'unified-browser-resumption.json').exists()
    assert not (tmp_path / 'shared-executor-cutover.complete').exists()
    assert not any('restore' in call or 'start' in call for call in calls)


def test_shared_journal_fences_preflight_and_dispatches_shared_finalizer(modules, monkeypatch):
    hook, _ = modules
    sha = 'b' * 40
    monkeypatch.setattr(hook, 'growth_cutover_pending', lambda s: False)
    monkeypatch.setattr(hook, 'unified_cutover_pending', lambda s: False)
    monkeypatch.setattr(hook, 'shared_cutover_pending', lambda s: True)
    assert hook.cutover_kind(sha) == 'shared-executor'
    monkeypatch.setattr(hook, 'current', lambda: sha)
    calls = []
    monkeypatch.setattr(hook, 'complete_shared_cutover', calls.append)
    hook.finalize_cutover(sha)
    assert calls == [sha]
    monkeypatch.setattr(hook, 'unified_cutover_pending', lambda s: True)
    with pytest.raises(hook.Refused, match='ambiguous'):
        hook.cutover_kind(sha)


def test_shared_finalizer_attests_both_helper_and_engine_then_uses_original_journal(modules, tmp_path, monkeypatch):
    hook,_=modules
    sha,old='b'*40,'a'*40
    stage=tmp_path/('maintenance-'+sha)
    monkeypatch.setattr(hook,'STORE',tmp_path)
    pending=[True]
    monkeypatch.setattr(hook,'shared_cutover_pending',lambda s:pending[0])
    files={'scripts/shared_executor_upgrade.py':'c'*40,'scripts/unified_browser_upgrade.py':'d'*40}
    monkeypatch.setattr(hook,'SHARED_EXECUTOR_FILES',files)
    reads=[]
    def read(path,**kw):
        reads.append(path.name)
        return json.dumps({'sha':sha,'old':old}).encode()
    monkeypatch.setattr(hook,'executor_host_file',read)
    monkeypatch.setattr(hook,'provision_executor',lambda *a:None)
    monkeypatch.setattr(hook,'register_executors',lambda *a:None)
    calls=[]
    def run(*args,**kw):
        calls.append(tuple(map(str,args)))
        if args[0]=='git':
            return files['scripts/'+args[-1].name]
        pending[0]=False
        return ''
    monkeypatch.setattr(hook,'run',run)
    hook.complete_shared_cutover(sha)
    assert {'shared-executor-migration.json','shared_executor_upgrade.py','unified_browser_upgrade.py'}<=set(reads)
    assert calls[-1][4:]==('finish','--sha',sha,'--old',old)
    assert calls[-1][3]==str(stage/'source/scripts/shared_executor_upgrade.py')


def test_shared_inventory_pins_the_new_drill_transport_without_widening_old_growth(modules):
    hook, _ = modules
    name = 'scripts/growth_runtime_upgrade.py'
    data = (Path(__file__).resolve().parents[1] / name).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    assert hook.SHARED_EXECUTOR_FILES.get(name) == blob
    assert hook.GROWTH_RUNTIME_FILES[name] == '5a98672c797361272a1a22148ba7b3539ca09aba'
    assert blob != hook.GROWTH_RUNTIME_FILES[name]


@pytest.mark.parametrize('held,matching,schema_ready,package_ready,accepted', [
    (True, True, True, True, True),
    (True, False, True, True, False),
    (False, True, True, True, False),
    (True, True, False, True, False),
    (True, True, True, False, False),
])
def test_shared_drill_helper_admission_preserves_hold_blob_catalog_and_package_guards(
        modules, tmp_path, monkeypatch, held, matching, schema_ready, package_ready, accepted):
    hook, _ = modules
    name = 'scripts/growth_runtime_upgrade.py'
    data = (Path(__file__).resolve().parents[1] / name).read_bytes()
    blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
    monkeypatch.setattr(hook, 'TOOLS', tmp_path)
    (tmp_path / 'home_live_maintenance.py').write_text('# synthetic verifier')
    monkeypatch.setattr(hook, 'shared_cutover_pending', lambda sha: held)
    monkeypatch.setattr(hook, 'growth_cutover_pending', lambda sha: False)
    monkeypatch.setattr(hook, 'shared_executor_schema_query', lambda: 'shared-catalog-query')
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == 'git':
            return blob if matching else 'f' * 40
        return 't' if schema_ready else 'f'

    def package(sha):
        calls.append(('package', sha))
        if not package_ready:
            raise hook.Refused('synthetic package rejected')

    monkeypatch.setattr(hook, 'run', run)
    monkeypatch.setattr(hook, 'verify_local_executor_package', package)

    def admit():
        extra = {name}
        extra -= hook.reviewed_shared_executor('/synthetic/mirror', 'b' * 40, extra)
        # The normal admission order consumes shared paths before the old gate.
        extra -= hook.reviewed_growth_runtime('/synthetic/mirror', 'b' * 40, extra)
        assert not extra

    if accepted:
        admit()
        assert calls[-1] == ('package', 'b' * 40)
    else:
        with pytest.raises(hook.Refused):
            admit()
        if not (held and matching and schema_ready):
            assert not any(call[0] == 'package' for call in calls)
