"""Authenticated ZIP reuse never replaces current CI/provenance or node checks."""

import copy
import hashlib
import importlib
import io
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SHA = "a" * 40


@pytest.fixture
def build(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("prepare_home_image")
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("lead.squashfs", b"synthetic squashfs")
    raw = stream.getvalue()
    run = dict(id=7, run_attempt=1, head_sha=SHA, head_branch="main",
               path=".github/workflows/ci.yml", event="push", status="completed",
               conclusion="success")
    artifact = dict(id=41, name=f"lead-home-{SHA}-1", expired=False,
                    workflow_run=dict(id=7, head_sha=SHA), size_in_bytes=len(raw),
                    digest="sha256:" + hashlib.sha256(raw).hexdigest())
    b = SimpleNamespace(module=module, run=run, artifact=artifact, raw=raw,
                        downloads=0, calls=[], revision=SHA + "\n", after_extract=lambda: None,
                        jobs=[dict(name=n, conclusion="success")
                              for n in ("backend", "frontend", "namespace")],
                        cache=tmp_path / "cache", stage=tmp_path / "stage")

    def get(path):
        b.calls.append(path)
        if path == f"compare/{SHA}...main":
            return {"merge_base_commit": {"sha": SHA}}
        if path.startswith("actions/workflows/ci.yml/runs?"):
            return dict(total_count=1, workflow_runs=[copy.deepcopy(b.run)])
        if path.startswith("actions/runs/") and "/jobs?" in path:
            return dict(total_count=3, jobs=copy.deepcopy(b.jobs))
        if path.startswith("actions/runs/") and "/artifacts?" in path:
            return dict(total_count=1, artifacts=[copy.deepcopy(b.artifact)])
        raise AssertionError(path)

    def download(command, *, stdout, check):
        assert command == ["gh", "api", f"repos/zdaiwmm/lead/actions/artifacts/{b.artifact['id']}/zip"]
        assert check
        b.downloads += 1
        stdout.write(b.raw)

    def revision(command, *, text):
        assert command[:2] == ["unsquashfs", "-cat"] and command[-1] == "REVISION"
        assert text
        b.after_extract()
        return b.revision

    monkeypatch.setattr(module.subprocess, "run", download)
    monkeypatch.setattr(module.subprocess, "check_output", revision)
    b.get = get
    b.prepare = lambda **kw: module.prepare(SHA, b.stage, get=get, **kw)
    return b


def test_cache_reuses_zip_but_rechecks_ci_and_revision(build):
    first = build.prepare(cache_dir=build.cache)
    build.calls.clear()
    assert build.prepare(cache_dir=build.cache) == first
    assert build.downloads == 1
    assert sum("/ci.yml/runs?" in c for c in build.calls) == 2
    assert sum("/artifacts?" in c for c in build.calls) == 2
    assert (build.stage / f"{SHA}.squashfs").read_bytes() == b"synthetic squashfs"
    assert len(list(build.cache.glob("*.zip"))) == 1


def test_default_does_not_use_cache_or_trust_preexisting_staging(build):
    build.prepare()
    build.prepare()
    assert build.downloads == 2
    assert not build.cache.exists()


def test_operator_archive_mode_needs_no_squashfs_and_retains_provenance(build, monkeypatch):
    def no_extraction(*args, **kwargs):
        raise AssertionError("Operator archive mode must not run unsquashfs")
    monkeypatch.setattr(build.module.subprocess, "check_output", no_extraction)
    first = build.prepare(cache_dir=build.cache, archive_only=True)
    second = build.prepare(cache_dir=build.cache, archive_only=True)
    assert first == second
    assert build.downloads == 1
    assert first["sha"] == SHA and first["ci_run"] == 7 and first["ci_attempt"] == 1
    assert first["digest"] == build.artifact["digest"] and first["id"] == 41
    assert first["revision_verified"] is False
    assert Path(first["archive"]).read_bytes() == build.raw
    assert not list(build.stage.glob("*.squashfs"))


def test_archive_mode_rechecks_latest_ci_before_returning(build):
    get = build.get
    count = 0
    def changing(path):
        nonlocal count
        if "/ci.yml/runs?" in path:
            count += 1
            if count == 2:
                build.run["conclusion"] = "failure"
        return get(path)
    with pytest.raises(ValueError, match="Latest exact main CI"):
        build.module.prepare(SHA, build.stage, get=changing, cache_dir=build.cache,
                             archive_only=True)
    assert not list(build.stage.iterdir())
    assert not list(build.cache.iterdir())


def test_archive_cache_does_not_replace_node_revision_check(build):
    build.prepare(cache_dir=build.cache, archive_only=True)
    build.revision = "b" * 40 + "\n"
    with pytest.raises(ValueError, match="different commit"):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 1


@pytest.mark.parametrize("fault", ["expired", "digest", "size", "sha", "run"])
def test_cached_image_cannot_bypass_live_provenance(build, fault):
    build.prepare(cache_dir=build.cache)
    if fault == "expired":
        build.artifact["expired"] = True
    elif fault == "digest":
        build.artifact["digest"] = None
    elif fault == "size":
        build.artifact["size_in_bytes"] = 0
    else:
        build.artifact["workflow_run"]["head_sha" if fault == "sha" else "id"] = "wrong"
    with pytest.raises(ValueError):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 1


@pytest.mark.parametrize("state", ["failure", "cancelled", "in_progress"])
def test_cached_image_cannot_bypass_new_ci_result(build, state):
    build.prepare(cache_dir=build.cache)
    build.run["conclusion"] = state
    with pytest.raises(ValueError, match="Latest exact main CI"):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 1


def test_skipped_required_security_job_does_not_pass(build):
    build.jobs[-1]["conclusion"] = "skipped"
    with pytest.raises(ValueError, match="Required verification jobs"):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 0


def test_new_attempt_requires_its_own_artifact(build):
    build.prepare(cache_dir=build.cache)
    build.run["run_attempt"] = 2
    build.artifact.update(id=42, name=f"lead-home-{SHA}-2")
    build.prepare(cache_dir=build.cache)
    assert build.downloads == 2
    assert len(list(build.cache.glob("*.zip"))) == 2


def test_corrupt_cached_bytes_fail_without_silent_redownload(build):
    build.prepare(cache_dir=build.cache)
    cached = next(build.cache.glob("*.zip"))
    cached.chmod(0o600)
    cached.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="size/digest"):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 1


@pytest.mark.parametrize("fault", ["ci", "attempt", "expiry", "artifact"])
def test_change_during_preparation_never_publishes_candidate(build, fault):
    def change():
        if fault == "ci":
            build.run["conclusion"] = "failure"
        elif fault == "attempt":
            build.run["run_attempt"] += 1
        elif fault == "expiry":
            build.artifact["expired"] = True
        else:
            build.artifact["id"] += 1
    build.after_extract = change
    with pytest.raises(ValueError):
        build.prepare(cache_dir=build.cache)
    assert not (build.stage / f"{SHA}.squashfs").exists()
    assert not list(build.cache.iterdir())


def test_wrong_revision_is_not_cached_or_staged(build):
    build.revision = "b" * 40 + "\n"
    with pytest.raises(ValueError, match="different commit"):
        build.prepare(cache_dir=build.cache)
    assert not list(build.cache.iterdir())
    assert not list(build.stage.iterdir())


def test_cache_directory_cannot_be_shared_or_symlinked(build, tmp_path):
    build.cache.mkdir(mode=0o755)
    build.cache.chmod(0o755)
    with pytest.raises(ValueError, match="Private staging/cache"):
        build.prepare(cache_dir=build.cache)
    build.cache.rmdir()
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    build.cache.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="Private staging/cache"):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 0


def test_symlink_cache_file_is_not_followed(build, tmp_path):
    build.prepare(cache_dir=build.cache)
    cached = next(build.cache.glob("*.zip"))
    outside = tmp_path / "outside"
    outside.write_bytes(build.raw)
    cached.unlink()
    cached.symlink_to(outside)
    with pytest.raises(OSError):
        build.prepare(cache_dir=build.cache)
    assert build.downloads == 1


def test_cache_limit_uses_normal_download_without_deleting(build, monkeypatch):
    build.module.private_directory(build.cache)
    other = build.cache / "keep"
    other.write_bytes(b"other release")
    monkeypatch.setattr(build.module, "CACHE_LIMIT", 1)
    build.prepare(cache_dir=build.cache)
    build.prepare(cache_dir=build.cache)
    assert build.downloads == 2
    assert other.read_bytes() == b"other release"
    assert list(build.cache.iterdir()) == [other]


def test_interrupted_download_does_not_leave_reusable_partial(build, monkeypatch):
    def interrupted(command, *, stdout, check):
        stdout.write(build.raw[:20])
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(build.module.subprocess, "run", interrupted)
    with pytest.raises(subprocess.CalledProcessError):
        build.prepare(cache_dir=build.cache)
    assert not list(build.cache.iterdir())
    assert not list(build.stage.iterdir())


def test_concurrent_cache_publish_accepts_only_same_authenticated_bytes(build):
    build.prepare(cache_dir=build.cache)
    cached = next(build.cache.glob("*.zip"))
    build.module.save_cache(cached, cached, build.artifact)
    assert len(list(build.cache.iterdir())) == 1
    cached.chmod(0o600)
    cached.write_bytes(b"changed")
    with pytest.raises(ValueError, match="size/digest"):
        build.module.save_cache(cached, cached, build.artifact)


def test_existing_staged_image_must_match(build):
    build.prepare(cache_dir=build.cache)
    staged = build.stage / f"{SHA}.squashfs"
    staged.chmod(0o600)
    staged.write_bytes(b"different")
    with pytest.raises(ValueError, match="different image"):
        build.prepare(cache_dir=build.cache)
