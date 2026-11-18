"""Publish the Chamber collector's reverse SSH socket on the production edge."""

import fcntl
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
from pathlib import Path

DIRECTORY = Path("/run/lead-chamber-shortlink")


def attach(directory, name):
    if not re.fullmatch(r"[0-9a-f]{24}\.sock", name):
        raise ValueError("Invalid SSH listener name")
    owner = directory.lstat()
    if not stat.S_ISDIR(owner.st_mode) or owner.st_uid != os.getuid() or stat.S_IMODE(owner.st_mode) != 0o2750:
        raise ValueError("Invalid SSH relay directory")
    listener = directory / name
    identity = listener.lstat()
    if not stat.S_ISSOCK(identity.st_mode) or identity.st_uid != os.getuid():
        raise ValueError("Unexpected SSH listener identity")
    if identity.st_gid != owner.st_gid:
        raise ValueError("SSH listener did not inherit the edge group")
    try:
        fd = os.open(directory / "relay.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            listener.chmod(0o660)
            link = directory / "redirect.sock"
            if link.exists() and not link.is_symlink():
                raise ValueError("Unexpected redirect socket path")
            temporary_link = directory / (name + ".ln")
            temporary_link.symlink_to(name)
            try:
                temporary_link.replace(link)
                sys.stdin.buffer.read(1)
            finally:
                temporary_link.unlink(missing_ok=True)
                if link.is_symlink() and os.readlink(link) == name:
                    link.unlink()
    finally:
        listener.unlink(missing_ok=True)


def connect(ssh_config, host, local_port):
    name = secrets.token_hex(12) + ".sock"
    source = Path(__file__).read_text()
    command = shlex.join(["/usr/bin/python3", "-IB", "-c", source, "attach", name])
    return subprocess.call(
        [
            "/usr/bin/ssh", "-F", ssh_config, "-T", "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ExitOnForwardFailure=yes",
            "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3", "-R", f"{DIRECTORY / name}:127.0.0.1:{local_port}",
            host, command,
        ],
        stdin=subprocess.PIPE,
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "attach":
        attach(DIRECTORY, sys.argv[2])
    else:
        sys.exit(connect(*sys.argv[1:]))
