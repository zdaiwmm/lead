#!/usr/bin/python3 -IB
"""Promote only the protected main CI artifact. Only called by the trusted node dispatcher."""

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# This program and its two sibling modules must be installed root-owned outside the application.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from prepare_home_image import prepare  # noqa: E402
from request_home_release import REPO, github_cli  # noqa: E402

STORE = Path("/srv/lead-prod-vault/lead-git-releases")
TOOLS = Path("/usr/local/lib/lead-home-release")


def public_api(resource):
    # Approval and artifact metadata come from GitHub independently of the transfer identity.
    request = urllib.request.Request(f"https://api.github.com/repos/{REPO}/{resource}",
                                     headers={"User-Agent": "lead-home-node", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def mount_unit(sha, image, mountpoint):
    return f"""[Unit]
Description=Lead verified release {sha}
RequiresMountsFor={image.parent}
Before=lead-home-live.target lead-home-api.service lead-home-web.service lead-home-worker.service lead-home-browser.service lead-home-maintenance.service lead-home-collector.service

[Mount]
What={image}
Where={mountpoint}
Type=squashfs
Options=loop,ro,nodev,nosuid

[Install]
WantedBy=lead-home-live.target
"""


def materialize(sha):
    if os.geteuid() or not re.fullmatch("[0-9a-f]{40}", sha):
        raise ValueError("local administrator and full SHA required")
    vault = subprocess.check_output(["findmnt", "-n", "-M", STORE.parent, "-o", "TARGET"], text=True).strip()
    if vault != str(STORE.parent):
        raise ValueError("vault not mounted")
    images = STORE / "images"
    for p in (STORE, images):
        if p == images:
            p.mkdir(mode=0o700, exist_ok=True)
        if p.resolve() != p or p.stat().st_uid != 0 or p.stat().st_mode & 0o022:
            raise ValueError("untrusted image store")
    prepare(sha, images, get=public_api, download_command=github_cli(node=True))
    point = STORE / sha
    point.mkdir(mode=0o755, exist_ok=True)
    if point.resolve() != point or point.stat().st_uid or point.stat().st_mode & 0o022:
        raise ValueError("untrusted mount point")
    if not point.is_mount() and any(point.iterdir()):
        raise ValueError("mount point is not empty")
    unit = subprocess.check_output(["systemd-escape", "--path", "--suffix=mount", point], text=True).strip()
    config = Path("/etc/systemd/system", unit)
    body = mount_unit(sha, images / (sha + ".squashfs"), point).encode()
    with tempfile.TemporaryDirectory(dir=config.parent) as tmp:
        new = Path(tmp, "unit")
        with new.open("xb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        new.chmod(0o644)
        try:
            os.link(new, config)
        except FileExistsError:
            if config.is_symlink() or config.read_bytes() != body:
                raise ValueError("existing mount unit differs") from None
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", unit], check=True)


if __name__ == "__main__":
    try:
        materialize(*sys.argv[1:])
    except Exception:
        sys.exit("release image promotion failed; inspect locally")
