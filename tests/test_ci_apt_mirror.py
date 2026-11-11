"""All four authenticated suites must pass before the runner mirror changes."""

import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_apt_mirror", Path(__file__).resolve().parents[1] / ".github/scripts/ci_apt_mirror.py"
)
mirror = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mirror)


@pytest.fixture
def runner(monkeypatch, tmp_path):
    monkeypatch.setattr(mirror, "guard", lambda: None)
    path = tmp_path / "apt-mirrors.txt"
    path.write_text("https://archive.ubuntu.com/ubuntu/\tpriority:1\n")
    path.chmod(0o644)
    monkeypatch.setattr(mirror, "MIRRORS", path)
    real_stat = Path.lstat

    def owned(candidate):
        info = real_stat(candidate)
        if candidate in (path, path.parent):
            fields = list(info)
            fields[4] = 0
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(Path, "lstat", owned)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "curl":
            suite = command[-1].split("/")[-2]
            Path(command[command.index("--output") + 1]).write_text("unverified " + suite)
        else:
            source = Path(command[-1])
            suite = source.read_text().split()[-1]
            Path(command[command.index("--output") + 1]).write_text(
                f"Origin: Ubuntu\nSuite: {suite}\nCodename: noble\n"
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(mirror.subprocess, "run", run)
    return path, calls


def test_success_replaces_once_after_all_four_signatures(runner, monkeypatch):
    path, calls = runner
    original = os.replace
    writes = []

    def replace(source, target):
        assert len([call for call in calls if call[0] == "gpgv"]) == 4
        writes.append(target)
        original(source, target)

    monkeypatch.setattr(mirror.os, "replace", replace)
    mirror.main()
    assert writes == [path]
    assert path.read_text() == mirror.MIRROR + "/\tpriority:1\n"
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("failure", ["download", "signature", "suite", "duplicate", "unsigned_suffix"])
def test_any_suite_failure_leaves_existing_mirror_unchanged(runner, monkeypatch, failure):
    path, _ = runner
    before = path.read_bytes()
    original = mirror.subprocess.run

    def failing(command, **kwargs):
        chosen = "noble-security" in command[-1]
        if chosen and ((failure == "download" and command[0] == "curl")
                       or (failure == "signature" and command[0] == "gpgv")):
            raise subprocess.CalledProcessError(1, command)
        result = original(command, **kwargs)
        if chosen and command[0] == "gpgv":
            output = Path(command[command.index("--output") + 1])
            if failure == "suite":
                output.write_text("Origin: Ubuntu\nSuite: jammy\nCodename: noble\n")
            if failure == "duplicate":
                output.write_text(output.read_text() + "Suite: noble-security\n")
            if failure == "unsigned_suffix":
                # Raw download looks right; authenticated plaintext does not.
                Path(command[-1]).write_text("Origin: Ubuntu\nSuite: noble-security\nCodename: noble\n")
                output.write_text("Origin: Ubuntu\nSuite: jammy\nCodename: noble\n")
        return result

    monkeypatch.setattr(mirror.subprocess, "run", failing)
    with pytest.raises((RuntimeError, subprocess.SubprocessError)):
        mirror.main()
    assert path.read_bytes() == before


def test_invalid_environment_rejects_before_network(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setattr(mirror.subprocess, "run", lambda *args, **kwargs: pytest.fail("network reached"))
    with pytest.raises(RuntimeError, match="hosted Linux"):
        mirror.main()


def test_symlink_mirror_is_refused_before_network(runner, monkeypatch, tmp_path):
    path, calls = runner
    path.unlink()
    path.symlink_to(tmp_path / "other")
    with pytest.raises(RuntimeError, match="unsafe"):
        mirror.main()
    assert calls == []



def test_existing_group_writable_mirror_is_refused(runner):
    path, calls = runner
    path.chmod(0o664)
    with pytest.raises(RuntimeError, match="unsafe"):
        mirror.main()
    assert calls == []


def test_concurrent_mirror_change_is_preserved(runner, monkeypatch):
    path, _ = runner
    original = mirror.verified_suite

    def verified(suite):
        result = original(suite)
        if suite == "noble-security":
            path.write_text("concurrent operator edit")
        return result

    monkeypatch.setattr(mirror, "verified_suite", verified)
    with pytest.raises(RuntimeError, match="changed during verification"):
        mirror.main()
    assert path.read_text() == "concurrent operator edit"
