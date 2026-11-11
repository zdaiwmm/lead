"""Select one authenticated Ubuntu mirror on a disposable GitHub Linux runner."""

import json
import os
import stat
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MIRROR = "https://mirrors.edge.kernel.org/ubuntu"
SUITES = ("noble", "noble-updates", "noble-security", "noble-backports")
MIRRORS = Path("/etc/apt/apt-mirrors.txt")
KEYRING = "/usr/share/keyrings/ubuntu-archive-keyring.gpg"


def guard():
    if (sys.platform != "linux" or os.geteuid() != 0
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_OS") != "Linux"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise RuntimeError("mirror selection requires a root GitHub-hosted Linux runner")


def verified_suite(suite):
    with tempfile.TemporaryDirectory(prefix=f"lead-ci-{suite}-") as folder:
        signed, plain = Path(folder) / "InRelease", Path(folder) / "verified"
        subprocess.run([
            "curl", "--fail", "--silent", "--show-error", "--proto", "=https",
            "--location", "--proto-redir", "=https", "--connect-timeout", "5",
            "--max-time", "15", "--max-filesize", "1048576", "--output", str(signed),
            f"{MIRROR}/dists/{suite}/InRelease",
        ], check=True, timeout=20)
        subprocess.run([
            "gpgv", "--keyring", KEYRING, "--output", str(plain), str(signed),
        ], check=True, timeout=15)
        expected = {"Origin": "Ubuntu", "Suite": suite, "Codename": "noble"}
        fields = {}
        # Parse only gpgv's authenticated plaintext, never unsigned suffixes.
        for line in plain.read_text().splitlines():
            key, separator, value = line.partition(": ")
            if separator and key in expected:
                if key in fields:
                    raise RuntimeError(f"duplicate {key} in verified {suite} metadata")
                fields[key] = value
        if fields != expected:
            raise RuntimeError(f"unexpected authenticated metadata for {suite}")
    return suite


def target_state():
    info, parent = MIRRORS.lstat(), MIRRORS.parent.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or info.st_nlink != 1 or info.st_size > 16384
            or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022):
        raise RuntimeError("unsafe existing APT mirror file")
    # Reading may update atime; compare only identity, ownership and write state.
    stable = {key: getattr(info, key) for key in (
        "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"
    )}
    return stable, MIRRORS.read_bytes()


def main():
    guard()
    original = target_state()
    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(verified_suite, SUITES))
    if target_state() != original:
        raise RuntimeError("APT mirror file changed during verification")
    fd, name = tempfile.mkstemp(prefix=".lead-ci-mirror-", dir=MIRRORS.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), stat.S_IMODE(original[0]["st_mode"]))
            os.fchown(stream.fileno(), -1, original[0]["st_gid"])
            stream.write(MIRROR + "/\tpriority:1\n")
            stream.flush()
            os.fsync(stream.fileno())
        if target_state() != original:
            raise RuntimeError("APT mirror file changed before replacement")
        os.replace(temporary, MIRRORS)
    finally:
        temporary.unlink(missing_ok=True)
    print(json.dumps({"mirror": MIRROR, "verified_suites": verified}))


if __name__ == "__main__":
    main()
