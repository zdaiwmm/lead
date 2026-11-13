"""Opt-in complete CI step with a real mounted SDK, no model/DB/application build.

Run only in an owned disposable Linux mount namespace as root. Supply the real
runtime via LEAD_CI_IMAGE_RUNTIME and a public Python 3.12.3+ executable via
LEAD_CI_IMAGE_PYTHON. A diagnostic reconstruction is not a release artifact;
its source inventory and dependency/ABI differences must be recorded separately.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux" or os.geteuid() != 0
    or os.environ.get("LEAD_DISPOSABLE_RUNNER") != "1" or not os.environ.get("LEAD_CI_IMAGE_RUNTIME"),
    reason="requires explicitly owned Linux mount namespace and real SDK runtime")


def test_complete_nonroot_image_step_from_a_private_checkout(tmp_path):
    runtime = Path(os.environ["LEAD_CI_IMAGE_RUNTIME"])
    python = Path(os.environ["LEAD_CI_IMAGE_PYTHON"])
    manifest = json.loads((runtime/"manifest.json").read_text())
    sha = manifest["sha"]
    workflow = yaml.load((ROOT/".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader)
    command = next(step["run"] for step in workflow["jobs"]["frontend"]["steps"]
        if step.get("name") == "Verify non-root access to the exact release image")
    # Use public Python for the actual nobody SDK subprocess, never a private
    # checkout/venv interpreter that would conceal a second permission failure.
    probe = subprocess.run(["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", str(python),
        "-IB", "-c", "import os,sys;assert os.getuid()==os.getgid()==65534;assert sys.version_info[:3]>=(3,12,3);assert sys.version_info[:2]==(3,12)"],
        cwd="/tmp", capture_output=True, text=True)
    assert probe.returncode == 0, probe.stderr
    checkout = tmp_path/"private-checkout"
    checkout.mkdir(mode=0o700)
    (checkout/"scripts").mkdir()
    verifier = checkout/"scripts/build_browser_runtime.py"
    verifier.write_bytes((ROOT/"scripts/build_browser_runtime.py").read_bytes())
    verifier.chmod(0o644)
    cwd = checkout/"apps/web"
    cwd.mkdir(parents=True)
    # Same source CLI check and actual runtime. No fake import, inventory,
    # browser, smoke function, filesystem compressor or mount command.
    prebuilt = os.environ.get("LEAD_CI_IMAGE_PATH")
    if prebuilt:
        image = Path(prebuilt)
        assert image.is_file() and not image.is_symlink()
        image_folder = image.parent
    else:
        stage = tmp_path/"stage"
        stage.mkdir(mode=0o755)
        stage.chmod(0o755)
        (stage/"REVISION").write_text(sha+"\n")
        (stage/"REVISION").chmod(0o644)
        shutil.copytree(ROOT/"services/api", stage/"services/api")
        shutil.copytree(runtime, stage/"services/browser-runtime", copy_function=os.link)
        for directory in stage.rglob("*"):
            if directory.is_dir():
                directory.chmod(0o755)
        image_folder = tmp_path/"image"
        image_folder.mkdir()
        image = image_folder/"lead.squashfs"
        subprocess.run(["mksquashfs", str(stage), str(image), "-noappend", "-all-root", "-no-progress", "-comp", "zstd", "-processors", "2"],
            check=True, capture_output=True, text=True, timeout=180)
    environment = {key: value for key, value in os.environ.items()
        if not key.startswith(("LEAD_", "PYTHON", "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "GH_", "GITHUB_"))}
    environment.update(RUNNER_TEMP=str(image_folder), GITHUB_SHA=sha,
        PATH=str(python.parent)+":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    result = subprocess.run(["bash", "-euo", "pipefail", "-x", "-c", command], cwd=cwd,
        env=environment, capture_output=True, text=True, timeout=180)
    (tmp_path/"ci-step.stdout").write_text(result.stdout)
    (tmp_path/"ci-step.stderr").write_text(result.stderr)
    paths = re.findall(r"^\+ (?:image_root|runtime_verifier)=(/tmp/lead-(?:image-root|runtime-verifier)\.[^\s]+)$", result.stderr, re.M)
    assert paths, result.stderr
    try:
        for value in paths:
            path = Path(value)
            assert not path.exists(), "the actual CI EXIT trap did not remove its unique temporary path"
            assert subprocess.run(["mountpoint", "-q", str(path)], check=False).returncode != 0
        assert not subprocess.check_output(["losetup", "-j", str(image)], text=True).strip(), "owned loop device leaked"
        assert result.returncode == 0, json.dumps({"exit": result.returncode, "stderr": result.stderr[-7000:]})
        assert "Exact release image root, REVISION and executor source are readable by non-root" in result.stdout
        assert "Exact mounted Python 3.12 SDK and Chromium passed a non-root native smoke test" in result.stdout
    finally:
        # Cleanup only paths returned by this exact invocation; never sweep /tmp.
        for value in paths:
            path = Path(value)
            if subprocess.run(["mountpoint", "-q", str(path)], check=False).returncode == 0:
                subprocess.run(["umount", str(path)], check=True)
            if path.is_dir():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
