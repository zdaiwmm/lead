#!/usr/bin/python3
"""Native browser network boundary; stdlib only, runs before Firefox starts.

No network interfaces beyond loopback. The sole egress bridge connects to the
profile-specific authenticated host proxy through one mounted Unix socket.
"""

import hashlib
import hmac
import json
import os
import select
import shutil
import socket
import stat
import sys
import threading
from pathlib import Path


def relay(connection, path):
    family = socket.AF_INET if isinstance(path, tuple) else socket.AF_UNIX
    with connection, socket.socket(family, socket.SOCK_STREAM) as upstream:
        try:
            upstream.connect(path)
            sockets = [connection, upstream]
            while True:
                ready, _, _ = select.select(sockets, [], [], 60)
                if not ready:
                    return
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    destination = upstream if source is connection else connection
                    destination.sendall(data)
        except OSError:
            return  # There is no direct-network fallback.


def control_settings(directory, port, args):
    """Accept one manager-owned private endpoint, never a caller-chosen network route."""
    directory = Path(directory)
    port = int(port)
    if not 1024 <= port <= 65535:
        raise ValueError("Invalid CDP control port")
    if not directory.is_absolute() or directory.resolve(strict=True) != directory:
        raise ValueError("Control directory must be an absolute non-symlink path")
    info = directory.stat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700 or any(directory.iterdir())):
        raise ValueError("Control directory must be private, owned and empty")
    for name, value in (("--remote-debugging-port", str(port)),
                        ("--remote-debugging-address", "127.0.0.1")):
        settings = [arg for arg in args if arg == name or arg.startswith(name + "=")]
        if settings != [name + "=" + value]:
            raise ValueError("Chromium control arguments differ from the assigned endpoint")
    return directory


def inside(binary, args):
    nonce = os.environ.get("LEAD_BOUNDARY_NONCE")
    if nonce:
        blocked = False
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            try:
                probe.sendto(b"lead-boundary-probe", ("8.8.8.8", 53))
            except OSError:
                blocked = True
        proof = {
            "netns": os.readlink("/proc/self/ns/net"),
            "only_loopback": all(name == "lo" for _, name in socket.if_nameindex()),
            "direct_udp_blocked": blocked,
        }
        if not proof["only_loopback"] or not blocked:
            raise ValueError("Namespace has an unexpected direct route")
        payload = json.dumps(proof, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(bytes.fromhex(nonce), payload, hashlib.sha256).hexdigest()
        with socket.socket(socket.AF_UNIX) as channel:
            channel.settimeout(10)
            channel.connect("/lead/forward.sock")
            channel.sendall(
                (
                    f"POST /__lead/attest HTTP/1.1\r\nContent-Length: {len(payload)}\r\nX-Lead-Proof: {signature}\r\n\r\n"
                ).encode()
                + payload
            )
            if b" 200 " not in channel.recv(512):
                raise ValueError("Namespace verification rejected")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", int(os.environ["LEAD_BROWSER_PROXY_PORT"])))
    listener.listen(64)
    listeners = {listener: "/lead/forward.sock"}
    if control_port := os.environ.get("LEAD_BROWSER_CDP_PORT"):
        control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        # The manager owns a fresh 0700 directory. Never replace an old socket.
        control.bind("/lead/control/cdp.sock")
        os.chmod("/lead/control/cdp.sock", 0o600)
        control.listen(8)
        listeners[control] = ("127.0.0.1", int(control_port))
    child = os.fork()
    if child == 0:
        # The bridge cannot retain native transport or manager descriptors.
        kept = {item.fileno() for item in listeners}
        for entry in os.listdir("/proc/self/fd"):
            descriptor = int(entry)
            if descriptor not in kept:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        while True:
            ready, _, _ = select.select(list(listeners), [], [])
            for server in ready:
                connection, _ = server.accept()
                threading.Thread(
                    target=relay, args=(connection, listeners[server]), daemon=True
                ).start()
    for server in listeners:
        server.close()
    for name in list(os.environ):
        if name.startswith("LEAD_"):
            del os.environ[name]
    os.execv(binary, [binary, *args])


def command(binary, profile, assets, proxy_socket, args, *, control_directory=None, cdp_port=None):
    bwrap = shutil.which("bwrap")
    if not bwrap or os.geteuid() == 0:
        raise ValueError("Linux browser node requires rootless bubblewrap")
    profile = Path(profile).resolve(strict=True)
    assets = Path(assets).resolve(strict=True)
    proxy_socket = Path(proxy_socket).resolve(strict=True)
    runtime = Path(binary).resolve(strict=True).parent
    script = Path(__file__).resolve()
    if not proxy_socket.is_socket() or not profile.is_dir() or not assets.is_dir():
        raise ValueError("Invalid isolated browser resources")
    result = [
        bwrap,
        "--unshare-user",
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
    ]
    # Only standard native browser libraries, fonts and certificate roots are
    # exposed. Other profiles, app files, .env and operator keys are absent.
    for path in (
        "/usr",
        "/bin",
        "/lib",
        "/lib64",
        "/etc/fonts",
        "/etc/ssl/certs",
        "/etc/ld.so.cache",
        "/var/cache/fontconfig",
    ):
        if Path(path).exists():
            result += ["--ro-bind", path, path]
    result += [
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/home/browser",
        "--dir",
        "/lead",
        "--ro-bind",
        str(runtime),
        str(runtime),
        "--bind",
        str(profile),
        str(profile),
        "--ro-bind",
        str(assets),
        str(assets),
        "--ro-bind",
        str(proxy_socket),
        "/lead/forward.sock",
        "--ro-bind",
        str(script),
        "/lead/namespace.py",
        "--setenv",
        "HOME",
        "/home/browser",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--chdir",
        str(profile),
    ]
    if (control_directory is None) != (cdp_port is None):
        raise ValueError("Both control directory and port are required")
    if control_directory is not None:
        control_directory = control_settings(control_directory, cdp_port, args)
        if (control_directory.is_relative_to(profile) or profile.is_relative_to(control_directory)
                or control_directory.is_relative_to(assets) or assets.is_relative_to(control_directory)):
            raise ValueError("Control directory must be separate from browser data")
        result += ["--bind", str(control_directory), "/lead/control"]
    # Bubblewrap forwards inherited descriptors to the executable. Its monitor
    # and PID 1 close their copies, allowing native transport EOF to propagate.
    return result + [
        "--",
        "/usr/bin/python3",
        "/lead/namespace.py",
        "--inside",
        str(Path(binary).resolve()),
        *args,
    ]


def main():
    if sys.argv[1:2] == ["--inside"]:
        return inside(sys.argv[2], sys.argv[3:])
    values = os.environ
    args = command(
        values["LEAD_REAL_BROWSER"],
        values["LEAD_BROWSER_PROFILE"],
        values["LEAD_BROWSER_ASSETS"],
        values["LEAD_BROWSER_SOCKET"],
        sys.argv[1:],
        control_directory=values.get("LEAD_BROWSER_CONTROL_DIRECTORY"),
        cdp_port=values.get("LEAD_BROWSER_CDP_PORT"),
    )
    if values.get("LEAD_BROWSER_CDP_PORT") == values.get("LEAD_BROWSER_PROXY_PORT"):
        raise ValueError("Control and proxy ports must differ")
    for entry in os.listdir("/proc/self/fd"):
        descriptor = int(entry)
        if descriptor > 4:
            try:
                os.close(descriptor)
            except OSError:
                pass
    os.execv(args[0], args)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Browser network isolation could not start.", file=sys.stderr)
        raise SystemExit(78) from None
