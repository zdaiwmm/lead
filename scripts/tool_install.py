"""Single-file atomic maintenance-tool installation; no application cutover or restart."""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path

INSTALLABLE = {
    "home_systemd_release.py", "request_home_release.py", "poll_home_release.py",
    "prepare_home_image.py", "home_release_operator.py",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def trusted(path, directory=False):
    value = path.lstat()
    if value.st_uid != os.geteuid() or value.st_mode & 0o022 or not (
        stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    ):
        raise ValueError("Untrusted tool/store path")


def atomic_write(path, data, mode):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temp = Path(file.name)
        try:
            file.write(data)
            file.flush()
            os.fchmod(file.fileno(), mode)
            os.fsync(file.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


def install(tools, store, name, data, sha, expected_old, expected_app, *, current, git_source):
    if name not in INSTALLABLE:
        raise ValueError("Tool outside installation scope")
    if (not re.fullmatch("[a-f0-9]{40}", sha) or not re.fullmatch("[a-f0-9]{40}", expected_app)
            or not re.fullmatch("[a-f0-9]{64}", expected_old) or not 0 < len(data) <= 1024 * 1024):
        raise ValueError("Exact bounded tool and application identities required")
    trusted(tools, True)
    trusted(store, True)
    target = tools / name
    trusted(target)  # First-time provisioning is a separate, explicit operation.
    compile(data, name, "exec")
    with (store / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if current() != expected_app:
            raise ValueError("Live application changed; no write performed")
        if git_source(sha, "scripts/" + name) != data:
            raise ValueError("Approved main Git source differs")
        old, new_hash = target.read_bytes(), digest(data)
        folder = store / "tool-backups"
        folder.mkdir(mode=0o700, exist_ok=True)
        trusted(folder, True)
        folder = folder / sha
        folder.mkdir(mode=0o700, exist_ok=True)
        trusted(folder, True)
        backup, receipt = folder / name, folder / (name + ".json")
        if digest(old) == new_hash:
            # A retry verifies the prior installation; it never repeats a write.
            trusted(receipt)
            record = json.loads(receipt.read_text())
            trusted(backup)
            if record != {"sha": sha, "name": name, "old": expected_old,
                          "new": new_hash, "app": expected_app} or digest(backup.read_bytes()) != expected_old:
                raise ValueError("Existing installation has no matching backup/provenance")
        else:
            if digest(old) != expected_old:
                raise ValueError("Installed tool changed; no overwrite")
            record = {"sha": sha, "name": name, "old": expected_old, "new": new_hash, "app": expected_app}
            if backup.exists():
                trusted(backup)
                if backup.read_bytes() != old:
                    raise ValueError("Backup collision")
            else:
                atomic_write(backup, old, 0o600)
            atomic_write(receipt, (json.dumps(record) + "\n").encode(), 0o600)
            try:
                atomic_write(target, data, 0o755)
                trusted(target)
                if digest(target.read_bytes()) != new_hash or current() != expected_app:
                    raise ValueError("Post-install identity check failed")
            except BaseException:
                if target.is_file() and not target.is_symlink() and digest(target.read_bytes()) == new_hash:
                    atomic_write(target, old, 0o755)
                raise
        if digest(target.read_bytes()) != new_hash or current() != expected_app:
            raise ValueError("Independent installation identity unavailable")
        return {"result": "TOOL_INSTALLED", "sha": sha, "tool": name, "sha256": new_hash,
                "application_sha": expected_app, "backup": str(backup),
                "application_switch": False, "service_restarts": False}
