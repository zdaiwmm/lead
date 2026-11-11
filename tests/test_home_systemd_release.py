"""Filesystem cutover, mount identity and lost-status regression cases (no production data)."""

import importlib
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    h = importlib.import_module("home_systemd_release")
    monkeypatch.setattr(h, "STORE", tmp_path)
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


def test_deploy_path_checks_compatibility_and_skips_repeats(hook, monkeypatch):
    new = "b" * 40
    commands = []
    changes = "infra/migrations/new.py\0"

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

    def verify(sha, *, fenced=False):
        assert hook.current() == sha

    def restart(sha):
        verify(sha)
        activations.append(sha)

    monkeypatch.setattr(hook, "verify", verify)
    monkeypatch.setattr(hook, "restart_and_verify", restart)
    with pytest.raises(hook.Refused, match="compatibility"):
        hook.deploy(new)
    assert activations == [] and not any("start" in c for c in commands)

    changes = "services/api/lead_app/app.py\0"
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
