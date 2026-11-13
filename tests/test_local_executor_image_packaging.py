"""Actual Linux mini-image packaging without rg; no application build/DB/model.

Reuse the exact-source browser-runtime fixture. Only its dependency/browser bytes
are synthetic; Bash, Git archive, GNU tar, package verification, mksquashfs and
unsquashfs are real. A minimal PATH reproduces the hosted packaging environment.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from test_browser_runtime_package import builder
from test_browser_runtime_package import runtime_fixture as runtime_fixture

pytestmark = pytest.mark.skipif(sys.platform != "linux" or not all(
    shutil.which(name) for name in ("mksquashfs", "unsquashfs")),
    reason="requires an explicit Linux mini-image environment with installed squashfs tools")
SOURCE = Path(__file__).resolve().parents[1]


@pytest.fixture
def mini_package(runtime_fixture, tmp_path):
    checkout, _, runtime = runtime_fixture
    for name in ("services/api/lead_app/config.py", "services/local-executor/package.json",
                 "apps/web/app/page.tsx", "infra/home-browser-frames/frame-live.cjs",
                 "scripts/backup.py", "infra/alembic.ini", "infra/compose.yaml", "infra/migrations/env.py"):
        path = checkout/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")
    (checkout/"scripts/build_browser_runtime.py").write_bytes((SOURCE/"scripts/build_browser_runtime.py").read_bytes())
    subprocess.run(["git", "add", "."], cwd=checkout, check=True)
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
        "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", "commit", "-qm", "mini image"], cwd=checkout, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
    original = builder.sources(checkout, sha)
    packages = builder.linux_requirements(*(original[name].decode() for name in builder.SOURCE_FILES[:2]))
    builder.write_manifest(runtime, sha, original, packages,
        {"python": {"required_minor": "3.12", "cache_tag": "cpython-312"},
         "chromium_version": "fixture", "native_smoke": True}, "fixture")
    (checkout/"apps/web/.next").mkdir()
    (checkout/"apps/web/.next/BUILD_ID").write_text("fixture")
    (checkout/"apps/web/node_modules/next").mkdir(parents=True)
    tools = tmp_path/"no-rg-bin"
    tools.mkdir()
    for name in ("bash", "git", "tar", "gzip", "cp", "mv", "rm", "mkdir", "rmdir", "chmod", "mktemp", "mksquashfs", "python3"):
        target = sys.executable if name == "python3" else shutil.which(name)
        assert target, name
        (tools/name).symlink_to(Path(target).resolve())
    assert shutil.which("rg", path=str(tools)) is None
    return checkout, sha, runtime, {**os.environ, "PATH": str(tools)}


def connector_package(path, sha, defect):
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as archive:
        def regular(name, body):
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(body))
        if defect == "symlink_cli":
            entry = tarfile.TarInfo("package/dist/cli.js")
            entry.type, entry.linkname = tarfile.SYMTYPE, "../../outside"
            archive.addfile(entry)
        else:
            # CLI first, then >64KiB of listing data. A grep -q reader exits
            # before GNU tar finishes writing, exposing pipefail/SIGPIPE.
            regular("package/dist/cli.js", b"#!/usr/bin/env node\n")
        regular("package/REVISION", (("0"*40 if defect == "wrong_revision" else sha)+"\n").encode())
        for index in range(2048):
            regular(f"package/dist/padding/{index:05d}-"+"x"*66+".js", b"// fixture\n")
        if defect == "duplicate_cli":
            regular("package/dist/cli.js", b"different CLI\n")
        if defect == "duplicate_revision":
            regular("package/REVISION", (sha+"\n").encode())


@pytest.mark.parametrize("defect", [None, "wrong_revision", "duplicate_revision", "duplicate_cli", "symlink_cli"])
def test_linux_packaging_consumes_full_connector_without_rg(mini_package, tmp_path, defect):
    checkout, sha, runtime, environment = mini_package
    connector, image = tmp_path/"connector.tgz", tmp_path/"image.squashfs"
    connector_package(connector, sha, defect)
    result = subprocess.run(["bash", str(SOURCE/"scripts/build_home_image.sh"), sha, str(image), str(runtime), str(connector)],
        cwd=checkout, env=environment, capture_output=True, text=True, timeout=40)
    if defect:
        assert result.returncode not in (0, 127, 141), result.stderr
        assert not image.exists(), "invalid connector must fail before image compression"
        return
    assert result.returncode == 0, json.dumps({"exit": result.returncode, "stderr": result.stderr[-2000:]})
    assert image.is_file()
    assert subprocess.check_output(["unsquashfs", "-cat", str(image), "REVISION"], text=True) == sha+"\n"
    packed = subprocess.check_output(["unsquashfs", "-cat", str(image), "services/local-executor/lead-local-executor.tgz"])
    assert packed == connector.read_bytes(), "the image must retain the same validated package bytes"
