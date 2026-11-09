"""Disposable GitHub Linux runner only. Exercise kernel process mounts across a failed cutover."""

import os
import subprocess
import sys
import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import home_systemd_release as hook  # noqa: E402


def run(*cmd):
    return subprocess.check_output(cmd, text=True).strip()


def main():
    if os.geteuid() or os.environ.get("LEAD_DISPOSABLE_RUNNER") != "1":
        raise SystemExit("disposable Linux runtime required")
    old, new = "a" * 40, "b" * 40
    unit = "lead-fixture-" + uuid.uuid4().hex + ".service"
    with tempfile.TemporaryDirectory(dir="/run") as work, ExitStack() as cleanup:
        folder = Path(work)
        source = folder / "source"
        (source / "services").mkdir(parents=True)
        (source / "web").mkdir()
        for sha in (old, new):
            (source / "REVISION").write_text(sha)
            (source / "services/identity").write_text(sha)
            mountpoint = folder / sha
            mountpoint.mkdir()
            image = folder / (sha + ".squashfs")
            run("mksquashfs", source, image, "-noappend", "-no-progress")
            run("mount", "-o", "loop,ro,nodev,nosuid", image, mountpoint)
            cleanup.callback(run, "umount", mountpoint)
        target = folder / "app-view/services"
        target.mkdir(parents=True)
        cleanup.enter_context(patch.object(hook, "STORE", folder))
        cleanup.enter_context(patch.object(hook, "ROLES", {"api": target}))
        cleanup.enter_context(patch.object(hook, "unit", return_value=unit))
        hook.select(old)
        run("systemd-run", "--quiet", "--collect", "--unit", unit,
            "--property", "Type=exec", "--property", f"BindReadOnlyPaths={folder}/current/services:{target}",
            "/usr/bin/sleep", "300")
        cleanup.callback(run, "systemctl", "stop", unit)
        assert hook.current() == old
        hook.select(new)
        try:
            hook.current()
        except hook.Refused:
            pass
        else:
            raise AssertionError("selector incorrectly overruled the old live mount")
        hook.select(old)
        fail = False

        def restart(sha):
            run("systemctl", "restart", unit)
            assert hook.current() == sha
            if fail and sha == new:
                raise hook.Refused("injected synthetic readiness failure")

        cleanup.enter_context(patch.object(hook, "restart_and_verify", restart))
        hook.replace_release(old, new)
        assert hook.current() == new
        hook.replace_release(new, old)
        fail = True
        try:
            hook.replace_release(old, new)
        except hook.Refused as error:
            assert "previous release independently verified" in str(error)
        else:
            raise AssertionError("failed candidate accepted")
        assert hook.current() == old
        print("Live process mount, exact image, cutover and verified rollback passed")


if __name__ == "__main__":
    main()
