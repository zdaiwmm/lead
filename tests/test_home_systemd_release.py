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
