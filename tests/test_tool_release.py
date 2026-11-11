"""Bundle authentication and real filesystem failure/retry behavior without production access."""

import hashlib
import importlib
import io
import json
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
bundle = importlib.import_module("build_tool_bundle")
release = importlib.import_module("tool_release")
installer = importlib.import_module("tool_install")

SHA, APP = "a" * 40, "b" * 40


@pytest.fixture
def source_bundle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", repo], check=True)
    for name in bundle.TOOL_FILES:
        p = repo / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("VALUE=1\n")
    subprocess.run(["git", "-C", repo, "add", "."], check=True)
    subprocess.run(["git", "-C", repo, "-c", "user.name=test", "-c", "user.email=test@example.test",
                    "commit", "-qm", "tools"], check=True)
    sha = subprocess.check_output(["git", "-C", repo, "rev-parse", "HEAD"], text=True).strip()
    out = tmp_path / "tools.tar"
    bundle.build(repo, sha, out)
    return repo, sha, out


def test_deterministic_bundle_is_exact_git_source(source_bundle, tmp_path):
    repo, sha, out = source_bundle
    second = tmp_path / "again.tar"
    bundle.build(repo, sha, second)
    assert out.read_bytes() == second.read_bytes()
    assert set(release.unpack(out.read_bytes(), sha)) == bundle.TOOL_FILES
    with pytest.raises(ValueError, match="another revision"):
        release.unpack(out.read_bytes(), SHA)
    (repo / "scripts/ci_scope.py").write_text("uncommitted")
    with pytest.raises(subprocess.CalledProcessError):
        bundle.build(repo, sha, tmp_path / "dirty.tar")


@pytest.mark.parametrize("fault", ["digest", "link", "duplicate", "path"])
def test_untrusted_bundle_members_rejected(source_bundle, fault):
    _, sha, path = source_bundle
    with tarfile.open(path) as original:
        contents = [(m, original.extractfile(m).read()) for m in original.getmembers()]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as target:
        for info, data in contents:
            if info.name == "scripts/ci_scope.py":
                if fault == "digest":
                    data = b"tampered"
                    info.size = len(data)
                elif fault == "link":
                    info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
                elif fault == "path":
                    info.name = "../ci_scope.py"
                elif fault == "duplicate":
                    target.addfile(info, io.BytesIO(data))
            target.addfile(info, io.BytesIO(data))
    with pytest.raises(ValueError):
        release.unpack(stream.getvalue(), sha)


def test_artifact_download_rechecks_identity_and_transport(source_bundle, monkeypatch):
    _, sha, path = source_bundle
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("lead-tools.tar", path.read_bytes())
    raw = stream.getvalue()
    ci = {"id": 7, "run_attempt": 1}
    item = dict(id=41, name=f"lead-tools-{sha}-1", expired=False, size_in_bytes=len(raw),
                digest="sha256:" + hashlib.sha256(raw).hexdigest(), workflow_run={"id": 7, "head_sha": sha})
    called = []
    def verified(candidate, *, get, kind):
        assert kind == "tools" and candidate == sha
        called.append(True)
        return ci.copy()
    monkeypatch.setattr(release, "verified_run", verified)
    def get(_):
        return {"total_count": 1, "artifacts": [item.copy()]}
    files, proof = release.fetch(sha, get=get, download=lambda _: raw)
    assert set(files) == bundle.TOOL_FILES and proof["artifact"] == 41
    assert len(called) == 2
    with pytest.raises(ValueError, match="transport digest"):
        release.fetch(sha, get=get, download=lambda _: b"wrong")
    item["expired"] = True
    with pytest.raises(ValueError, match="provenance"):
        release.fetch(sha, get=get, download=lambda _: raw)
    item["expired"] = False
    def changed(_):
        ci["run_attempt"] = 2
        return raw
    with pytest.raises(ValueError, match="CI changed"):
        release.fetch(sha, get=get, download=changed)


@pytest.fixture
def install_case(tmp_path):
    tools, store = tmp_path / "tools", tmp_path / "store"
    tools.mkdir(mode=0o700)
    store.mkdir(mode=0o700)
    target = tools / "home_systemd_release.py"
    old, new = b"VALUE=1\n", b"VALUE=2\n"
    target.write_bytes(old)
    target.chmod(0o755)
    def apply(**kwargs):
        return installer.install(tools, store, target.name, new, SHA, installer.digest(old), APP,
                                 current=kwargs.get("current", lambda: APP),
                                 git_source=kwargs.get("git_source", lambda *_: new))
    return tools, store, target, old, new, apply


def test_install_and_retry_preserve_original_backup_and_do_not_rewrite(install_case, monkeypatch):
    tools, store, target, old, new, apply = install_case
    writes = []
    original = installer.atomic_write
    def write(path, data, mode):
        writes.append(path)
        return original(path, data, mode)
    monkeypatch.setattr(installer, "atomic_write", write)
    result = apply()
    assert result["application_switch"] is False and result["service_restarts"] is False
    assert target.read_bytes() == new
    assert Path(result["backup"]).read_bytes() == old
    assert apply() == result
    assert writes.count(target) == 1
    assert (target.stat().st_mode & 0o777) == 0o755
    assert json.loads((store / "tool-backups" / SHA / (target.name + ".json")).read_text())["old"] == installer.digest(old)


def test_failure_after_atomic_swap_restores_old_tool(install_case):
    _, _, target, old, new, apply = install_case
    with pytest.raises(ValueError, match="Post-install"):
        apply(current=lambda: APP if target.read_bytes() == old else "c" * 40)
    assert target.read_bytes() == old
    assert apply()["result"] == "TOOL_INSTALLED"
    assert target.read_bytes() == new


def test_lock_wrong_source_drift_symlink_and_permission_refuse_without_overwrite(install_case):
    import fcntl
    _, store, target, old, _, apply = install_case
    with (store / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            apply()
    with pytest.raises(ValueError, match="application changed"):
        apply(current=lambda: "c" * 40)
    with pytest.raises(ValueError, match="Git source"):
        apply(git_source=lambda *_: b"other")
    target.chmod(0o777)
    with pytest.raises(ValueError, match="Untrusted"):
        apply()
    target.chmod(0o755)
    copy = target.with_suffix(".old")
    target.rename(copy)
    target.symlink_to(copy)
    with pytest.raises(ValueError, match="Untrusted"):
        apply()
    assert copy.read_bytes() == old


def test_remote_program_is_scoped_and_uses_authenticated_installer():
    root = Path(__file__).resolve().parents[1]
    files = {"scripts/tool_install.py": (root / "scripts/tool_install.py").read_bytes(),
             "scripts/home_systemd_release.py": b"VALUE=2"}
    script = release.remote_program(files, SHA, "home_systemd_release.py",
                                    {"old": "0" * 64, "app": APP})
    compile(script, "remote", "exec")
    assert "__MODULE__" not in script and "shell=True" not in script
    with pytest.raises(ValueError):
        release.ssh_command(root / "config", "host; command")


def test_cli_lost_reply_preserves_original_identity_and_resumes_readback(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    files = {"scripts/tool_install.py": (root / "scripts/tool_install.py").read_bytes(),
             "scripts/home_systemd_release.py": b"VALUE=2\n"}
    evidence = tmp_path / "private" / "receipt.json"
    monkeypatch.setattr(release, "fetch", lambda _: (files, {"sha": SHA, "ci_run": 7,
                                                          "ci_attempt": 1, "artifact": 41}))
    old = {"old": "0" * 64, "app": APP}
    new = {"old": hashlib.sha256(files["scripts/home_systemd_release.py"]).hexdigest(), "app": APP}
    state = {"installed": False}
    monkeypatch.setattr(release, "inspect", lambda *_: new.copy() if state["installed"] else old.copy())
    def install(*_):
        if not state["installed"]:
            state["installed"] = True
            raise ConnectionError("lost SSH reply")
        return {"result": "verified prior install"}
    monkeypatch.setattr(release, "run_remote", install)
    args = ["tool_release.py", SHA, "--tool", "home_systemd_release.py", "--install",
            "--ssh-config", str(tmp_path / "config"), "--host", "lead-gateway", "--receipt", str(evidence)]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(ConnectionError):
        release.main()
    assert json.loads(evidence.read_text())["before"] == old
    release.main()
    result = json.loads(evidence.read_text())
    assert result["before"] == old and result["readback"] == "TOOL_READBACK_OK"
    monkeypatch.setattr(sys, "argv", ["other-host" if x == "lead-gateway" else x for x in args])
    with pytest.raises(ValueError, match="destination"):
        release.main()


def test_attended_broker_never_executes_an_unscoped_operation(monkeypatch):
    operator = importlib.import_module("home_release_operator")
    for request in ({"operation": "delete", "sha": SHA}, {"operation": "deploy", "sha": APP}):
        with pytest.raises(ValueError, match="unexpected production command"):
            operator.serve(io.StringIO(json.dumps(request) + "\n"), io.StringIO(), ["unused"], SHA)
    monkeypatch.setattr(operator.subprocess, "run", lambda *_, **__: subprocess.CompletedProcess([], 255))
    with pytest.raises(ConnectionError, match="interrupted"):
        operator.serve(io.StringIO(json.dumps({"operation": "deploy", "sha": SHA}) + "\n"),
                       io.StringIO(), ["unused"], SHA)
