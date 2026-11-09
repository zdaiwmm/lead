#!/usr/bin/python3 -IB
"""Read-only hashes for a privileged audit of the legacy systemd mount namespaces."""

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

BIN = Path("run/lead-home/bin")
APP = BIN / "original/services/api/lead_app"
SAFE_NAME = re.compile(r"[a-zA-Z0-9_]+\.py")


def file_digest(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unexpected code file type")
        return hashlib.file_digest(f, "sha256").hexdigest()


def scan(unit):
    def pid():
        value = subprocess.check_output(["systemctl", "show", unit, "-p", "MainPID", "--value"], text=True)
        if not value.strip().isdigit() or int(value) < 2:
            raise ValueError("service has no main process")
        return value.strip()

    before = pid()
    root = Path("/proc", before, "root")
    if unit == "lead-home-web.service":
        base = root / BIN / "web/.next"
        files = [base / "BUILD_ID"]
        for folder in ("server", "static"):
            files.extend(p for p in (base / folder).rglob("*")
                         if p.suffix in {".js", ".mjs", ".css", ".wasm"} and not p.is_symlink())
        # Keep asset names private, return one aggregate for comparison with the retained build.
        h = hashlib.sha256()
        for p in sorted(files):
            h.update(os.fsencode(p.relative_to(base)) + b"\0" + file_digest(p).encode() + b"\n")
        result = {"web_build_digest": h.hexdigest(), "files_hashed": len(files)}
    else:
        files = {p.name: file_digest(p) for p in (root / APP).iterdir() if SAFE_NAME.fullmatch(p.name)}
        if unit.startswith("lead-tg-"):
            for name in ("resident.py", "lead_business.py"):
                files[name] = file_digest(root / "run/lead-home" / name)
        result = {"code_sha256": files, "git_entry_present": (root / BIN / "original/.git").exists()}
    if pid() != before:
        raise ValueError("service changed during scan")
    return result


if __name__ == "__main__":
    try:
        units = ["lead-home-api.service", "lead-home-collector.service", "lead-home-web.service"]
        listing = subprocess.check_output(["systemctl", "list-units", "--type=service", "--state=running",
                                           "--plain", "--no-legend", "lead-tg-*.service"], text=True)
        units += [line.split()[0] for line in listing.splitlines()
                  if line.split() and re.fullmatch(r"lead-tg-[0-9a-f]+\.service", line.split()[0])]
        print(json.dumps({u: scan(u) for u in units}, sort_keys=True))
    except (OSError, ValueError, subprocess.SubprocessError):
        sys.exit("code inventory unavailable; check read permission and service identity")
