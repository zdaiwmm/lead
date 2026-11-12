"""The verification harness must never fall back to user state."""
import importlib.util
from pathlib import Path

import pytest


def load_harness():
    path = Path(__file__).parents[1] / 'scripts/verify_growth_runtime.py'
    spec = importlib.util.spec_from_file_location('growth_verification', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reject_default_user_state(tmp_path):
    harness = load_harness()
    with pytest.raises(ValueError, match='isolated'):
        harness.validate_state(Path.cwd() / '.state')
    assert harness.validate_state(tmp_path / 'growth-test') == (tmp_path / 'growth-test').resolve()


def test_manifest_guard_requires_explicit_test_endpoints(tmp_path):
    harness = load_harness()
    with pytest.raises(ValueError, match='manifest'):
        harness.environment(tmp_path)


def test_stop_refuses_unrelated_temporal_process_and_uses_only_owned_database(tmp_path,monkeypatch):
    from types import SimpleNamespace
    harness=load_harness()
    calls=[]
    monkeypatch.setattr(harness,'read_manifest',lambda state:{'temporal_pid':1234})
    monkeypatch.setattr(harness,'pg_bin',lambda:tmp_path/'tools')
    def run(args,**kwargs):
        calls.append(args)
        if args[0]=='ps':
            return SimpleNamespace(returncode=0,stdout='unrelated-existing-queue --database /somewhere-else')
        assert args==[str(tmp_path/'tools/pg_ctl'),'-D',str(tmp_path/'pg'),'status']
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(harness.subprocess,'run',run)
    monkeypatch.setattr(harness.os,'kill',lambda *args:pytest.fail('Unrelated process must remain running'))
    harness.stop(tmp_path)
    assert len(calls)==2


def test_manifest_rejects_shared_endpoint_and_user_profiles(tmp_path):
    import json
    harness=load_harness()
    with pytest.raises(ValueError,match='isolated'):
        harness.validate_state(tmp_path/'profiles'/'production')
    (tmp_path/'manifest.json').write_text(json.dumps({'root':str(harness.ROOT),'state':str(tmp_path.resolve()),
        'pg_port':55432,'temporal_port':56859,'temporal_ui_port':56860}))
    with pytest.raises(ValueError,match='endpoints'):
        harness.read_manifest(tmp_path)
