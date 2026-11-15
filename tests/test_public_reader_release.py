"""Fixed-purpose public reader provisioning: synthetic files and service replies only."""

import importlib.util
import io
import json
import stat
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(hook, 'verify_public_reader_application', lambda sha: calls.append(sha) or {'ok': True}, raising=False)
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
                       'uid': 995, 'gid': 983, 'route': None, 'PrivateNetwork': 'yes',
                       'JoinsNamespaceOf': 'lead-home-net.service', 'BindPaths': '',
                       'BindReadOnlyPaths': '/original:/original', 'unit_files': {'original-unit': 'd' * 64}}
                 for index, role in enumerate(h.PUBLIC_READER_CONSUMERS)}
    def consumer(role):
        data = {**snapshots[role], 'unit_files': dict(snapshots[role]['unit_files'])}
        path = h.public_reader_paths()[2 + h.PUBLIC_READER_CONSUMERS.index(role)]
        if path.exists():
            data['unit_files'][str(path)] = h.executor_digest(path.read_bytes())
            data['route'] = str(h.PUBLIC_READER_RUNTIME / 'reader.sock')
            data['BindReadOnlyPaths'] += ' ' + str(h.PUBLIC_READER_RUNTIME) + ':' + str(h.PUBLIC_READER_RUNTIME)
        return data
    monkeypatch.setattr(h, 'public_reader_consumer', consumer)
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
    monkeypatch.setattr(h, 'public_reader_blobs', lambda _sha: {'source': 'd' * 40})
    monkeypatch.setattr(h, 'public_reader_runtime', lambda _sha: {'runtime': 'synthetic'})
    monkeypatch.setattr(h, 'public_reader_account_unused', lambda: None)
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


def test_delivered_start_with_lost_reply_only_reconciles_original_journal(provision, monkeypatch):
    p = provision
    def delivered(*args, **_kwargs):
        if args == ('systemctl', 'start', p.h.PUBLIC_READER_SERVICE):
            raise TimeoutError('synthetic delivered start reply lost')
        return ''
    monkeypatch.setattr(p.h, 'run', delivered)
    with pytest.raises(TimeoutError):
        p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    original = p.h.public_reader_journal(p.sha).read_bytes()
    p.commands.clear()
    monkeypatch.setattr(p.h, 'run', lambda *args, **_kwargs: p.commands.append(args) or '')
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    record = p.h.public_reader_load(p.sha)
    assert record['state'] == 'ready'
    assert record['reconciled_from'] == 'start-requested'
    assert record['reconciled_journal_sha256'] == p.h.executor_digest(original)
    assert record['previous'] == p.plan and p.commands == []


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


def test_later_application_reuses_only_identical_reader_inputs(hook, tmp_path, monkeypatch):
    assert hasattr(hook, 'public_reader_for_application'), 'same-input reader reuse is missing'
    reader, app = 'b' * 40, 'c' * 40
    folder = hook.public_reader_journal(reader).parent
    folder.mkdir()
    hook.public_reader_journal(reader).write_text('{}')
    blobs = {'source': 'd' * 40}
    record = {'version': 1, 'sha': reader, 'state': 'ready', 'source_blobs': blobs}
    monkeypatch.setattr(hook, 'public_reader_load', lambda sha: record)
    monkeypatch.setattr(hook, 'public_reader_blobs', lambda sha: blobs)
    assert hook.public_reader_for_application(app) == record
    monkeypatch.setattr(hook, 'public_reader_blobs', lambda sha: {'source': 'e' * 40})
    with pytest.raises(hook.Refused, match='unchanged reader'):
        hook.public_reader_for_application(app)


def test_same_input_reuse_refuses_ambiguous_or_restored_journal(hook, monkeypatch):
    assert hasattr(hook, 'public_reader_for_application'), 'same-input reader reuse is missing'
    folder = hook.public_reader_journal('b' * 40).parent
    folder.mkdir()
    for sha in ('b' * 40, 'd' * 40):
        hook.public_reader_journal(sha).write_text('{}')
    monkeypatch.setattr(hook, 'public_reader_blobs', lambda sha: {'source': 'e' * 40})
    monkeypatch.setattr(hook, 'public_reader_load', lambda sha: {'sha': sha, 'state': 'ready',
        'source_blobs': {'source': 'e' * 40}})
    with pytest.raises(hook.Refused, match='unique unchanged reader'):
        hook.public_reader_for_application('c' * 40)
    monkeypatch.setattr(hook, 'public_reader_load', lambda sha: {'sha': sha, 'state': 'rolled-back',
        'source_blobs': {'source': 'e' * 40}})
    with pytest.raises(hook.Refused, match='unchanged reader'):
        hook.public_reader_for_application('c' * 40)


def test_reused_service_health_keeps_reader_revision_and_actual_application_revision(hook, monkeypatch):
    assert hasattr(hook, 'verify_public_reader_application'), 'same-input reader verification is missing'
    app, reader = 'c' * 40, 'b' * 40
    monkeypatch.setattr(hook, 'public_reader_for_application', lambda sha: {'sha': reader})
    calls = []
    monkeypatch.setattr(hook, 'verify_public_reader', lambda sha, **kwargs:
        calls.append((sha, kwargs)) or {'sha': sha, 'consumers_verified': True})
    result = hook.verify_public_reader_application(app)
    assert calls == [(reader, {'application_sha': app})]
    assert result['application_sha'] == app and result['reader_sha'] == reader


@pytest.mark.parametrize('changed', (
    'services/api/lead_app/public_reader.py', 'services/api/lead_app/fetch.py',
    'services/public_reader/systemd/lead-public-reader.service.in',
    'services/public_reader/systemd/lead-public-reader.socket.in',
    'pyproject.toml', 'requirements.lock.txt',
))
def test_changed_reader_or_dependency_input_cannot_reuse_old_service(hook, monkeypatch, changed):
    reader, app = 'b' * 40, 'c' * 40
    blobs = {name: 'd' * 40 for name in hook.PUBLIC_READER_REUSE_FILES}
    assert changed in blobs
    hook.public_reader_journal(reader).parent.mkdir()
    hook.public_reader_journal(reader).write_text('{}')
    monkeypatch.setattr(hook, 'public_reader_load', lambda sha: {'sha': reader, 'state': 'ready', 'source_blobs': blobs})
    def git(*args):
        revision, name = args[-1].split(':', 1)
        return 'e' * 40 if revision == app and name == changed else blobs[name]
    monkeypatch.setattr(hook, 'run', git)
    with pytest.raises(hook.Refused, match='unchanged reader'):
        hook.public_reader_for_application(app)


@pytest.mark.parametrize('drift', ('unit', 'read-mount', 'write-mount', 'network'))
def test_prepared_readback_refuses_reload_only_consumer_drift(provision, drift):
    p = provision
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    if drift == 'unit':
        p.snapshots['worker']['unit_files']['new-foreign-dropin'] = 'e' * 64
    elif drift == 'read-mount':
        p.snapshots['worker']['BindReadOnlyPaths'] += ' /foreign:/foreign'
    elif drift == 'write-mount':
        p.snapshots['worker']['BindPaths'] = '/foreign:/foreign'
    else:
        p.snapshots['worker']['JoinsNamespaceOf'] = 'other.service'
    with pytest.raises(p.h.Refused, match='consumer.*changed|consumer.*drift'):
        p.h.verify_public_reader_prepared(p.sha)


def test_changed_reader_is_refused_before_maintenance_or_application_restart(hook, monkeypatch):
    new, old = 'b' * 40, 'a' * 40
    class Store(type(hook.STORE)):
        def stat(self, **kwargs):
            value = super().stat(**kwargs)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode)
    monkeypatch.setattr(hook, 'STORE', Store(hook.STORE))
    source = hook.STORE / new / hook.PUBLIC_READER_SOURCE_FILES[0]
    source.parent.mkdir(parents=True)
    source.write_text('new source')
    monkeypatch.setattr(hook.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(hook, 'current', lambda: old)
    monkeypatch.setattr(hook, 'safe_layout', lambda: None)
    monkeypatch.setattr(hook, 'verify', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, 'image', lambda sha: hook.STORE / sha)
    monkeypatch.setattr(hook, 'sync_public_main', lambda *_args: None)
    monkeypatch.setattr(hook, 'public_reader_for_application', lambda sha:
        (_ for _ in ()).throw(hook.Refused('unchanged reader proof required')))
    monkeypatch.setattr(hook, 'restart_and_verify', lambda *_args: None)
    commands = []
    monkeypatch.setattr(hook, 'run', lambda *args, **_kwargs:
        commands.append(args) or (str(hook.VAULT) if args[0] == 'findmnt' else ''))
    with pytest.raises(hook.Refused, match='unchanged reader proof'):
        hook.deploy(new)
    assert not any(args[:2] == ('systemctl', 'start') for args in commands)


def test_render_binds_fixed_shared_application_site(hook):
    assert hasattr(hook, 'PUBLIC_READER_SITE'), 'separate shared site runtime is missing'
    assert hook.public_reader_render('@SITE@', 'b' * 40, 995, 983) == str(hook.PUBLIC_READER_SITE)


@pytest.mark.parametrize('field', ('uid', 'gid', 'groups'))
def test_reader_cannot_reuse_business_os_identity(hook, field):
    assert hasattr(hook, 'public_reader_process_identity'), 'reader identity separation is missing'
    uid, gid, groups = ('995', '62001', '62001') if field == 'uid' else (
        ('62001', '983', '62001') if field == 'gid' else ('62001', '62001', '62001 983'))
    status = f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t{gid}\t{gid}\t{gid}\t{gid}\nGroups:\t{groups}\n'
    with pytest.raises(hook.Refused):
        hook.public_reader_process_identity(status, 995, 983)


def test_first_provision_rejects_static_reader_account_collision(hook, monkeypatch):
    assert hasattr(hook, 'public_reader_account_unused'), 'dynamic identity collision guard missing'
    monkeypatch.setattr(hook.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=995))
    with pytest.raises(hook.Refused, match='identity already exists'):
        hook.public_reader_account_unused()


def test_metadata_dependency_versions_must_match_candidate_lock(hook, tmp_path, monkeypatch):
    assert hasattr(hook, 'public_reader_dependencies'), 'shared site dependency proof is missing'
    monkeypatch.setattr(hook, 'PUBLIC_READER_SITE', tmp_path / 'site')
    hook.PUBLIC_READER_SITE.mkdir()
    lock = ''
    for name in hook.PUBLIC_READER_DEPENDENCIES:
        folder = hook.PUBLIC_READER_SITE / (name.replace('-', '_') + '-1.0.dist-info')
        folder.mkdir()
        (folder / 'METADATA').write_text(f'Name: {name}\nVersion: 1.0\n')
        lock += name + '==1.0\n'
    monkeypatch.setattr(hook, 'executor_directory', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, 'executor_host_file', lambda path: path.read_bytes())
    monkeypatch.setattr(hook, 'run', lambda *_args, **_kwargs: lock)
    result = hook.public_reader_dependencies('b' * 40)
    assert len(result) == 10 and all(value['version'] == '1.0' for value in result.values())
    next(hook.PUBLIC_READER_SITE.glob('httpx-*.dist-info/METADATA')).write_text('Name: httpx\nVersion: 2.0\n')
    with pytest.raises(hook.Refused, match='dependency.*differ'):
        hook.public_reader_dependencies('b' * 40)


@pytest.mark.parametrize('during_backup', (False, True))
def test_direct_deploy_refuses_consumer_drift_before_application_switch(provision, monkeypatch, during_backup):
    p = provision
    p.h.provision_public_reader(p.sha, p.h.PUBLIC_READER_PLAN)
    source = p.h.STORE / p.sha / p.h.PUBLIC_READER_SOURCE_FILES[0]
    source.parent.mkdir(parents=True)
    source.write_text('public reader source fixture')
    class Store(type(p.h.STORE)):
        def stat(self, **kwargs):
            value = super().stat(**kwargs)
            return SimpleNamespace(st_uid=0, st_mode=value.st_mode)
    monkeypatch.setattr(p.h, 'STORE', Store(p.h.STORE))
    monkeypatch.setattr(p.h, 'safe_layout', lambda: None)
    monkeypatch.setattr(p.h, 'verify', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(p.h, 'image', lambda sha: p.h.STORE / sha)
    monkeypatch.setattr(p.h, 'sync_public_main', lambda *_args: None)
    p.commands.clear()
    activations = []
    monkeypatch.setattr(p.h, 'replace_release', lambda *_args: activations.append(True))
    def drift():
        p.snapshots['worker']['unit_files']['foreign-dropin'] = 'e' * 64
    def command(*args, **_kwargs):
        p.commands.append(args)
        if args == ('systemctl', 'start', 'lead-pc-local-backup.service'):
            drift()
        return str(p.h.VAULT) if args[0] == 'findmnt' else ''
    monkeypatch.setattr(p.h, 'run', command)
    if not during_backup:
        drift()
    with pytest.raises(p.h.Refused, match='consumer.*changed'):
        p.h.deploy(p.sha)
    assert activations == []
    if not during_backup:
        assert not any(args[:2] == ('systemctl', 'start') for args in p.commands)


@pytest.fixture
def masked_root(hook, tmp_path, monkeypatch):
    proc = tmp_path / 'proc/123'
    (proc / 'root/root').mkdir(parents=True)
    source = tmp_path / 'inaccessible/dir'
    source.mkdir(parents=True)
    monkeypatch.setattr(hook, 'PUBLIC_READER_INACCESSIBLE', source, raising=False)
    status = ('Uid:\t62001 62001 62001 62001\nGid:\t62001 62001 62001 62001\nGroups:\t62001\n'
              'CapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n'
              'CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\n'
              'CapAmb:\t0000000000000000\nNoNewPrivs:\t1\n')
    (proc / 'status').write_text(status)
    (proc / 'mountinfo').write_text('20 10 0:10 /systemd/inaccessible/dir /root ro,nosuid - tmpfs tmpfs rw\n')
    node = SimpleNamespace(st_mode=stat.S_IFDIR, st_uid=0, st_gid=0, st_dev=10, st_ino=100)
    target = SimpleNamespace(**vars(node))
    real_lstat = Path.lstat
    def lstat(path):
        if path == source:
            return node
        if path == proc / 'root/root':
            return target
        return real_lstat(path)
    monkeypatch.setattr(Path, 'lstat', lstat)
    monkeypatch.setattr(hook, 'executor_directory', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, 'process', lambda _role: proc)
    properties = {'ProtectHome': 'yes', 'InvocationID': 'a' * 32}
    monkeypatch.setattr(hook, 'property_of', lambda _role, prop: properties[prop])
    probes = []
    monkeypatch.setattr(hook, 'public_reader_root_probe', lambda *args: probes.append(args), raising=False)
    return SimpleNamespace(h=hook, proc=proc, node=node, target=target, probes=probes,
                           properties=properties, plan={'client_uid': 995, 'client_gid': 983})


def test_systemd_fixed_inaccessible_root_is_accepted(masked_root):
    p = masked_root
    assert hasattr(p.h, 'public_reader_masked_root'), 'fixed masked-root verification is missing'
    p.h.public_reader_masked_root(p.proc, p.plan)
    assert len(p.probes) == 1 and p.probes[0][:3] == (p.proc, 62001, 62001)


@pytest.mark.parametrize('drift', ('ordinary-empty', 'other-inode', 'symlink', 'source-mode', 'target-mode',
                                  'missing-mount', 'writable', 'nested', 'protection', 'capabilities'))
def test_root_exception_rejects_ordinary_or_unproven_directory(masked_root, drift):
    p = masked_root
    assert hasattr(p.h, 'public_reader_masked_root'), 'fixed masked-root verification is missing'
    if drift == 'ordinary-empty':
        p.target.st_ino += 1
        (p.proc / 'mountinfo').write_text('')
    elif drift == 'other-inode':
        p.target.st_ino += 1
    elif drift == 'symlink':
        p.target.st_mode = stat.S_IFLNK
    elif drift == 'source-mode':
        p.node.st_mode |= 0o755
    elif drift == 'target-mode':
        p.target.st_mode |= 0o700
    elif drift == 'missing-mount':
        (p.proc / 'mountinfo').write_text('')
    elif drift == 'writable':
        path = p.proc / 'mountinfo'
        path.write_text(path.read_text().replace('ro,nosuid', 'rw,nosuid'))
    elif drift == 'nested':
        path = p.proc / 'mountinfo'
        path.write_text(path.read_text() + '21 20 1:1 /secret /root/secret ro - ext4 device ro\n')
    elif drift == 'protection':
        p.properties['ProtectHome'] = 'read-only'
    else:
        path = p.proc / 'status'
        path.write_text(path.read_text().replace('CapEff:\t0000000000000000', 'CapEff:\t0000000000000001'))
    with pytest.raises(p.h.Refused):
        p.h.public_reader_masked_root(p.proc, p.plan)
    assert p.probes == []


def test_root_proof_rejects_service_invocation_change(masked_root, monkeypatch):
    p = masked_root
    assert hasattr(p.h, 'public_reader_masked_root'), 'fixed masked-root verification is missing'
    def probe(*args):
        p.properties['InvocationID'] = 'b' * 32
    monkeypatch.setattr(p.h, 'public_reader_root_probe', probe)
    with pytest.raises(p.h.Refused, match='service changed'):
        p.h.public_reader_masked_root(p.proc, p.plan)


@pytest.mark.parametrize('path', ('srv', 'home', 'run/lead-home', 'run/credentials', 'etc/lead-home-release'))
def test_other_private_paths_still_require_absence(masked_root, path):
    p = masked_root
    assert hasattr(p.h, 'public_reader_private_paths'), 'private path categories are missing'
    (p.proc / 'root' / path).mkdir(parents=True)
    with pytest.raises(p.h.Refused, match='private .* visible'):
        p.h.public_reader_private_paths(p.proc, p.plan)


def test_root_probe_has_empty_environment_and_fixed_no_output_contract(hook, monkeypatch):
    assert hasattr(hook, 'public_reader_root_probe'), 'service-root EACCES probe is missing'
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'root-inaccessible\n', b'')
    monkeypatch.setattr(hook.subprocess, 'run', run)
    hook.public_reader_root_probe(Path('/proc/123'), 62001, 62001, (10, 100))
    args, options = calls[0]
    assert args[:5] == ['/usr/bin/python3', '-I', '-S', '-B', '-c']
    assert options['env'] == {} and options['timeout'] <= 10 and options['close_fds'] is True
    assert options['capture_output'] is True
    assert options.get('stdin') == subprocess.DEVNULL


@pytest.mark.parametrize('returncode,stdout', ((1, b''), (0, b''), (0, b'wrong\n'), (0, b'root-inaccessible\nextra')))
def test_root_probe_rejects_unknown_or_non_eacces_result(hook, monkeypatch, returncode, stdout):
    assert hasattr(hook, 'public_reader_root_probe'), 'service-root EACCES probe is missing'
    monkeypatch.setattr(hook.subprocess, 'run', lambda args, **kwargs:
        subprocess.CompletedProcess(args, returncode, stdout, b'ignored private failure'))
    with pytest.raises(hook.Refused, match='probe failed'):
        hook.public_reader_root_probe(Path('/proc/123'), 62001, 62001, (10, 100))


def test_root_identity_proof_ignores_unrelated_process_counters(masked_root, monkeypatch):
    p = masked_root
    def probe(*args):
        path = p.proc / 'status'
        path.write_text(path.read_text() + 'voluntary_ctxt_switches:\t100\n')
    monkeypatch.setattr(p.h, 'public_reader_root_probe', probe)
    p.h.public_reader_masked_root(p.proc, p.plan)


@pytest.mark.parametrize(('role', 'expected'), [
    ('api', 'lead-home-api.service'),
    ('lead-public-reader.service', 'lead-public-reader.service'),
    ('lead-public-reader.socket', 'lead-public-reader.socket'),
])
def test_status_readback_queries_the_actual_unit(hook, monkeypatch, role, expected):
    calls = []
    monkeypatch.setattr(hook, 'run', lambda *args: calls.append(args) or 'active')
    assert hook.property_of(role, 'ActiveState') == 'active'
    assert calls == [('systemctl', 'show', expected, '-p', 'ActiveState', '--value')]


def test_systemd_optional_bind_source_is_a_modifier_not_a_relative_path(hook):
    assert hook.public_reader_bindings('/usr:/usr:rbind -/lib:/lib:rbind -/lib64:/lib64:rbind') == {
        ('/usr', '/usr'), ('/lib', '/lib'), ('/lib64', '/lib64')}


@pytest.mark.parametrize('binding', ('--/lib:/lib:rbind', '-lib:/lib', '-/usr/../lib:/lib',
                                     '/lib:-/lib', '/lib:/lib:unknown', '/lib:/lib:'))
def test_optional_bind_modifier_never_bypasses_canonical_path_guard(hook, binding):
    with pytest.raises(hook.Refused):
        hook.public_reader_bindings(binding)
