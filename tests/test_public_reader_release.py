"""Fixed-purpose public reader provisioning: synthetic files and service replies only."""

import importlib.util
import io
import json
import tarfile
from types import SimpleNamespace
from pathlib import Path

import pytest


@pytest.fixture
def hook(monkeypatch, tmp_path):
    source = Path(__file__).resolve().parents[1] / 'scripts/home_systemd_release.py'
    spec = importlib.util.spec_from_file_location('public_reader_release_hook', source)
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)
    monkeypatch.setattr(h, 'STORE', tmp_path / 'store')
    h.STORE.mkdir()
    return h


def test_public_reader_has_formal_entrypoints(hook):
    for name in ('public_reader_plan', 'provision_public_reader', 'verify_public_reader',
                 'verify_public_reader_prepared', 'rollback_public_reader', 'reviewed_public_reader_files'):
        assert callable(getattr(hook, name, None)), f'missing fixed-purpose operation: {name}'


def test_public_reader_dropin_only_appends_fixed_directory(hook):
    assert hasattr(hook, 'public_reader_dropin'), 'public reader mount contract is missing'
    text = hook.public_reader_dropin().decode()
    assert text == ('[Service]\n'
                    'BindReadOnlyPaths=/run/lead-public-reader:/run/lead-public-reader\n'
                    'Environment=LEAD_PUBLIC_READER_SOCKET=/run/lead-public-reader/reader.sock\n')
    assert 'BindReadOnlyPaths=\n' not in text
    assert 'PrivateNetwork=' not in text and 'JoinsNamespaceOf=' not in text


def test_public_reader_rejects_template_parameter_injection(hook):
    assert hasattr(hook, 'public_reader_render'), 'public reader template validation is missing'
    template = '@IMAGE@ @PYTHON@ @UID@ @GID@ @SHA@'
    output = hook.public_reader_render(template, 'b' * 40, 995, 983)
    assert str(hook.STORE / ('b' * 40)) in output
    assert '@' not in output
    with pytest.raises(hook.Refused):
        hook.public_reader_render('@IMAGE@ @COMMAND@', 'b' * 40, 995, 983)
    with pytest.raises(hook.Refused):
        hook.public_reader_render(template, 'b' * 40, 0, 983)


def test_public_reader_health_rejects_revision_or_protocol_drift(hook):
    assert hasattr(hook, 'public_reader_health_result'), 'public reader health proof is missing'
    good = {'v': 1, 'ok': True, 'protocol': 1, 'revision': 'b' * 40}
    assert hook.public_reader_health_result(json.dumps(good).encode() + b'\n', 'b' * 40) == good
    for changed in ({'revision': 'a' * 40}, {'protocol': 2}, {'ok': False}, {'extra': True}):
        with pytest.raises(hook.Refused):
            hook.public_reader_health_result(json.dumps({**good, **changed}).encode() + b'\n', 'b' * 40)


def test_public_reader_unknown_journal_cannot_repeat_install(hook, monkeypatch):
    assert hasattr(hook, 'public_reader_existing'), 'unknown-result recovery guard is missing'
    monkeypatch.setattr(hook, 'verify_public_reader', lambda *_args, **_kwargs: (_ for _ in ()).throw(hook.Refused('not ready')))
    with pytest.raises(hook.Refused, match='inspect.*journal'):
        hook.public_reader_existing({'state': 'installing', 'sha': 'b' * 40}, 'b' * 40)


def test_connector_compatibility_is_exact_not_directory_wide(hook, monkeypatch):
    assert hasattr(hook, 'PUBLIC_READER_CONNECTOR_FILES'), 'installed connector compatibility proof is missing'
    assert set(hook.PUBLIC_READER_CONNECTOR_FILES) == {
        'services/local-executor/src/runner.ts', 'services/local-executor/test/server-worker.test.ts'}
    assert all(len(blob) == 40 for blob in hook.PUBLIC_READER_CONNECTOR_FILES.values())


def test_consumer_dropin_sorts_after_existing_executor_reset(hook):
    assert all(path.name > 'zzzz-lead-executor.conf' for path in hook.public_reader_paths()[2:])


def test_health_requires_integer_protocol_not_json_boolean(hook):
    with pytest.raises(hook.Refused):
        hook.public_reader_health_result(json.dumps(
            {'v': True, 'ok': True, 'protocol': True, 'revision': 'b' * 40}).encode() + b'\n', 'b' * 40)


def test_public_reader_formal_cli_dispatches_without_implicit_actions(hook, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(hook, 'verify_public_reader', lambda sha: calls.append(sha) or {'ok': True})
    monkeypatch.setattr('sys.argv', ['hook', 'verify-public-reader', 'b' * 40])
    hook.main()
    assert calls == ['b' * 40]
    assert json.loads(capsys.readouterr().out) == {'ok': True}


def test_rollback_restores_consumer_bindings_before_old_app_restart(hook, monkeypatch):
    calls = []
    monkeypatch.setattr(hook, 'select', lambda sha: calls.append(('select', sha)))
    def restart(sha):
        calls.append(('restart', sha))
        if sha == 'b' * 40:
            raise hook.Refused('injected candidate failure')
    monkeypatch.setattr(hook, 'restart_and_verify', restart)
    monkeypatch.setattr(hook, 'public_reader_restore_consumers', lambda sha, old: calls.append(('restore', old)) or True)
    monkeypatch.setattr(hook, 'public_reader_stop_restored', lambda sha: calls.append(('stop-reader', sha)))
    with pytest.raises(hook.Refused):
        hook.replace_release('a' * 40, 'b' * 40)
    assert calls == [('select', 'b' * 40), ('restart', 'b' * 40), ('restore', 'a' * 40),
                     ('select', 'a' * 40), ('restart', 'a' * 40), ('stop-reader', 'b' * 40)]


def test_connector_proof_checks_whole_tree_and_modes(hook, tmp_path):
    assert hasattr(hook, 'public_reader_connector_tree'), 'whole installed package comparison is missing'
    root = tmp_path / 'connector'
    root.mkdir(mode=0o755)
    (root / 'dist').mkdir(mode=0o755)
    (root / 'dist/runner.js').write_bytes(b'known')
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w:gz') as archive:
        entry = tarfile.TarInfo('package/dist/runner.js')
        entry.size, entry.mode = 5, 0o644
        archive.addfile(entry, io.BytesIO(b'known'))
    hook.public_reader_connector_tree(root, raw.getvalue())
    (root / 'dist/extra.js').write_text('unreviewed')
    with pytest.raises(hook.Refused):
        hook.public_reader_connector_tree(root, raw.getvalue())
    (root / 'dist/extra.js').unlink()
    (root / 'dist/runner.js').chmod(0o666)
    with pytest.raises(hook.Refused):
        hook.public_reader_connector_tree(root, raw.getvalue())


@pytest.mark.parametrize('kind', ('symlink', 'hardlink', 'parent', 'duplicate'))
def test_connector_proof_refuses_unsafe_package_entries(hook, tmp_path, kind):
    assert hasattr(hook, 'public_reader_connector_tree'), 'whole installed package comparison is missing'
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w:gz') as archive:
        entry = tarfile.TarInfo('package/../escape' if kind == 'parent' else 'package/file')
        if kind in ('symlink', 'hardlink'):
            entry.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
            entry.linkname = '/tmp/other'
        entry.mode = 0o644
        archive.addfile(entry)
        if kind == 'duplicate':
            archive.addfile(entry)
    with pytest.raises(hook.Refused):
        hook.public_reader_connector_tree(tmp_path, raw.getvalue())


@pytest.fixture
def provision(hook, tmp_path, monkeypatch):
    h = hook
    sha, old = 'b' * 40, 'a' * 40
    monkeypatch.setattr(h.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(h.os, 'chown', lambda *_args: None)
    monkeypatch.setattr(h, 'PUBLIC_READER_SYSTEMD', tmp_path / 'systemd')
    monkeypatch.setattr(h, 'PUBLIC_READER_ROOT', tmp_path / 'reader-data/root')
    monkeypatch.setattr(h, 'PUBLIC_READER_RUNTIME', tmp_path / 'runtime')
    monkeypatch.setattr(h, 'PUBLIC_READER_PLAN', tmp_path / 'config/public-reader-provision.json')
    h.PUBLIC_READER_SYSTEMD.mkdir()
    h.PUBLIC_READER_PLAN.parent.mkdir()
    def directory(path, *, create=False, private=False):
        if create:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert path.is_dir() and not path.is_symlink()
    monkeypatch.setattr(h, 'executor_directory', directory)
    monkeypatch.setattr(h, 'executor_config', lambda p: p.read_bytes())
    monkeypatch.setattr(h, 'executor_host_file', lambda p, **_kwargs: p.read_bytes())
    monkeypatch.setattr(h, 'current', lambda: old)
    snapshots = {role: {'MainPID': str(100 + index), 'InvocationID': role,
                       'uid': 995, 'gid': 983, 'route': None}
                 for index, role in enumerate(h.PUBLIC_READER_CONSUMERS)}
    monkeypatch.setattr(h, 'public_reader_consumer', lambda role: dict(snapshots[role]))
    def prop(service, name):
        if service in snapshots:
            return snapshots[service][name]
        assert name == 'LoadState'
        return 'not-found'
    monkeypatch.setattr(h, 'property_of', prop)
    release = h.STORE / sha
    folder = release / 'services/public_reader/systemd'
    folder.mkdir(parents=True)
    for service in (h.PUBLIC_READER_SERVICE, h.PUBLIC_READER_SOCKET):
        (folder / (service + '.in')).write_text('@IMAGE@ @PYTHON@ @UID@ @GID@ @SHA@\n')
    monkeypatch.setattr(h, 'public_reader_sources', lambda _sha: release)
    monkeypatch.setattr(h, 'public_reader_empty_root', lambda path: None)
    commands, checks = [], []
    monkeypatch.setattr(h, 'run', lambda *args, **_kwargs: commands.append(args) or '')
    monkeypatch.setattr(h, 'verify_public_reader', lambda *args, **kwargs: checks.append((args, kwargs)) or {'ready': True})
    plan = h.public_reader_plan(sha)
    h.PUBLIC_READER_PLAN.write_text(json.dumps(plan))
    h.PUBLIC_READER_PLAN.chmod(0o600)
    return SimpleNamespace(h=h, sha=sha, old=old, plan=plan, commands=commands,
                           checks=checks, snapshots=snapshots)


def test_first_provision_only_starts_new_reader_and_preserves_original_processes(provision):
    p = provision
    assert p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN) == {'ready': True}
    assert p.commands == [('systemctl', 'daemon-reload'),
                          ('systemctl', 'enable', '--now', p.h.PUBLIC_READER_SOCKET),
                          ('systemctl', 'start', p.h.PUBLIC_READER_SERVICE)]
    record = p.h.public_reader_load(p.sha)
    assert record['state'] == 'ready' and record['previous'] == p.plan
    assert p.h.PUBLIC_READER_ROOT.is_dir()
    for path in p.h.public_reader_paths()[2:]:
        assert path.read_bytes() == p.h.public_reader_dropin()


def test_successful_provision_retry_only_reads_existing_result(provision):
    p = provision
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    p.commands.clear()
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    assert p.commands == []


def test_unknown_start_never_repeats_or_automatically_stops_application(provision, monkeypatch):
    p = provision
    def fail(*args, **_kwargs):
        p.commands.append(args)
        if args == ('systemctl', 'start', p.h.PUBLIC_READER_SERVICE):
            raise TimeoutError('synthetic lost reply')
        return ''
    monkeypatch.setattr(p.h, 'run', fail)
    with pytest.raises(TimeoutError):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    assert p.h.public_reader_load(p.sha)['state'] == 'start-requested'
    p.commands.clear()
    monkeypatch.setattr(p.h, 'verify_public_reader', lambda *_args, **_kwargs: (_ for _ in ()).throw(p.h.Refused('unknown')))
    with pytest.raises(p.h.Refused, match='inspect original journal'):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    assert p.commands == []


@pytest.mark.parametrize('field', ('client_uid', 'expected_app', 'consumers'))
def test_provision_rejects_stale_preimage_without_target_write(provision, field):
    p = provision
    p.plan[field] = {} if field == 'consumers' else 'changed'
    p.h.PUBLIC_READER_PLAN.write_text(json.dumps(p.plan))
    with pytest.raises(p.h.Refused, match='baseline drift'):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    assert not p.h.public_reader_journal(p.sha).exists()
    assert not any(path.exists() for path in p.h.public_reader_paths())
    assert not p.commands


def test_provision_rejects_public_plan_or_nonstandard_target(provision):
    p = provision
    p.h.PUBLIC_READER_PLAN.chmod(0o644)
    with pytest.raises(p.h.Refused, match='not private'):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    with pytest.raises(p.h.Refused, match='nonstandard'):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN.with_name('other.json'))


def test_plan_refuses_distinct_consumer_uid_and_existing_service(provision):
    p = provision
    p.snapshots['worker']['uid'] = 996
    with pytest.raises(p.h.Refused, match='one actual UID'):
        p.h.public_reader_plan(p.sha)


def test_existing_foreign_dropin_is_not_overwritten(provision):
    p = provision
    path = p.h.public_reader_paths()[2]
    path.parent.mkdir()
    path.write_bytes(b'foreign task')
    with pytest.raises(p.h.Refused, match='already exist'):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    assert path.read_bytes() == b'foreign task'


def test_empty_isolation_root_must_be_empty_and_root_owned(hook, tmp_path):
    assert hasattr(hook, 'public_reader_empty_root'), 'empty sandbox root proof is missing'
    root = tmp_path / 'root'
    root.mkdir(mode=0o755)
    (root / 'business').write_text('must not be exposed')
    with pytest.raises(hook.Refused):
        hook.public_reader_empty_root(root)


def test_runtime_interpreter_cannot_escape_prefix_by_symlink(hook, tmp_path, monkeypatch):
    assert hasattr(hook, 'public_reader_python'), 'reader interpreter proof is missing'
    prefix = tmp_path / 'python'
    (prefix / 'bin').mkdir(parents=True)
    (prefix / 'bin/python3.12').symlink_to('/usr/bin/python3')
    monkeypatch.setattr(hook, 'PUBLIC_READER_PYTHON', prefix)
    with pytest.raises(hook.Refused):
        hook.public_reader_python()


def test_health_refuses_duplicate_fields_and_extra_frames(hook):
    duplicate = ('{"v":0,"v":1,"ok":true,"protocol":1,"revision":"' + 'b' * 40 + '"}\n').encode()
    with pytest.raises(hook.Refused):
        hook.public_reader_health_result(duplicate, 'b' * 40)
    with pytest.raises(hook.Refused):
        hook.public_reader_health_result(duplicate + b'\n', 'b' * 40)


def test_compatibility_proof_refuses_changed_runner_and_unlisted_sibling(hook, monkeypatch):
    called = []
    monkeypatch.setattr(hook, 'public_reader_connector', lambda: called.append(True))
    monkeypatch.setattr(hook, 'run', lambda *_args, **_kwargs: 'c' * 40)
    path = 'services/local-executor/src/runner.ts'
    with pytest.raises(hook.Refused, match='separate compatibility review'):
        hook.reviewed_public_reader_files('/mirror', 'b' * 40, {path})
    assert called == []
    assert hook.reviewed_public_reader_files('/mirror', 'b' * 40,
        {'services/local-executor/src/new-command.ts'}) == set()
    monkeypatch.setattr(hook, 'run', lambda *_args, **_kwargs: hook.PUBLIC_READER_CONNECTOR_FILES[path])
    assert hook.reviewed_public_reader_files('/mirror', 'b' * 40, {path}) == {path}
    assert called == [True]


@pytest.mark.parametrize('correct', (True, False))
def test_executor_allows_only_exact_fixed_late_append(hook, tmp_path, monkeypatch, correct):
    monkeypatch.setattr(hook, 'PUBLIC_READER_SYSTEMD', tmp_path / 'systemd')
    late = hook.public_reader_paths()[3]
    late.parent.mkdir(parents=True)
    late.write_bytes(hook.public_reader_dropin() if correct else b'[Service]\nExecStart=/unreviewed\n')
    fragment = tmp_path / 'unit'
    fragment.write_bytes(b'unit')
    expected = {'fragment_sha256': hook.executor_digest(fragment.read_bytes()),
                'dropins': {str(late): hook.executor_digest(late.read_bytes())},
                'binding_sha256': None, 'selector': None}
    props = {'FragmentPath': str(fragment), 'DropInPaths': str(late),
             'ActiveState': 'active', 'ExecStart': 'fixed-command', 'EnvironmentFiles': ''}
    monkeypatch.setattr(hook, 'property_of', lambda _service, name: props[name])
    monkeypatch.setattr(hook, 'executor_host_file', lambda path: path.read_bytes())
    monkeypatch.setattr(hook, 'executor_command_identity', lambda value: value)
    monkeypatch.setattr(hook, 'executor_selector', lambda _path: None)
    monkeypatch.setattr(hook, 'executor_existing_bindings', lambda *_args: set())
    monkeypatch.setattr(hook, 'executor_writable_bindings', lambda *_args: set())
    if correct:
        assert hook.executor_service_snapshot('worker', expected, tmp_path / 'binding.json',
            tmp_path / 'selector', tmp_path / 'zzzz-lead-executor.conf')['active'] is True
    else:
        with pytest.raises(hook.Refused, match='later executor dropin'):
            hook.executor_service_snapshot('worker', expected, tmp_path / 'binding.json',
                tmp_path / 'selector', tmp_path / 'zzzz-lead-executor.conf')


def test_prepared_readback_rejects_unexpected_application_restart(provision, monkeypatch):
    p = provision
    assert hasattr(p.h, 'verify_public_reader_prepared'), 'independent prepared-stage verification missing'
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    p.snapshots['api']['InvocationID'] = 'new-unexpected-invocation'
    with pytest.raises(p.h.Refused, match='original application'):
        p.h.verify_public_reader_prepared(p.sha)
