"""An immutable executor image retains its runtime helpers and operator settings."""

import os
import subprocess
from pathlib import Path

import pytest
from lead_app import config


def test_home_image_contains_runtime_dependencies_without_operator_files(tmp_path):
    source = Path(__file__).resolve().parents[1]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    required = (
        "services/api/lead_app/config.py",
        "services/channel_adapters/reddit/entry.py",
        "apps/web/app/page.tsx",
        "infra/home-browser-frames/frame-live.cjs",
        "scripts/browser_namespace.py",
        "scripts/browser_use_runner.py",
        "scripts/backup.py",
        "infra/alembic.ini",
        "infra/compose.yaml",
        "infra/migrations/env.py",
        "infra/migrations/versions/0001_baseline.py",
        "infra/migrations/sql/0001_baseline.sql",
    )
    excluded = (".env", "scripts/operator.py", "infra/private.txt", ".state/config.json")
    for name in (*required, *excluded):
        path = checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name + "\n")
    (checkout / "scripts/browser_namespace.py").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture"],
        cwd=checkout, check=True,
    )
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    (checkout / "services/untracked-private.env").write_text("fixture-local-only\n")
    (checkout / "apps/web/.next").mkdir()
    (checkout / "apps/web/.next/BUILD_ID").write_text("fixture")
    (checkout / "apps/web/node_modules/next").mkdir(parents=True)
    tools = tmp_path / "bin"
    tools.mkdir()
    # The real build prepares the archive; replace only filesystem compression.
    compressor = tools / "mksquashfs"
    compressor.write_text('#!/bin/sh\ncp -R "$1" "$2"\n')
    compressor.chmod(0o755)
    image = tmp_path / "image"
    subprocess.run(
        ["bash", str(source / "scripts/build_home_image.sh"), sha, str(image)],
        cwd=checkout, env={**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]},
        check=True,
    )
    assert (image / "REVISION").read_text() == sha + "\n"
    assert os.access(image / "scripts/browser_namespace.py", os.X_OK)
    assert not (image / "services/untracked-private.env").exists()
    for name in required:
        destination = name.replace("apps/web/", "web/").replace(
            "infra/home-browser-frames/", "home-browser-frames/"
        )
        assert (image / destination).read_text() == name + "\n"
    for name in excluded:
        assert not (image / name).exists()


@pytest.fixture
def environment_root(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    monkeypatch.setattr(config, "ROOT", root)
    monkeypatch.delenv("LEAD_ENV_FILE", raising=False)
    monkeypatch.delenv("LEAD_ENV_FIXTURE", raising=False)
    return root


def test_default_environment_location_and_process_precedence(environment_root, monkeypatch):
    path = environment_root / ".env"
    path.write_text("LEAD_ENV_FIXTURE=file\n")
    assert config.env()["LEAD_ENV_FIXTURE"] == "file"
    monkeypatch.setenv("LEAD_ENV_FIXTURE", "process")
    assert config.env()["LEAD_ENV_FIXTURE"] == "process"
    assert config.environment_file() == path


def test_operator_environment_is_reread_without_mutating_process(environment_root, monkeypatch):
    (environment_root / ".env").write_text("LEAD_ENV_FIXTURE=old-image\n")
    path = environment_root / "operator.env"
    path.write_text("LEAD_ENV_FIXTURE=first\n")
    monkeypatch.setenv("LEAD_ENV_FILE", str(path))
    assert config.env()["LEAD_ENV_FIXTURE"] == "first"
    assert "LEAD_ENV_FIXTURE" not in os.environ
    path.write_text("LEAD_ENV_FIXTURE=second\n")
    assert config.env()["LEAD_ENV_FIXTURE"] == "second"
    monkeypatch.setenv("LEAD_ENV_FIXTURE", "process")
    assert config.env()["LEAD_ENV_FIXTURE"] == "process"
    assert config.environment_file() == path


def test_environment_file_contents_cannot_redirect_loading(environment_root, monkeypatch):
    redirected = environment_root / "redirected.env"
    redirected.write_text("LEAD_ENV_FIXTURE=redirected\n")
    (environment_root / ".env").write_text(
        f"LEAD_ENV_FILE={redirected}\nLEAD_ENV_FIXTURE=original\n"
    )
    assert config.env()["LEAD_ENV_FIXTURE"] == "original"
    assert config.environment_file() == environment_root / ".env"


def test_missing_default_environment_preserves_process_only_configuration(environment_root, monkeypatch):
    monkeypatch.setenv("LEAD_ENV_FIXTURE", "process")
    assert config.env()["LEAD_ENV_FIXTURE"] == "process"


@pytest.mark.parametrize("invalid", ["", "relative.env", "parent", "missing", "directory", "symlink"])
def test_invalid_operator_environment_does_not_fall_back(environment_root, monkeypatch, invalid):
    (environment_root / ".env").write_text("LEAD_ENV_FIXTURE=must-not-fallback\n")
    source = environment_root / "source.env"
    source.write_text("LEAD_ENV_FIXTURE=redirected\n")
    link = environment_root / "link.env"
    link.symlink_to(source)
    values = {
        "": "", "relative.env": "relative.env",
        "parent": str(environment_root / "sub/../source.env"),
        "missing": str(environment_root / "missing.env"),
        "directory": str(environment_root), "symlink": str(link),
    }
    monkeypatch.setenv("LEAD_ENV_FILE", values[invalid])
    with pytest.raises(ValueError, match="LEAD_ENV_FILE"):
        config.env()
