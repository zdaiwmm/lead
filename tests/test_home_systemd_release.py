"""Filesystem cutover, mount identity and lost-status regression cases (no production data)."""

import importlib
import stat
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    h = importlib.import_module("home_systemd_release")
    monkeypatch.setattr(h, "STORE", tmp_path)
    # A maintenance host may really have these installed. Unit tests select
    # their own fixture tools instead of discovering the host's live verifier.
    monkeypatch.setattr(h, "TOOLS", tmp_path / "tools")
    for sha in ("a" * 40, "b" * 40):
        (tmp_path / sha).mkdir()
    h.select("a" * 40)
    return h


@pytest.mark.parametrize("fail", (False, True))
def test_switch_and_recovery(hook, monkeypatch, fail):
    old, new = "a" * 40, "b" * 40
    activations = []

    def restart(sha):
        assert (hook.STORE / "current").resolve() == hook.STORE / sha
        activations.append(sha)
        if fail and sha == new:
            raise hook.Refused("injected readiness failure")

    monkeypatch.setattr(hook, "restart_and_verify", restart)
    data = hook.STORE / "persistent-canary"
    data.write_bytes(b"fixture")
    if fail:
        with pytest.raises(hook.Refused, match="previous release independently verified"):
            hook.replace_release(old, new)
    else:
        hook.replace_release(old, new)
    assert activations == ([new, old] if fail else [new])
    assert (hook.STORE / "current").readlink().name == (old if fail else new)
    assert data.read_bytes() == b"fixture"


def test_live_mounts_overrule_selector(hook, monkeypatch):
    sha = "a" * 40
    target = hook.SERVICES
    proc = hook.STORE / "proc-1"
    view = proc / "root" / target.relative_to("/")
    view.parent.mkdir(parents=True)
    (hook.STORE / sha / target.name).mkdir()
    view.symlink_to(hook.STORE / sha / target.name)
    mounts = proc / "mountinfo"
    entry = f"30 20 1:1 /services {target} ro - squashfs image ro\n"
    mounts.write_text(entry)
    monkeypatch.setattr(hook, "safe_layout", lambda: None)
    monkeypatch.setattr(hook, "process", lambda role: proc)
    monkeypatch.setattr(hook, "ROLES", {"api": target})
    monkeypatch.setattr(hook, "image", lambda rev: hook.STORE / rev)
    assert hook.current() == sha

    mounts.write_text(entry + f"31 30 1:2 /overlay {target}/patched.py ro - ext4 disk rw\n")
    with pytest.raises(hook.Refused, match="overlay"):
        hook.current()
    mounts.write_text(entry)
    other = "b" * 40
    (hook.STORE / other / target.name).mkdir()
    hook.select(other)
    with pytest.raises(hook.Refused, match="different image"):
        hook.current()


def test_shared_layout_refuses_before_mutation(hook, monkeypatch):
    monkeypatch.setattr(hook, "property_of", lambda *_: "chamber-workspace-releases/build")
    with pytest.raises(hook.Refused, match="Chamber"):
        hook.current()


def test_lost_report_uses_saved_outcome_without_reverification(hook, monkeypatch):
    r = importlib.import_module("poll_home_release")
    sha = "b" * 40
    directory = hook.STORE
    r.save_record(directory / "7.started", sha)
    launched, reported = [], []

    def probe(cmd, check):
        launched.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    def report(request_id, state):
        reported.append((request_id, state))
        if len(reported) == 1:
            raise ConnectionError

    monkeypatch.setattr(r.subprocess, "run", probe)
    monkeypatch.setattr(r, "report", report)
    with pytest.raises(ConnectionError):
        r.finish(directory, "/hook", 7, sha)
    r.finish(directory, "/hook", 7, sha)
    assert launched == [["/hook", "verify", sha]]
    assert reported == [(7, "success")] * 2


def test_prior_request_recovered_before_new_deploy(hook, monkeypatch):
    r = importlib.import_module("poll_home_release")
    old, new = "a" * 40, "b" * 40
    r.save_record(hook.STORE / "7.started", old)
    r.save_record(hook.STORE / "7.result", "error")
    replies = iter([[dict(id=8, sha=new)], [], dict(id=7, sha=old), []])
    monkeypatch.setattr(r, "api", lambda _: next(replies))
    reports = []
    monkeypatch.setattr(r, "report", lambda *args: reports.append(args))
    with pytest.raises(ValueError, match="verification failed"):
        r.poll(hook.STORE, "/must-not-execute")
    assert reports == [(7, "error")]
    assert not (hook.STORE / "8.started").exists()


@pytest.mark.parametrize("application_path", (
    "services/api/lead_app/app.py", "services/channel_adapters/reddit/entry.py",
    "services/channel_adapters/systemd/executor.conf.in",
    "scripts/verify_cadence_ui.py",
))
@pytest.mark.parametrize("unreviewed_path", ("infra/migrations/new.py", "scripts/unreviewed.py"))
def test_deploy_path_checks_compatibility_and_skips_repeats(
    hook, monkeypatch, application_path, unreviewed_path,
):
    new = "b" * 40
    commands = []
    changes = unreviewed_path + "\0"

    def command(*args, **kwargs):
        commands.append(args)
        if args[0] == "findmnt":
            return str(hook.VAULT)
        if "diff" in args:
            return changes
        return ""

    class Store(type(hook.STORE)):
        def stat(self, **kwargs):
            from types import SimpleNamespace
            value = super().stat(**kwargs)
            return SimpleNamespace(st_mode=value.st_mode, st_uid=0)

    monkeypatch.setattr(hook, "STORE", Store(hook.STORE))
    monkeypatch.setattr(hook.os, "geteuid", lambda: 0)
    monkeypatch.setattr(hook, "run", command)
    monkeypatch.setattr(hook, "safe_layout", lambda: None)
    monkeypatch.setattr(hook, "image", lambda sha: hook.STORE / sha)
    monkeypatch.setattr(hook, "current", lambda: (hook.STORE / "current").readlink().name)
    activations = []

    def verify(sha, *, fenced=False, require_idle=True):
        assert hook.current() == sha

    def restart(sha):
        verify(sha)
        activations.append(sha)

    monkeypatch.setattr(hook, "verify", verify)
    monkeypatch.setattr(hook, "restart_and_verify", restart)
    with pytest.raises(hook.Refused, match="compatibility"):
        hook.deploy(new)
    assert activations == [] and not any("start" in c for c in commands)

    changes = application_path + "\0"
    hook.deploy(new)
    hook.deploy(new)
    assert [c[2] for c in commands if c[:2] == ("systemctl", "start")] == [
        f"lead-home-release-fence@{'a' * 40}-{new}.service", "lead-pc-local-backup.service",
        "lead-home-ingress.socket"
    ]
    assert activations == [new] and hook.current() == new


def test_code_archive_is_content_addressed_and_private(hook, tmp_path, monkeypatch):
    import io
    import json
    import tarfile
    from types import SimpleNamespace

    inventory = importlib.import_module("home_code_inventory")
    monkeypatch.setattr(inventory, "VAULT", tmp_path)
    monkeypatch.setattr(inventory.os, "geteuid", lambda: 0)
    monkeypatch.setattr(inventory.subprocess, "check_output", lambda *a, **kw: str(tmp_path))
    actual = Path.stat

    def stat(path, **kwargs):
        value = actual(path, **kwargs)
        if path.name == "lead-code-inventory":
            return SimpleNamespace(st_mode=value.st_mode, st_uid=0)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    def synthetic(bundle):
        inventory.add_bytes(bundle, "objects/test", b"code-only")
        return {"unit": {"files": {"service.py": "test"}}}
    monkeypatch.setattr(inventory, "inventory", synthetic)
    first = inventory.archive()
    assert inventory.archive() == first
    folder = tmp_path / "lead-code-inventory"
    archives = list(folder.glob("*.tar"))
    assert len(archives) == 1 and archives[0].stat().st_mode & 0o077 == 0
    with tarfile.open(fileobj=io.BytesIO(archives[0].read_bytes())) as bundle:
        assert bundle.extractfile("objects/test").read() == b"code-only"
        assert json.load(bundle.extractfile("manifest.json"))["unit"]["files"] == {"service.py": "test"}


def test_attended_node_rejects_other_revision_or_mismatched_response(hook, monkeypatch):
    import io

    module = importlib.import_module("poll_home_release")
    sha = "a" * 40
    host = module.AttendedHost(sha)
    with pytest.raises(ValueError, match="another revision"):
        host.call("deploy", "b" * 40)
    monkeypatch.setattr(module.sys, "stdin", io.StringIO('{"ok":true,"operation":"deploy","sha":"wrong"}\n'))
    with pytest.raises(ValueError, match="acknowledgement mismatch"):
        host.call("verify", sha)


def test_only_reviewed_maintenance_blobs_are_accepted(hook, monkeypatch, tmp_path):
    # A node tool changed in the repo must still pass a separate compatibility review.
    monkeypatch.setattr(hook, "TOOLS", tmp_path)
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    for name in hook.MAINTENANCE_FILES:
        path = repo / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text("reviewed\n")
    git("add", ".")
    git("-c", "user.name=fixture", "-c", "user.email=test@example.test", "commit", "-qm", "baseline")
    monkeypatch.setattr(hook, "MAINTENANCE_REVISION", git("rev-parse", "HEAD"))
    extra = hook.MAINTENANCE_FILES | {"infra/migrations/unrelated.sql"}
    assert not hook.reviewed_maintenance_files(repo / ".git", "HEAD", extra)
    (tmp_path / "home_live_maintenance.py").touch()
    assert hook.reviewed_maintenance_files(repo / ".git", "HEAD", extra) == hook.MAINTENANCE_FILES
    (repo / "scripts/poll_home_release.py").write_text("changed\n")
    git("add", ".")
    git("-c", "user.name=fixture", "-c", "user.email=test@example.test", "commit", "-qm", "new tool")
    assert hook.reviewed_maintenance_files(repo / ".git", "HEAD", extra) == (
        hook.MAINTENANCE_FILES - {"scripts/poll_home_release.py"})


def test_installed_handoff_tool_requires_exact_candidate_bytes(hook, tmp_path, monkeypatch):
    monkeypatch.setattr(hook, "TOOLS", tmp_path)

    repo = tmp_path / "handoff"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
    name = "scripts/home_live_maintenance.py"
    candidate = repo / name
    candidate.parent.mkdir()
    candidate.write_text("old")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=fixture@example.test", "commit", "-qm", "baseline")
    monkeypatch.setattr(hook, "MAINTENANCE_REVISION", git("rev-parse", "HEAD"))
    candidate.write_text("reviewed")
    git("add", ".")
    git("-c", "user.name=test", "-c", "user.email=fixture@example.test", "commit", "-qm", "candidate")
    installed = tmp_path / candidate.name
    installed.write_text("different")
    assert not hook.reviewed_maintenance_files(repo / ".git", "HEAD", {name})
    installed.write_bytes(candidate.read_bytes())
    assert hook.reviewed_maintenance_files(repo / ".git", "HEAD", {name}) == {name}


def test_only_reviewed_migrations_on_attested_database(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "TOOLS", tmp_path)
    (tmp_path / "home_live_maintenance.py").touch()
    paths = set(hook.CHANNEL_SCHEMA_FILES) | {"infra/migrations/versions/unreviewed.py"}
    live = ["ready"]
    override = {}
    def read(*args):
        if args[-1] == "channel-schema":
            return live[0]
        path = args[-1].split(":",1)[1]
        return override.get(path, hook.CHANNEL_SCHEMA_FILES[path])
    monkeypatch.setattr(hook,"run",read)
    assert hook.reviewed_channel_schema(tmp_path,"a"*40,paths) == set(hook.CHANNEL_SCHEMA_FILES)
    override[".env.example"] = "modified"
    assert not hook.reviewed_channel_schema(tmp_path,"a"*40,paths)
    override.clear()
    live[0] = "mismatch"
    with pytest.raises(hook.Refused):
        hook.reviewed_channel_schema(tmp_path,"a"*40,paths)


def test_runtime_manifest_and_tool_are_exact_reviewed_blobs(hook, monkeypatch, tmp_path):
    monkeypatch.setattr(hook, "TOOLS", tmp_path)
    (tmp_path / "home_live_maintenance.py").touch()
    checked = []
    monkeypatch.setattr(hook, "verify_model_runtime", lambda: checked.append(True))
    blobs = {path: next(iter(values)) for path, values in hook.RUNTIME_COMPATIBILITY_FILES.items()}
    monkeypatch.setattr(hook, "run", lambda *args: blobs[args[-1].split(":", 1)[1]])
    paths = set(blobs) | {"scripts/unreviewed.py", "requirements.txt"}
    assert hook.reviewed_runtime_files(tmp_path, "a" * 40, paths) == set(blobs)
    assert checked == [True]
    blobs["pyproject.toml"] = "new-dependency"
    with pytest.raises(hook.Refused, match="separate compatibility review"):
        hook.reviewed_runtime_files(tmp_path, "a" * 40, paths)
    assert checked == [True]
    blobs["scripts/verify_account_browser.py"] = "changed-tool"
    with pytest.raises(hook.Refused, match="separate compatibility review"):
        hook.reviewed_runtime_files(tmp_path, "a" * 40, {"scripts/verify_account_browser.py"})
    assert not hook.reviewed_runtime_files(tmp_path, "a" * 40, {"scripts/unreviewed.py"})


def test_runtime_dependency_probe_is_unprivileged_and_fails_closed(hook, monkeypatch):
    commands = []
    monkeypatch.setattr(hook, "process", lambda role: Path("/proc", str({"api": 2, "worker": 3, "browser": 4}[role])))
    def probe(*args):
        commands.append(args)
        return "model-runtime-ready"
    monkeypatch.setattr(hook, "run", probe)
    hook.verify_model_runtime()
    assert [args[2] for args in commands] == ["2", "3", "4"]
    for args in commands:
        assert "--reuid=lead-r-app" in args and "--clear-groups" in args
        assert "-I" in args and "-e" not in args
        assert "2.16.0" in args[-1] and "trust_env=False" in args[-1]
    monkeypatch.setattr(hook, "run", lambda *args: "missing")
    with pytest.raises(hook.Refused, match="dependency unavailable"):
        hook.verify_model_runtime()
    def missing(*args):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(hook, "run", missing)
    with pytest.raises(subprocess.CalledProcessError):
        hook.verify_model_runtime()
    monkeypatch.setattr(hook, "run", probe)
    processes = iter([Path("/proc/2"), Path("/proc/5")])
    monkeypatch.setattr(hook, "process", lambda role: next(processes))
    with pytest.raises(hook.Refused, match="changed during compatibility"):
        hook.verify_model_runtime()


def test_operator_download_tool_is_not_installed_or_executed_by_application_release(hook):
    name = "scripts/prepare_home_image.py"
    assert name in hook.OPERATOR_ONLY_FILES
    assert name not in hook.RUNTIME_COMPATIBILITY_FILES
    assert "scripts/arbitrary.py" not in hook.OPERATOR_ONLY_FILES
    assert "scripts/home_live_maintenance.py" not in hook.OPERATOR_ONLY_FILES
    builder = (Path(__file__).resolve().parents[1] / "scripts/build_home_image.sh").read_text()
    assert 'git archive "$sha" services apps/web infra/home-browser-frames' in builder


def test_fault_isolation_requires_exact_reviewed_files_and_live_schema(hook, monkeypatch, tmp_path):
    paths = set(hook.FAULT_ISOLATION_FILES)
    answers = {'ready': 't'}

    def run(*args, **kwargs):
        if args[0] == 'git':
            return hook.FAULT_ISOLATION_FILES[args[-1].split(':', 1)[1]]
        assert args[:3] == ('/usr/bin/python3', '-IB', '-c')
        assert 'site_sql' in args[3]
        return answers['ready']

    monkeypatch.setattr(hook, "TOOLS", tmp_path / "tools")
    hook.TOOLS.mkdir()
    (hook.TOOLS / 'home_live_maintenance.py').write_text('# existing verifier')
    monkeypatch.setattr(hook, 'run', run)
    assert hook.reviewed_fault_isolation('/mirror', 'b' * 40, paths) == paths
    answers['ready'] = 'f'
    with pytest.raises(hook.Refused, match='schema is not ready'):
        hook.reviewed_fault_isolation('/mirror', 'b' * 40, paths)
    monkeypatch.setattr(hook, 'run', lambda *a: 'unknown')
    with pytest.raises(hook.Refused, match='compatibility review'):
        hook.reviewed_fault_isolation('/mirror', 'b' * 40, paths)


def test_growth_schema_files_and_retained_cutover_hold_are_both_required(hook,monkeypatch,tmp_path):
    paths=set(hook.GROWTH_RUNTIME_FILES)
    assert len(paths)==3
    monkeypatch.setattr(hook,'TOOLS',tmp_path/'tools')
    hook.TOOLS.mkdir()
    (hook.TOOLS/'home_live_maintenance.py').write_text('# trusted existing verifier')
    def run(*args):
        if args[0]=='git':
            return hook.GROWTH_RUNTIME_FILES[args[-1].split(':',1)[1]]
        return 't'
    monkeypatch.setattr(hook,'run',run)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda sha:False)
    with pytest.raises(hook.Refused,match='hold is missing'):
        hook.reviewed_growth_runtime('/mirror','b'*40,paths)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda sha:True)
    assert hook.reviewed_growth_runtime('/mirror','b'*40,paths)==paths
    monkeypatch.setattr(hook,'run',lambda *args:'unreviewed')
    with pytest.raises(hook.Refused,match='compatibility review'):
        hook.reviewed_growth_runtime('/mirror','b'*40,paths)


def test_growth_hold_marker_fails_closed_on_changed_identity(hook,monkeypatch):
    sha='b'*40
    assert not hook.growth_cutover_pending(sha)
    stage=hook.STORE/('maintenance-'+sha)
    stage.mkdir(mode=0o700)
    marker=stage/'growth-cutover.pending'
    marker.write_text('a'*40)
    monkeypatch.setattr(hook,'executor_directory',lambda *args,**kwargs:None)
    monkeypatch.setattr(hook,'executor_host_file',lambda path,**kwargs:path.read_bytes())
    with pytest.raises(hook.Refused,match='differs'):
        hook.growth_cutover_pending(sha)
    marker.write_text(sha+'\n')
    assert hook.growth_cutover_pending(sha)


@pytest.mark.parametrize('failure',[None,'provision','reddit','finish'])
def test_growth_cutover_finishes_only_after_actual_executor_delivery(hook,monkeypatch,failure):
    import json
    sha,old='b'*40,'a'*40
    pending=True
    calls=[]
    stage=hook.STORE/('maintenance-'+sha)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:pending)
    def host_file(path,**kwargs):
        if path.name=='growth-migration.json':
            assert kwargs.get('private')
            return json.dumps({'sha':sha,'old':old}).encode()
        assert path==stage/'source/scripts/growth_runtime_upgrade.py'
        return b'# attested source'
    monkeypatch.setattr(hook,'executor_host_file',host_file)
    def provision(revision,path):
        channel=path.name.removesuffix('-provision.json')
        calls.append('provision:'+channel)
        assert revision==sha and path==hook.EXECUTOR_REGISTRY.parent/(channel+'-provision.json')
        if failure=='provision' or failure==channel:
            raise hook.Refused('provision failed')
    monkeypatch.setattr(hook,'provision_executor',provision)
    monkeypatch.setattr(hook,'register_executors',lambda revision:calls.append('register'))
    def run(*args,**kwargs):
        nonlocal pending
        if args[0]=='git':
            return hook.GROWTH_RUNTIME_FILES['scripts/growth_runtime_upgrade.py']
        assert args==(hook.EXECUTOR_PYTHON_SOURCE,'-I','-B',stage/'source/scripts/growth_runtime_upgrade.py',
            'finish','--sha',sha,'--old',old)
        calls.append('finish')
        if failure=='finish':
            raise hook.Refused('independent readback failed')
        pending=False
        return ''
    monkeypatch.setattr(hook,'run',run)
    if failure:
        with pytest.raises(hook.Refused):
            hook.complete_growth_cutover(sha)
        assert pending
    else:
        hook.complete_growth_cutover(sha)
        assert not pending
    assert calls==(['provision:telegram'] if failure=='provision' else
        ['provision:telegram','provision:reddit'] if failure=='reddit' else
        ['provision:telegram','provision:reddit','register','finish'])


def test_deploy_cli_does_not_report_completion_before_growth_finalization(hook,monkeypatch):
    import sys
    sha='b'*40
    calls=[]
    monkeypatch.setattr(sys,'argv',['home_systemd_release.py','deploy',sha])
    monkeypatch.setattr(hook,'deploy',lambda revision:calls.append(('app',revision)))
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:True)
    monkeypatch.setattr(hook,'complete_growth_cutover',lambda revision:calls.append(('executors-and-readback',revision)))
    hook.main()
    assert calls==[('app',sha),('executors-and-readback',sha)]


@pytest.mark.parametrize('pending',[False,True])
def test_preflight_reads_exact_staged_hold_without_reopening_intake(hook,monkeypatch,pending):
    import json
    old,sha='a'*40,'b'*40
    checks=[]
    monkeypatch.setattr(hook,'current',lambda:old)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:pending)
    monkeypatch.setattr(hook,'executor_host_file',lambda *args,**kwargs:json.dumps({'sha':sha,'old':old}).encode())
    monkeypatch.setattr(hook,'verify',lambda *args,**kwargs:checks.append((args,kwargs)))
    assert hook.release_preflight(sha)==old
    assert checks==[((old,),{'fenced':pending,'require_idle':not pending})]


def test_preflight_rejects_a_hold_journal_for_another_running_revision(hook,monkeypatch):
    import json
    sha='b'*40
    monkeypatch.setattr(hook,'current',lambda:'a'*40)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:True)
    monkeypatch.setattr(hook,'executor_host_file',lambda *args,**kwargs:json.dumps({'sha':sha,'old':'c'*40}).encode())
    monkeypatch.setattr(hook,'verify',lambda *args,**kwargs:pytest.fail('A mismatched hold must never reach verification'))
    with pytest.raises(hook.Refused,match='revision differs'):
        hook.release_preflight(sha)


def test_interrupted_cutover_resumes_only_after_same_app_revision_is_observed(hook,monkeypatch):
    sha='b'*40
    calls=[]
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:True)
    monkeypatch.setattr(hook,'current',lambda:'a'*40)
    monkeypatch.setattr(hook,'complete_growth_cutover',lambda revision:calls.append(revision))
    with pytest.raises(hook.Refused,match='has not completed'):
        hook.finalize_cutover(sha)
    assert not calls
    monkeypatch.setattr(hook,'current',lambda:sha)
    hook.finalize_cutover(sha)
    assert calls==[sha]


def test_attended_request_finalizes_before_separate_readback(hook,monkeypatch):
    import io
    import json
    from types import SimpleNamespace

    operator=importlib.import_module('home_release_operator')
    sha,old='b'*40,'a'*40
    calls=[]
    def read(command,**kwargs):
        calls.append(command[-2:] if command[-1]!= 'current' else ['current'])
        return old+'\n'
    def run(command,**kwargs):
        calls.append(command[-2:])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(operator.subprocess,'check_output',read)
    monkeypatch.setattr(operator.subprocess,'run',run)
    replies=io.StringIO()
    operator.serve([json.dumps({'operation':operation,'sha':sha}) for operation in ('current','verify')],replies,['ssh','existing-host'],sha)
    assert calls==[['current'],['preflight',sha],['finalize-cutover',sha],['verify',sha]]
    assert all(json.loads(line)['ok'] for line in replies.getvalue().splitlines())


def test_growth_upgrade_uses_registered_executor_configuration(hook,tmp_path):
    import json
    upgrade=importlib.import_module('growth_runtime_upgrade')
    config=tmp_path/'etc/lead-home-release'
    config.mkdir(parents=True)
    expected={'channel':'telegram','profile_slot':'synthetic-slot'}
    (config/'telegram.json').write_text(json.dumps(expected))
    reads=[]
    def read(path):
        reads.append(path)
        return path.read_bytes()
    verifier={'EXECUTOR_REGISTRY':config/'executors.json','executor_config':read,'executor_binding':lambda value:value}
    assert upgrade.telegram_binding(verifier)==expected
    assert reads==[config/'telegram.json']
    (config/'telegram.json').unlink()
    with pytest.raises(FileNotFoundError):
        upgrade.telegram_binding(verifier)


@pytest.fixture
def executors(hook, monkeypatch, tmp_path):
    """Synthetic proc trees; only kernel mount identity and owner uid are substituted."""
    import hashlib
    import json
    from types import SimpleNamespace

    revision = 'a' * 40
    release = hook.STORE / revision
    core = release / 'services/api/lead_app'
    core.mkdir(parents=True)
    (core / '__init__.py').write_text('')
    (core / 'channel_executor.py').write_text('# fixture launcher\n')
    (release / 'REVISION').write_text(revision)
    proc_base = tmp_path / 'proc'
    registry = tmp_path / 'config/executors.json'
    registry.parent.mkdir(mode=0o700)
    entries, bindings, proofs, props = [], {}, {}, {}
    writable_identity = {}
    interpreter = tmp_path / 'trusted/python3.12'
    interpreter.parent.mkdir()
    interpreter.write_bytes(b'fixture interpreter')
    interpreter.chmod(0o755)
    monkeypatch.setattr(hook, 'EXECUTOR_PYTHON_SOURCE', interpreter, raising=False)
    def digest(data):
        return hashlib.sha256(data).hexdigest()
    core_files = {p.name: digest(p.read_bytes()) for p in core.iterdir()}
    for i, channel in enumerate(('telegram', 'reddit'), 10):
        service = f'lead-test-{channel}.service'
        pid, invocation = str(i), str(i) * 16
        proc = proc_base / pid
        root = proc / 'root'
        target = '/run/lead-executor/release'
        view = root / target.lstrip('/')
        view.mkdir(parents=True)
        # Hard links model identity of the exact mounted image files, avoiding symlinks.
        import shutil
        shutil.copytree(release, view, dirs_exist_ok=True)
        adapter = root / f'run/lead-executors/{channel}/adapter'
        adapter.mkdir(parents=True)
        (adapter / 'main.py').write_text('async def run(binding): pass\n')
        binding_file = registry.parent / f'{channel}.json'
        binding = dict(version=1, channel=channel, executor_id=f'test-{channel}',
                       agent_id='00000000-0000-0000-0000-000000000001', profile_slot=channel,
                       release=target, adapter_root='/' + str(adapter.relative_to(root)),
                       entrypoint='main:run', adapter_files={'main.py': digest((adapter / 'main.py').read_bytes())},
                       receipt=f'/run/lead-executors/{channel}/receipt/boot.json')
        binding_file.write_text(json.dumps(binding))
        binding_file.chmod(0o600)
        process_binding = root / str(binding_file).lstrip('/')
        process_binding.parent.mkdir(parents=True)
        process_binding.write_bytes(binding_file.read_bytes())
        process_binding.chmod(0o600)
        receipt = root / binding['receipt'].lstrip('/')
        receipt.parent.mkdir(parents=True, mode=0o700)
        proof = dict(version=1, channel=channel, executor_id=binding['executor_id'], revision=revision,
                     pid=int(pid), invocation=invocation, core_files=core_files, adapter_files=binding['adapter_files'],
                     binding_sha256=digest(json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()),
                     boot_sha256=core_files['channel_executor.py'])
        receipt.write_text(json.dumps(proof))
        receipt.chmod(0o600)
        (proc / 'exe').symlink_to(interpreter)
        process_python = root / 'run/lead-home/bin/py/bin/python3.12'
        process_python.parent.mkdir(parents=True)
        process_python.symlink_to(interpreter)
        host_receipt = hook.STORE / 'executors' / channel / 'receipt'
        host_data = host_receipt.with_name('data')
        host_receipt.mkdir(parents=True, mode=0o700)
        host_data.mkdir(mode=0o700)
        process_data = root / 'run/lead-home/actor-home'
        process_data.mkdir(parents=True)
        writable = [(str(host_data), '/run/lead-home/actor-home'),
                    (str(host_receipt), str(Path(binding['receipt']).parent))]
        writable_identity[(process_data, host_data)] = True
        writable_identity[(receipt.parent, host_receipt)] = True
        (proc / 'mountinfo').write_text(
            f'40 10 1:8 /data /run/lead-home/actor-home rw - ext4 disk rw\n'
            f'41 10 1:9 /receipt {Path(binding["receipt"]).parent} rw - ext4 disk rw\n'
            '10 1 1:3 / / ro - ext4 disk ro\n'
            '20 10 1:4 /py /run/lead-home/bin/py ro - ext4 disk rw\n'
            f'30 20 1:1 / {target} ro - squashfs image ro\n'
            f'31 20 1:2 /adapter {binding["adapter_root"]} ro - ext4 disk rw\n')
        (proc / 'cmdline').write_bytes(b'\0'.join(s.encode() for s in [
            '/run/lead-home/bin/py/bin/python3.12', '-I', '-B', target + '/services/api/lead_app/channel_executor.py', str(binding_file)]) + b'\0')
        props[service] = dict(MainPID=pid, InvocationID=invocation, ActiveState='active',
                              BindPaths=' '.join(source + ':' + target + ':rbind' for source, target in writable))
        entry = dict(service=service, channel=channel, binding=str(binding_file))
        layout = registry.parent / (channel + '-layout.json')
        layout.write_text(json.dumps(dict(version=1, **entry, writable_bindings=writable)))
        layout.chmod(0o600)
        entries.append(entry)
        bindings[channel], proofs[channel] = binding, proof
    registry.write_text(json.dumps(dict(version=1, executors=entries)))
    registry.chmod(0o600)
    monkeypatch.setattr(hook, 'EXECUTOR_REGISTRY', registry, raising=False)
    monkeypatch.setattr(hook, 'PROC', proc_base, raising=False)
    monkeypatch.setattr(hook, 'image', lambda sha: hook.STORE / sha)
    monkeypatch.setattr(hook, 'property_of', lambda service, prop: props[service][prop])
    # We cannot chown root or bind mount in this unprivileged unit-test environment.
    actual_lstat, actual_samefile = Path.lstat, Path.samefile
    def lstat(path):
        st = actual_lstat(path)
        # The host's temporary-directory ancestors are outside the synthetic node.
        # Keep actual modes and symlink types within tmp_path, including its root.
        mode = stat.S_IFDIR | 0o755 if path in tmp_path.parents else st.st_mode
        return SimpleNamespace(st_mode=mode, st_uid=0, st_size=st.st_size)
    monkeypatch.setattr(Path, 'lstat', lstat)
    def samefile(path, other):
        if (path, other) in writable_identity:
            return True
        if path in [proc_base / str(i) / 'root' / bindings[c]['release'].lstrip('/')
                    for i, c in enumerate(('telegram', 'reddit'), 10)] and other == release:
            return True
        return actual_samefile(path, other)
    monkeypatch.setattr(Path, 'samefile', samefile)
    return SimpleNamespace(hook=hook, revision=revision, registry=registry, entries=entries,
                           bindings=bindings, proofs=proofs, props=props, proc=proc_base,
                           save=lambda p, data: p.write_text(json.dumps(data)))


def test_executor_registry_attests_both_independent_processes(executors):
    e = executors
    proofs = e.hook.verify_executors(e.revision)
    assert {p['channel'] for p in proofs} == {'telegram', 'reddit'}
    assert {p['pid'] for p in proofs} == {10, 11}


@pytest.mark.parametrize('fault', ['missing-channel', 'duplicate-service', 'duplicate-executor', 'shared-pid',
                                    'wrong-channel', 'unsafe-unit', 'unsafe-registry', 'symlink-registry',
                                    'writable-binding', 'binding-drift', 'stale-receipt', 'public-receipt',
                                    'wrong-revision', 'core-drift', 'boot-drift', 'adapter-drift',
                                    'extra-pyc', 'extra-so', 'adapter-symlink', 'core-overlay', 'adapter-overlay',
                                    'writable-adapter', 'wrong-mount', 'wrong-launcher'])
def test_executor_evidence_rejects_drift(executors, monkeypatch, fault):
    e = executors
    b = e.bindings['telegram']
    root = e.proc / '10/root'
    receipt = root / b['receipt'].lstrip('/')
    adapter = root / b['adapter_root'].lstrip('/')
    core = root / b['release'].lstrip('/') / 'services/api/lead_app'
    binding_file = Path(e.entries[0]['binding'])
    if fault == 'missing-channel':
        e.entries.pop()
    if fault == 'duplicate-service':
        e.entries[1]['service'] = e.entries[0]['service']
    if fault == 'duplicate-executor':
        other = e.bindings['reddit']
        other['executor_id'] = b['executor_id']
        e.save(Path(e.entries[1]['binding']), other)
    if fault == 'shared-pid':
        e.props[e.entries[1]['service']]['MainPID'] = '10'
    if fault == 'wrong-channel':
        e.entries[0]['channel'] = 'reddit'
    if fault == 'unsafe-unit':
        e.entries[0]['service'] = '../other.service'
    if fault == 'unsafe-registry':
        e.registry.chmod(0o666)
    if fault == 'writable-binding':
        binding_file.chmod(0o666)
    if fault == 'binding-drift':
        (root / str(binding_file).lstrip('/')).write_text('{}')
    if fault == 'stale-receipt':
        e.proofs['telegram']['invocation'] = 'f' * 32
        e.save(receipt, e.proofs['telegram'])
    if fault == 'public-receipt':
        receipt.chmod(0o644)
    if fault == 'wrong-revision':
        (root / b['release'].lstrip('/') / 'REVISION').write_text('b' * 40)
    if fault == 'core-drift':
        (core / 'changed.py').write_text('pass')
    if fault == 'boot-drift':
        e.proofs['telegram']['boot_sha256'] = '0' * 64
        e.save(receipt, e.proofs['telegram'])
    if fault == 'adapter-drift':
        (adapter / 'main.py').write_text('changed')
    if fault == 'extra-pyc':
        (adapter / 'hidden.pyc').write_bytes(b'code')
    if fault == 'extra-so':
        (adapter / 'hidden.so').write_bytes(b'code')
    if fault == 'adapter-symlink':
        (adapter / 'link.py').symlink_to(adapter / 'main.py')
    mounts = e.proc / '10/mountinfo'
    if fault in ('core-overlay', 'adapter-overlay'):
        target = b['release'] if fault == 'core-overlay' else b['adapter_root']
        mounts.write_text(mounts.read_text() + f'33 30 1:3 / {target}/hidden ro - ext4 disk rw\n')
    if fault == 'writable-adapter':
        mounts.write_text(mounts.read_text().replace(f'{b["adapter_root"]} ro', f'{b["adapter_root"]} rw'))
    if fault == 'wrong-mount':
        monkeypatch.setattr(Path, 'samefile', lambda *a: False)
    if fault == 'wrong-launcher':
        (e.proc / '10/cmdline').write_bytes(b'/usr/bin/python3\0-c\0pass\0')
    e.save(e.registry, dict(version=1, executors=e.entries))
    if fault == 'symlink-registry':
        backing = e.registry.with_suffix('.real')
        e.registry.rename(backing)
        e.registry.symlink_to(backing)
    with pytest.raises((e.hook.Refused, ValueError)):
        e.hook.verify_executors(e.revision)


def test_executor_process_rechecked_after_all_evidence(executors, monkeypatch):
    e = executors
    original = e.hook.property_of
    counts = {}
    def changing(service, prop):
        key = service, prop
        counts[key] = counts.get(key, 0) + 1
        if prop == 'InvocationID' and counts[key] > 1:
            return 'f' * 32
        return original(service, prop)
    monkeypatch.setattr(e.hook, 'property_of', changing)
    with pytest.raises(e.hook.Refused, match='changed'):
        e.hook.verify_executors(e.revision)


def test_executor_rejects_source_ancestor_symlink(executors):
    e = executors
    root = e.proc / '10/root' / e.bindings['telegram']['release'].lstrip('/')
    services = root / 'services'
    moved = root / 'elsewhere'
    services.rename(moved)
    services.symlink_to(moved, target_is_directory=True)
    with pytest.raises(e.hook.Refused, match='redirected'):
        e.hook.verify_executors(e.revision)


def test_executor_cli_is_separate_from_application_verification(executors, monkeypatch, capsys):
    import json
    e = executors
    monkeypatch.setattr(e.hook.sys, 'argv', ['hook', 'executors', e.revision])
    monkeypatch.setattr(e.hook, 'verify', lambda *a: pytest.fail('application verifier must stay separate'))
    monkeypatch.setattr(e.hook, 'deploy', lambda *a: pytest.fail('executor verification is read only'))
    e.hook.main()
    assert {proof['channel'] for proof in json.loads(capsys.readouterr().out)} == {'telegram', 'reddit'}


def test_executor_rechecks_mount_namespace_after_evidence(executors, monkeypatch):
    e = executors
    original = e.hook.property_of
    reads = 0
    def changing(service, prop):
        nonlocal reads
        if service == e.entries[0]['service'] and prop == 'InvocationID':
            reads += 1
            if reads == 2:
                mounts = e.proc / '10/mountinfo'
                mounts.write_text(mounts.read_text() + '50 20 1:9 / /new-mount ro - tmpfs tmpfs ro\n')
        return original(service, prop)
    monkeypatch.setattr(e.hook, 'property_of', changing)
    with pytest.raises(e.hook.Refused, match='mount.*changed'):
        e.hook.verify_executors(e.revision)


@pytest.mark.parametrize('registered', [False, True])
def test_final_verify_cli_attests_registered_executors(hook, monkeypatch, tmp_path, registered):
    registry = tmp_path / 'executors.json'
    if registered:
        registry.write_text('{}')
    monkeypatch.setattr(hook, 'EXECUTOR_REGISTRY', registry)
    calls = []
    monkeypatch.setattr(hook, 'verify', lambda sha: calls.append(('application', sha)))
    monkeypatch.setattr(hook, 'verify_executors', lambda sha: calls.append(('executors', sha)))
    monkeypatch.setattr(hook.sys, 'argv', ['hook', 'verify', 'a' * 40])
    hook.main()
    assert calls == [('application', 'a' * 40)] + ([('executors', 'a' * 40)] if registered else [])


def test_final_verify_cli_rejects_broken_registry_symlink(hook, monkeypatch, tmp_path):
    registry = tmp_path / 'executors.json'
    registry.symlink_to(tmp_path / 'missing')
    monkeypatch.setattr(hook, 'EXECUTOR_REGISTRY', registry)
    monkeypatch.setattr(hook, 'verify', lambda sha: None)
    monkeypatch.setattr(hook, 'image', lambda sha: hook.STORE / sha)
    core = hook.STORE / ('a' * 40) / 'services/api/lead_app'
    core.mkdir(parents=True)
    (core / 'channel_executor.py').write_text('# fixture')
    monkeypatch.setattr(hook.sys, 'argv', ['hook', 'verify', 'a' * 40])
    with pytest.raises(SystemExit):
        hook.main()


@pytest.mark.parametrize('fault', ['argv', 'actual-exe', 'writable-source', 'writable-mount'])
def test_executor_requires_trusted_actual_interpreter(executors, fault):
    e = executors
    proc = e.proc / '10'
    if fault == 'argv':
        command = proc / 'cmdline'
        command.write_bytes(command.read_bytes().replace(
            b'/run/lead-home/bin/py/bin/python3.12', b'/tmp/untrusted-replacement-interpreter', 1))
    elif fault == 'actual-exe':
        replacement = e.proc / 'replacement-python'
        replacement.write_bytes(b'untrusted executable')
        (proc / 'exe').unlink()
        (proc / 'exe').symlink_to(replacement)
    elif fault == 'writable-source':
        e.hook.EXECUTOR_PYTHON_SOURCE.chmod(0o777)
    else:
        mounts = proc / 'mountinfo'
        mounts.write_text(mounts.read_text().replace('/run/lead-home/bin/py ro', '/run/lead-home/bin/py rw'))
    with pytest.raises(e.hook.Refused, match='interpreter'):
        e.hook.verify_executors(e.revision)


def test_executor_accepts_root_owned_interpreter_symlink(executors, monkeypatch):
    e = executors
    original = e.hook.EXECUTOR_PYTHON_SOURCE
    link = original.with_name('python-link')
    link.symlink_to(original)
    monkeypatch.setattr(e.hook, 'EXECUTOR_PYTHON_SOURCE', link)
    assert len(e.hook.verify_executors(e.revision)) == 2


def test_single_executor_can_be_verified_before_registry_registration(executors):
    e = executors
    e.registry.unlink()
    proof = e.hook.verify_executor_entry(e.revision, e.entries[0])
    assert proof['channel'] == 'telegram'
    assert proof['pid'] == 10


@pytest.fixture
def provisioning(hook, monkeypatch, tmp_path):
    import hashlib
    import json
    import os
    from types import SimpleNamespace

    h = hook
    channel, sha = 'telegram', 'b' * 40
    config_root = tmp_path / 'etc/lead-home-release'
    config_root.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(h, 'EXECUTOR_REGISTRY', config_root / 'executors.json')
    systemd = tmp_path / 'etc/systemd/system'
    systemd.mkdir(parents=True)
    monkeypatch.setattr(h, 'EXECUTOR_SYSTEMD', systemd, raising=False)
    template = Path(__file__).resolve().parents[1] / 'services/channel_adapters/systemd/executor.conf.in'
    bundled = h.STORE / sha / 'services/channel_adapters/systemd/executor.conf.in'
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(template.read_bytes())
    source = b'async def run(binding): pass\n'
    manifest = {'entry.py': hashlib.sha256(source).hexdigest()}
    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    digest = hashlib.sha256(canonical(manifest)).hexdigest()
    base = h.STORE / 'executors' / channel
    adapter = base / 'adapters' / digest
    adapter.mkdir(parents=True)
    (adapter / 'entry.py').write_bytes(source)
    (config_root / f'{channel}.env').write_text('LEAD_ENV_FILE=/run/site/.env\n')
    (config_root / f'{channel}.env').chmod(0o600)
    preserved = tmp_path / 'preserved.env'
    preserved.write_text('fixture=true\n')
    preserved.chmod(0o600)
    service = 'lead-fixture-telegram.service'
    fragment = systemd / service
    fragment.write_text('[Service]\nExecStart=/legacy\n')
    plan = dict(version=1, channel=channel, service=service, core_mode='independent',
                binding=dict(version=1, channel=channel, executor_id='fixture-telegram',
                             agent_id='00000000-0000-0000-0000-000000000001', profile_slot='fixture',
                             release='/run/lead-executor/release', adapter_root='/run/lead-executor/adapter',
                             entrypoint='entry:run', adapter_files=manifest,
                             receipt='/run/lead-executor/receipt/boot.json'),
                preserved_readonly_bindings=[dict(source=str(preserved), target='/run/site/.env')],
                expected=dict(fragment_sha256=hashlib.sha256(fragment.read_bytes()).hexdigest(), dropins={},
                              binding_sha256=None, selector=None))
    plan_path = config_root / f'{channel}-provision.json'
    plan_path.write_bytes(canonical(plan))
    plan_path.chmod(0o600)
    props = dict(FragmentPath=str(fragment), DropInPaths='', MainPID='10', InvocationID='a' * 32,
                 ActiveState='active', User='fixture', Group='fixture',
                 ExecStart='{ path=/legacy ; argv[]=/legacy ; ignore_errors=no ; pid=10 ; status=0/0 }',
                 BindReadOnlyPaths=str(preserved) + ':/run/site/.env:rbind', BindPaths='', EnvironmentFiles='')
    commands, holds = [], []
    dropin = systemd / (service + '.d') / 'zzzz-lead-executor.conf'
    def run(*args, **kwargs):
        commands.append(args)
        if args[:2] == ('systemctl', 'stop'):
            props['ActiveState'] = 'inactive'
        if args[:2] == ('systemctl', 'start'):
            props['ActiveState'] = 'active'
        if args[:2] == ('systemctl', 'daemon-reload'):
            executable = '/new' if dropin.exists() else '/legacy'
            props['ExecStart'] = '{ path=' + executable + ' ; argv[]=' + executable + ' ; ignore_errors=no ; pid=20 ; status=0/0 }'
        return ''
    monkeypatch.setattr(h, 'run', run)
    monkeypatch.setattr(h, 'property_of', lambda service, prop: props[prop])
    monkeypatch.setattr(h, 'image', lambda rev: h.STORE / rev)
    monkeypatch.setattr(h, 'current', lambda: sha)
    monkeypatch.setattr(h, 'executor_maintenance', lambda rev, aid: holds.append((rev, aid)), raising=False)
    monkeypatch.setattr(h, 'verify_executor_entry', lambda rev, entry: dict(channel=entry['channel'], revision=rev))
    monkeypatch.setattr(h, 'wait_executor_entry', lambda rev, entry: h.verify_executor_entry(rev, entry), raising=False)
    monkeypatch.setattr(h.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(h.os, 'chown', lambda *a, **kw: None)
    actual = Path.lstat
    def lstat(path):
        value = actual(path)
        # Model only strict external ancestors as trusted, not fixture directories.
        mode = stat.S_IFDIR | 0o755 if path in tmp_path.parents else value.st_mode
        return SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0, st_size=value.st_size)
    monkeypatch.setattr(Path, 'lstat', lstat)
    import grp
    import pwd
    monkeypatch.setattr(pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.getuid() or 1234, pw_gid=os.getgid() or 1234))
    monkeypatch.setattr(grp, 'getgrnam', lambda name: SimpleNamespace(gr_gid=os.getgid() or 1234))
    return SimpleNamespace(h=h, sha=sha, channel=channel, service=service, plan=plan, path=plan_path,
                           base=base, commands=commands, props=props, holds=holds, dropin=dropin,
                           config=config_root / f'{channel}.json', adapter=adapter,
                           save=lambda: plan_path.write_bytes(canonical(plan)))


def test_provision_executor_is_single_service_and_durable(provisioning):
    import json
    p = provisioning
    result = p.h.provision_executor(p.sha, p.path)
    assert result['channel'] == p.channel
    assert json.loads(p.config.read_text()) == p.plan['binding']
    assert (p.base / 'current').readlink() == p.h.STORE / p.sha
    assert 'ExecStart=/run/lead-home/bin/py/bin/python3.12 -I -B' in p.dropin.read_text()
    assert not p.h.EXECUTOR_REGISTRY.exists()
    assert p.holds
    actions = [a for a in p.commands if a[:2] in (('systemctl', 'stop'), ('systemctl', 'start'))]
    assert actions == [('systemctl', 'stop', p.service), ('systemctl', 'start', p.service)]
    journal = json.loads((p.base / 'journal' / (p.sha + '.json')).read_text())
    assert journal['state'] == 'verified'
    count = len(actions)
    p.h.provision_executor(p.sha, p.path)
    assert len([a for a in p.commands if a[:2] in (('systemctl', 'stop'), ('systemctl', 'start'))]) == count


@pytest.mark.parametrize('fault', ['bad-schema', 'unsafe-plan', 'adapter-drift', 'unit-drift',
                                  'overlay', 'other-current', 'not-held'])
def test_provision_rejects_before_service_mutation(provisioning, monkeypatch, fault):
    p = provisioning
    if fault == 'bad-schema':
        p.plan['arbitrary_output'] = '/etc/unrelated'
    if fault == 'unsafe-plan':
        p.path.chmod(0o666)
    if fault == 'adapter-drift':
        (p.adapter / 'hidden.pyc').write_bytes(b'code')
    if fault == 'unit-drift':
        p.plan['expected']['fragment_sha256'] = '0' * 64
    if fault == 'overlay':
        p.plan['preserved_readonly_bindings'][0]['target'] = '/run/lead-executor/release/patched.py'
    if fault == 'other-current':
        p.plan['core_mode'] = 'main-current'
        monkeypatch.setattr(p.h, 'current', lambda: 'a' * 40)
    if fault == 'not-held':
        def unavailable(*args):
            raise p.h.Refused('maintenance unavailable')
        monkeypatch.setattr(p.h, 'executor_maintenance', unavailable)
    p.save()
    with pytest.raises((p.h.Refused, ValueError)):
        p.h.provision_executor(p.sha, p.path)
    assert not any(a[:2] in (('systemctl', 'stop'), ('systemctl', 'start')) for a in p.commands)
    assert not p.dropin.exists()


def test_provision_rolls_back_files_and_only_same_service(provisioning, monkeypatch):
    import json
    p = provisioning
    old_config = b'{"previous":"exact bytes"}\n'
    p.config.write_bytes(old_config)
    p.config.chmod(0o640)
    import hashlib
    p.plan['expected']['binding_sha256'] = hashlib.sha256(old_config).hexdigest()
    p.save()
    def failed(*args):
        raise p.h.Refused('fixture boot failure')
    monkeypatch.setattr(p.h, 'verify_executor_entry', failed)
    with pytest.raises(p.h.Refused, match='restored'):
        p.h.provision_executor(p.sha, p.path)
    assert p.config.read_bytes() == old_config
    assert p.config.stat().st_mode & 0o777 == 0o640
    assert not p.dropin.exists()
    assert not (p.base / 'current').exists()
    assert p.props['ActiveState'] == 'active'
    assert json.loads((p.base / 'journal' / (p.sha + '.json')).read_text())['state'] == 'rolled-back'
    assert all(a[-1] == p.service for a in p.commands if a[:2] in (('systemctl', 'stop'), ('systemctl', 'start')))


def test_incomplete_provision_resumes_readback_without_replaying(provisioning, monkeypatch):
    import json
    p = provisioning
    p.h.provision_executor(p.sha, p.path)
    journal_path = p.base / 'journal' / (p.sha + '.json')
    journal = json.loads(journal_path.read_text())
    journal['state'] = 'installed'
    journal_path.write_text(json.dumps(journal))
    p.commands.clear()
    def unknown(*args):
        raise p.h.Refused('not yet verified')
    monkeypatch.setattr(p.h, 'verify_executor_entry', unknown)
    with pytest.raises(p.h.Refused, match='incomplete'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(a[0] == 'systemctl' for a in p.commands)


def test_reviewed_executor_image_builder_is_exact(hook, monkeypatch):
    name = 'scripts/build_home_image.sh'
    monkeypatch.setattr(hook, 'run', lambda *a: '7079b3b48ec7d727800c5959ba6f321dfb24c07c')
    assert hook.reviewed_executor_image_files('/mirror', 'a' * 40, {name, 'scripts/arbitrary.py'}) == {name}
    monkeypatch.setattr(hook, 'run', lambda *a: 'other')
    with pytest.raises(hook.Refused, match='image builder'):
        hook.reviewed_executor_image_files('/mirror', 'a' * 40, {name})


def test_independent_executor_can_precede_main_application_cutover(provisioning, monkeypatch):
    p = provisioning
    monkeypatch.setattr(p.h, 'current', lambda: 'a' * 40)
    assert p.h.provision_executor(p.sha, p.path)['revision'] == p.sha


def test_maintenance_check_uses_only_trusted_readonly_query(hook, monkeypatch, tmp_path):
    site = tmp_path / 'tools/home_live_maintenance.py'
    site.parent.mkdir()
    site.write_text('# trusted fixture')
    monkeypatch.setattr(hook, 'TOOLS', site.parent)
    monkeypatch.setattr(hook, 'executor_host_file', lambda p: b'trusted')
    programs = []
    def run(*args):
        assert args[:3] == ('/usr/bin/python3', '-IB', '-c')
        programs.append(args[3])
        return 't'
    monkeypatch.setattr(hook, 'run', run)
    hook.executor_maintenance('a' * 40, '00000000-0000-0000-0000-000000000001')
    import runpy
    queries = []
    monkeypatch.setattr(runpy, 'run_path', lambda path: {
        'KEY': 'home_release_maintenance', 'BUSY': 'FALSE', 'FINGERPRINT': "jsonb_build_object('version',a.version)",
        'site_sql': lambda query: queries.append(query) or 't',
    })
    exec(compile(programs[0], '<trusted-maintenance-query>', 'exec'), {})
    assert len(queries) == 1 and queries[0].startswith('SELECT ')
    assert "desired_state='running'" in queries[0]
    assert "home_release_maintenance,sha" in queries[0]
    assert "home_release_maintenance,before" in queries[0]
    assert all(token not in queries[0] for token in ('UPDATE ', 'DELETE ', 'INSERT '))


def test_executor_start_wait_is_bounded_and_readonly(hook, monkeypatch):
    attempts = []
    def verify(sha, entry):
        attempts.append(entry)
        if len(attempts) < 3:
            raise FileNotFoundError('boot receipt pending')
        return {'revision': sha}
    monkeypatch.setattr(hook, 'verify_executor_entry', verify)
    monkeypatch.setattr(hook.time, 'sleep', lambda seconds: None)
    assert hook.wait_executor_entry('a' * 40, {'channel': 'telegram'}) == {'revision': 'a' * 40}
    assert len(attempts) == 3


def test_registry_registration_requires_both_completed_channels(provisioning, monkeypatch):
    p = provisioning
    p.h.provision_executor(p.sha, p.path)
    with pytest.raises((p.h.Refused, OSError)):
        p.h.register_executors(p.sha)
    assert not p.h.EXECUTOR_REGISTRY.exists()


def test_growth_cutover_delivers_both_channels_before_real_registry_acceptance(provisioning, monkeypatch):
    import copy
    import json
    import shutil

    p = provisioning
    other = copy.deepcopy(p.plan)
    other['channel'] = 'reddit'
    other['service'] = 'lead-fixture-reddit.service'
    other['binding']['channel'] = 'reddit'
    other['binding']['executor_id'] = 'fixture-reddit'
    other['core_mode'] = 'main-current'
    other_path = p.path.with_name('reddit-provision.json')
    other_path.write_text(json.dumps(other))
    other_path.chmod(0o600)
    env = p.path.with_name('reddit.env')
    env.write_bytes(p.path.with_name('telegram.env').read_bytes())
    env.chmod(0o600)
    shutil.copytree(p.base / 'adapters', p.h.STORE / 'executors/reddit/adapters')
    stage = p.h.STORE / ('maintenance-' + p.sha)
    stage.mkdir(mode=0o700)
    (stage / 'growth-cutover.pending').write_text(p.sha + '\n')
    (stage / 'growth-migration.json').write_text(json.dumps({'sha': p.sha, 'old': 'a' * 40}))
    (stage / 'growth-cutover.pending').chmod(0o600)
    (stage / 'growth-migration.json').chmod(0o600)
    helper = stage / 'source/scripts/growth_runtime_upgrade.py'
    helper.parent.mkdir(parents=True)
    helper.write_text('# source-attested operator fixture\n')
    original_run = p.h.run

    def run(*args, **kwargs):
        if args[:3] == ('git', 'hash-object', '--no-filters'):
            return p.h.GROWTH_RUNTIME_FILES['scripts/growth_runtime_upgrade.py']
        if args[0] == p.h.EXECUTOR_PYTHON_SOURCE:
            assert p.h.EXECUTOR_REGISTRY.exists(), 'both real journals must be accepted before finishing'
            (stage / 'growth-cutover.pending').rename(stage / 'growth-cutover.complete')
            return ''
        return original_run(*args, **kwargs)

    monkeypatch.setattr(p.h, 'run', run)
    monkeypatch.setattr(p.h, 'verify_executor_entries', lambda sha, entries, **kw: [
        {'channel': entry['channel'], 'revision': sha} for entry in entries])
    p.h.complete_growth_cutover(p.sha)
    entries = json.loads(p.h.EXECUTOR_REGISTRY.read_text())['executors']
    assert {entry['channel'] for entry in entries} == {'telegram', 'reddit'}
    for channel in ('telegram', 'reddit'):
        journal = p.h.STORE / 'executors' / channel / 'journal' / (p.sha + '.json')
        assert json.loads(journal.read_text())['state'] == 'verified'
    assert (stage / 'growth-cutover.complete').read_text() == p.sha + '\n'


def test_registry_registration_is_final_readonly_service_step(provisioning, monkeypatch):
    import copy
    import json
    import shutil

    p = provisioning
    p.h.provision_executor(p.sha, p.path)
    other = copy.deepcopy(p.plan)
    other['channel'] = 'reddit'
    other['service'] = 'lead-fixture-reddit.service'
    other['binding']['channel'] = 'reddit'
    other['binding']['executor_id'] = 'fixture-reddit'
    other_path = p.path.with_name('reddit-provision.json')
    other_path.write_text(json.dumps(other))
    other_path.chmod(0o600)
    environment = p.path.with_name('reddit.env')
    environment.write_bytes(p.path.with_name('telegram.env').read_bytes())
    environment.chmod(0o600)
    shutil.copytree(p.base / 'adapters', p.h.STORE / 'executors/reddit/adapters')
    p.h.provision_executor(p.sha, other_path)
    entries_seen = []
    def verify_entries(sha, entries, **kwargs):
        entries_seen.append(entries)
        assert {entry['channel'] for entry in entries} == {'telegram', 'reddit'}
        assert len({entry['service'] for entry in entries}) == 2
        return [{'channel': entry['channel'], 'revision': sha} for entry in entries]
    monkeypatch.setattr(p.h, 'verify_executor_entries', verify_entries)
    p.commands.clear()
    proofs = p.h.register_executors(p.sha)
    assert {proof['channel'] for proof in proofs} == {'telegram', 'reddit'}
    assert len(json.loads(p.h.EXECUTOR_REGISTRY.read_text())['executors']) == 2
    assert p.h.EXECUTOR_REGISTRY.stat().st_mode & 0o777 == 0o600
    assert not any(command[0] == 'systemctl' for command in p.commands)
    assert len(entries_seen) == 2
    assert p.h.register_executors(p.sha) == proofs


def test_provision_honors_shared_deployment_lock(provisioning):
    import fcntl
    p = provisioning
    with (p.h.STORE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            p.h.provision_executor(p.sha, p.path)
    assert p.commands == []


def test_provision_inherits_exact_existing_bin_and_data_mounts(provisioning, monkeypatch):
    import socket
    import tempfile
    p = provisioning
    host_bin = p.h.STORE / 'trusted-bin'
    host_bin.mkdir()
    monkeypatch.setattr(p.h, 'EXECUTOR_PYTHON_SOURCE', host_bin / 'py/bin/python3.12')
    data = p.h.STORE / 'app-owned-data'
    data.mkdir(mode=0o777)
    data.chmod(0o777)
    with tempfile.TemporaryDirectory(prefix='lead-sock-', dir='/tmp') as directory, socket.socket(socket.AF_UNIX) as sock:
        sock_path = Path(directory) / 'sock'
        sock.bind(str(sock_path))
        inherited = [dict(source=str(host_bin), target=str(p.h.BIN)),
                     dict(source=str(data), target='/run/lead-home/state'),
                     dict(source=str(sock_path), target='/run/model.sock')]
        p.plan['preserved_readonly_bindings'] = inherited
        p.props['BindReadOnlyPaths'] = ' '.join(item['source'] + ':' + item['target'] + ':rbind' for item in inherited)
        p.save()
        assert p.h.provision_executor(p.sha, p.path)['channel'] == p.channel


def test_provision_refuses_new_data_access_not_in_existing_unit(provisioning):
    p = provisioning
    p.plan['preserved_readonly_bindings'][0]['target'] = '/run/new-private-data'
    p.save()
    with pytest.raises(p.h.Refused, match='existing'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(command[0] == 'systemctl' for command in p.commands)


def test_provision_allows_only_existing_environment_file_at_fixed_env_target(provisioning):
    p = provisioning
    item = p.plan['preserved_readonly_bindings'][0]
    p.props['BindReadOnlyPaths'] = ''
    p.props['EnvironmentFiles'] = item['source'] + ' (ignore_errors=no)'
    item['target'] = '/run/lead-home/model.env'
    p.save()
    assert p.h.provision_executor(p.sha, p.path)['channel'] == p.channel


def test_executor_command_identity_ignores_systemd_runtime_counters(hook):
    first = '{ path=/python ; argv[]=/python -B /entry.py ; ignore_errors=no ; start_time=[Wed] ; stop_time=[n/a] ; pid=10 ; code=(null) ; status=0/0 }'
    next_run = first.replace('[Wed]', '[Thu]').replace('pid=10', 'pid=11').replace('code=(null)', 'code=exited')
    assert hook.executor_command_identity(first) == hook.executor_command_identity(next_run)
    assert hook.executor_command_identity(first) != hook.executor_command_identity(first.replace('/entry.py', '/other.py'))


@pytest.mark.parametrize('fault', ['writable', 'symlink'])
def test_executor_fixture_keeps_unsafe_internal_config_ancestor(executors, fault):
    e = executors
    assert len(e.hook.verify_executors(e.revision)) == 2
    directory = e.registry.parent
    if fault == 'writable':
        directory.chmod(0o777)
    else:
        moved = directory.with_name('redirected-config')
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(e.hook.Refused, match='unsafe executor config directory'):
        e.hook.verify_executors(e.revision)


@pytest.mark.parametrize('fault', ['writable', 'symlink'])
def test_provision_fixture_keeps_unsafe_internal_config_ancestor(provisioning, fault):
    p = provisioning
    assert p.h.executor_provision_plan(p.path, p.sha)[0]['channel'] == p.channel
    directory = p.path.parent
    if fault == 'writable':
        directory.chmod(0o777)
    else:
        moved = directory.with_name('redirected-config')
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(p.h.Refused, match='unsafe executor config directory'):
        p.h.executor_provision_plan(p.path, p.sha)
    assert not any(command[0] == 'systemctl' for command in p.commands)



def test_provision_refuses_private_image_root_before_stopping_service(provisioning):
    p = provisioning
    (p.h.STORE / p.sha).chmod(0o700)
    with pytest.raises(p.h.Refused, match='executor image root'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(command[:2] == ('systemctl', 'stop') for command in p.commands)
    assert not p.dropin.exists()
    assert not p.config.exists()


def test_provision_preserves_all_existing_writable_bindings(provisioning):
    import json
    p = provisioning
    source = p.base / 'original-data'
    source.mkdir()
    original = str(source) + ':/run/lead-home/actor-home:rbind'
    p.props['BindPaths'] = original
    p.h.provision_executor(p.sha, p.path)
    assert 'BindPaths=' + original + '\n' in p.dropin.read_text()
    journal = json.loads((p.base / 'journal' / (p.sha + '.json')).read_text())
    assert journal['previous']['writable_bindings'] == [[str(source), '/run/lead-home/actor-home']]
    assert '\nBindPaths=\n' not in p.dropin.read_text()


def test_provision_cannot_request_new_writable_bindings_in_plan(provisioning):
    p = provisioning
    p.plan['preserved_writable_bindings'] = [{'source': '/private-data', 'target': '/run/new-access'}]
    p.save()
    with pytest.raises(p.h.Refused, match='fields'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(command[:2] == ('systemctl', 'stop') for command in p.commands)


def test_provision_rechecks_writable_mapping_before_stop(provisioning, monkeypatch):
    p = provisioning
    original = p.h.property_of
    calls = 0
    def property_of(service, prop):
        nonlocal calls
        if prop == 'BindPaths':
            calls += 1
            source = 'old-data' if calls == 1 else 'changed-data'
            return str(p.base / source) + ':/run/lead-home/actor-home:rbind'
        return original(service, prop)
    monkeypatch.setattr(p.h, 'property_of', property_of)
    with pytest.raises(p.h.Refused, match='baseline changed'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(command[:2] == ('systemctl', 'stop') for command in p.commands)


def test_empty_inherited_writable_list_never_emits_a_second_reset(provisioning):
    p = provisioning
    p.props['BindPaths'] = ''
    p.h.provision_executor(p.sha, p.path)
    assert '\nBindPaths=\n' not in p.dropin.read_text()
    assert sum(line.startswith('BindPaths=') for line in p.dropin.read_text().splitlines()) == 1


@pytest.mark.parametrize('fault', ['missing-map', 'changed-source', 'readonly-mount', 'wrong-inode', 'missing-layout'])
def test_executor_writable_mounts_require_original_mapping_and_actual_source(executors, monkeypatch, fault):
    e = executors
    service = e.entries[0]['service']
    if fault == 'missing-map':
        e.props[service]['BindPaths'] = e.props[service]['BindPaths'].split(' ', 1)[1]
    elif fault == 'changed-source':
        e.props[service]['BindPaths'] = e.props[service]['BindPaths'].replace('/data:', '/other-data:', 1)
    elif fault == 'readonly-mount':
        mountinfo = e.proc / '10/mountinfo'
        mountinfo.write_text(mountinfo.read_text().replace('/run/lead-home/actor-home rw', '/run/lead-home/actor-home ro'))
    elif fault == 'wrong-inode':
        original = Path.samefile
        def samefile(path, other):
            return False if path.name == 'actor-home' else original(path, other)
        monkeypatch.setattr(Path, 'samefile', samefile)
    else:
        (e.registry.parent / 'telegram-layout.json').unlink()
    with pytest.raises((e.hook.Refused, FileNotFoundError)):
        e.hook.verify_executors(e.revision)


@pytest.mark.parametrize('target', ['/run/lead-home/bin/site', '/run/lead-home/bin/py', '/run/lead-executor/release'])
def test_provision_rejects_writable_code_dependencies(provisioning, target):
    p = provisioning
    p.props['BindPaths'] = str(p.base / 'data') + ':' + target + ':rbind'
    with pytest.raises(p.h.Refused, match='writable binding overlaps'):
        p.h.provision_executor(p.sha, p.path)
    assert not any(command[:2] == ('systemctl', 'stop') for command in p.commands)


def test_writable_layout_survives_shared_application_revision_change(executors, monkeypatch):
    import shutil
    e = executors
    before = {path.name: path.read_bytes() for path in e.registry.parent.glob('*-layout.json')}
    revision = 'b' * 40
    release = e.hook.STORE / revision
    shutil.copytree(e.hook.STORE / e.revision, release, dirs_exist_ok=True)
    (release / 'REVISION').write_text(revision)
    views = set()
    for pid, channel in enumerate(('telegram', 'reddit'), 10):
        view = e.proc / str(pid) / 'root' / e.bindings[channel]['release'].lstrip('/')
        views.add(view)
        (view / 'REVISION').write_text(revision)
        proof = {**e.proofs[channel], 'revision': revision}
        receipt = e.proc / str(pid) / 'root' / e.bindings[channel]['receipt'].lstrip('/')
        e.save(receipt, proof)
    original = Path.samefile
    def samefile(path, other):
        return True if path in views and other == release else original(path, other)
    monkeypatch.setattr(Path, 'samefile', samefile)
    assert {proof['revision'] for proof in e.hook.verify_executors(revision)} == {revision}
    assert before == {path.name: path.read_bytes() for path in e.registry.parent.glob('*-layout.json')}


def test_failed_provision_restores_previous_writable_layout_bytes(provisioning, monkeypatch):
    p = provisioning
    layout = p.config.with_name(p.channel + '-layout.json')
    prior = b'{"operator":"original exact layout bytes"}\n'
    layout.write_bytes(prior)
    layout.chmod(0o600)
    def failed(*args):
        raise p.h.Refused('fixture rejected startup')
    monkeypatch.setattr(p.h, 'verify_executor_entry', failed)
    with pytest.raises(p.h.Refused, match='restored'):
        p.h.provision_executor(p.sha, p.path)
    assert layout.read_bytes() == prior
    assert layout.stat().st_mode & 0o777 == 0o600


def test_unified_compatibility_only_consumes_exact_frozen_files(hook,tmp_path,monkeypatch):
    tools=tmp_path/'tools'
    tools.mkdir()
    (tools/'home_live_maintenance.py').write_text('# fixture')
    monkeypatch.setattr(hook,'TOOLS',tools)
    monkeypatch.setattr(hook,'UNIFIED_BROWSER_FILES',{'scripts/browser_namespace.py':'c'*40})
    monkeypatch.setattr(hook,'unified_cutover_pending',lambda sha:True)
    def run(*args,**kwargs):
        return 'c'*40 if args[0]=='git' else 't'
    monkeypatch.setattr(hook,'run',run)
    assert hook.reviewed_unified_browser('/mirror','b'*40,{'scripts/browser_namespace.py','scripts/arbitrary.py'})=={'scripts/browser_namespace.py'}
    monkeypatch.setattr(hook,'run',lambda *a,**kw:'d'*40)
    with pytest.raises(hook.Refused,match='compatibility review'):
        hook.reviewed_unified_browser('/mirror','b'*40,{'scripts/browser_namespace.py'})


def test_unified_unfrozen_schema_runtime_never_releases(hook,monkeypatch):
    monkeypatch.setattr(hook,'UNIFIED_BROWSER_FILES',{'scripts/browser_namespace.py':'UNFROZEN'})
    with pytest.raises(hook.Refused,match='not frozen'):
        hook.reviewed_unified_browser('/mirror','b'*40,{'scripts/browser_namespace.py'})


def test_ambiguous_cutover_journals_refuse(hook,monkeypatch):
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda sha:True)
    monkeypatch.setattr(hook,'unified_cutover_pending',lambda sha:True)
    with pytest.raises(hook.Refused,match='ambiguous'):
        hook.cutover_kind('b'*40)


def test_unified_preflight_uses_original_hold_and_revision(hook,monkeypatch):
    import json
    old,sha='a'*40,'b'*40
    reads=[]
    monkeypatch.setattr(hook,'current',lambda:old)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda revision:False)
    monkeypatch.setattr(hook,'unified_cutover_pending',lambda revision:True)
    def host_file(path,**kwargs):
        assert path.name=='unified-browser-migration.json'
        return json.dumps({'sha':sha,'old':old}).encode()
    monkeypatch.setattr(hook,'executor_host_file',host_file)
    monkeypatch.setattr(hook,'verify',lambda *a,**kw:reads.append((a,kw)))
    assert hook.release_preflight(sha)==old
    assert reads==[((old,),{'fenced':True,'require_idle':False})]


@pytest.mark.parametrize('failure',[None,'hash','telegram','reddit','finish'])
def test_unified_finalizer_preserves_hold_until_all_consumers_verify(hook,monkeypatch,failure):
    import json
    sha,old='b'*40,'a'*40
    pending=True
    calls=[]
    stage=hook.STORE/('maintenance-'+sha)
    monkeypatch.setattr(hook,'unified_cutover_pending',lambda revision:pending)
    monkeypatch.setattr(hook,'UNIFIED_BROWSER_FILES',{'scripts/unified_browser_upgrade.py':'c'*40})
    def host_file(path,**kwargs):
        if path.name=='unified-browser-migration.json':
            return json.dumps({'sha':sha,'old':old}).encode()
        assert path==stage/'source/scripts/unified_browser_upgrade.py'
        return b'# reviewed source'
    monkeypatch.setattr(hook,'executor_host_file',host_file)
    def provision(revision,path):
        channel=path.name.removesuffix('-provision.json')
        calls.append(channel)
        if failure==channel:
            raise hook.Refused('executor verification failed')
    monkeypatch.setattr(hook,'provision_executor',provision)
    monkeypatch.setattr(hook,'register_executors',lambda revision:calls.append('register'))
    def run(*args,**kwargs):
        nonlocal pending
        if args[0]=='git':
            return 'd'*40 if failure=='hash' else 'c'*40
        assert args==(hook.EXECUTOR_PYTHON_SOURCE,'-I','-B',stage/'source/scripts/unified_browser_upgrade.py',
            'finish','--sha',sha,'--old',old)
        calls.append('finish')
        if failure=='finish':
            raise hook.Refused('readback failed')
        pending=False
        return ''
    monkeypatch.setattr(hook,'run',run)
    if failure:
        with pytest.raises(hook.Refused):
            hook.complete_unified_cutover(sha)
        assert pending
    else:
        hook.complete_unified_cutover(sha)
        assert not pending
    assert calls==([] if failure=='hash' else ['telegram'] if failure=='telegram' else
        ['telegram','reddit'] if failure=='reddit' else ['telegram','reddit','register','finish'])


def test_maintenance_window_does_not_release_unified_upgrade_hold(hook,monkeypatch,tmp_path):
    import json
    tools=tmp_path/'tools'
    tools.mkdir()
    (tools/'home_live_maintenance.py').write_text('# fixture')
    monkeypatch.setattr(hook,'TOOLS',tools)
    monkeypatch.setattr(hook,'growth_cutover_pending',lambda sha:False)
    monkeypatch.setattr(hook,'unified_cutover_pending',lambda sha:True)
    monkeypatch.setattr(hook,'current',lambda:'a'*40)
    monkeypatch.setattr(hook,'verify',lambda *a,**kw:None)
    calls=[]
    def run(*args,**kwargs):
        calls.append(tuple(map(str,args)))
        if args[0]==tools/'home_live_maintenance.py':
            return json.dumps({'active_profiles':0}) if args[1]=='health' else 'idle'
        if args[:2]==('systemctl','show'):
            return 'inactive'
        return ''
    monkeypatch.setattr(hook,'run',run)
    with hook.maintenance_window('a'*40,'b'*40):
        pass
    assert not any('restore' in call for call in calls)
    assert not any(call[:2]==('systemctl','start') for call in calls)
