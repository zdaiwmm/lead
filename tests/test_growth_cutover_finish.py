"""Resume the same maintenance journal across restored scheduling and ingress."""
import importlib.util
from pathlib import Path

import pytest


def helper():
    path = Path(__file__).parents[1] / 'scripts/growth_runtime_upgrade.py'
    spec = importlib.util.spec_from_file_location('growth_cutover_finish', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('interrupt_after', ['restore', 'start', 'verify', None])
def test_finish_resumes_same_journal_after_scheduling_or_ingress_restore(tmp_path, monkeypatch, interrupt_after):
    upgrade = helper()
    sha = 'b' * 40
    pending = tmp_path / 'growth-cutover.pending'
    complete = tmp_path / 'growth-cutover.complete'
    pending.write_text(sha + '\n')
    state = {'ingress': 'inactive', 'held': True, 'business_busy': False, 'interrupted': False}
    resumes = []

    def interrupt(point):
        if interrupt_after == point and not state['interrupted']:
            state['interrupted'] = True
            raise InterruptedError('lost operator after ' + point)

    def verify(revision, *, fenced=False, require_idle=True):
        assert revision == sha
        assert state['ingress'] == ('inactive' if fenced else 'active')
        assert not (fenced and require_idle and state['business_busy']), 'restored business must not block readback'

    def run(*args, **kwargs):
        if args[:3] == ('systemctl', 'show', 'lead-home-ingress.socket'):
            return state['ingress']
        if args[:2] == (upgrade.TOOLS / 'home_live_maintenance.py', 'restore'):
            assert args[2] == sha
            if state['held']:
                state.update(held=False, business_busy=True)
                resumes.append(sha)
            interrupt('restore')
        elif args[:2] == ('systemctl', 'start'):
            assert args[2:] == ('lead-home-live.path', 'lead-home-ingress.socket')
            state['ingress'] = 'active'
            interrupt('start')
        elif args[:2] == (upgrade.TOOLS / 'home_systemd_release.py', 'verify'):
            verify(args[2])
            interrupt('verify')
        else:
            pytest.fail('Unexpected operation: ' + repr(args))
        return ''

    monkeypatch.setattr(upgrade, 'run', run)
    verifier = {'verify': verify, 'verify_executors': lambda revision: [{'revision': revision}],
                'growth_schema_query': lambda: 'SELECT schema_attestation'}
    site = {'sql': lambda query: 't'}
    if interrupt_after:
        with pytest.raises(InterruptedError, match='lost operator'):
            upgrade.finish_cutover(tmp_path, sha, verifier, site)
        assert pending.read_text() == sha + '\n' and not complete.exists()
    upgrade.finish_cutover(tmp_path, sha, verifier, site)
    assert complete.read_text() == sha + '\n' and not pending.exists()
    assert resumes == [sha]
    assert state['ingress'] == 'active' and not state['held']


@pytest.mark.parametrize('failed_proof', ['application', 'executors', 'schema', 'ingress'])
def test_finish_never_restores_scheduling_before_exact_running_proofs(tmp_path, monkeypatch, failed_proof):
    upgrade = helper()
    sha = 'b' * 40
    (tmp_path / 'growth-cutover.pending').write_text(sha + '\n')

    def verify(revision, **kwargs):
        if failed_proof == 'application':
            raise ValueError('application mismatch')

    def executors(revision):
        if failed_proof == 'executors':
            raise ValueError('executor mismatch')

    def run(*args, **kwargs):
        if args[:3] == ('systemctl', 'show', 'lead-home-ingress.socket'):
            return 'activating' if failed_proof == 'ingress' else 'inactive'
        pytest.fail('Invalid proofs must not restore scheduling or ingress')

    monkeypatch.setattr(upgrade, 'run', run)
    verifier = {'verify': verify, 'verify_executors': executors, 'growth_schema_query': lambda: 'SELECT proof'}
    with pytest.raises(ValueError):
        upgrade.finish_cutover(tmp_path, sha, verifier, {'sql': lambda query: 'f' if failed_proof == 'schema' else 't'})
    assert (tmp_path / 'growth-cutover.pending').exists()
    assert not (tmp_path / 'growth-cutover.complete').exists()
