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
import sys
import threading
from pathlib import Path


def relay(connection, path):
    with connection, socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
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
    child = os.fork()
    if child == 0:
        # The proxy helper must not keep Playwright's Juggler pipes alive.
        for descriptor in (0, 1, 2, 3, 4):
            if descriptor == listener.fileno():
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass
        while True:
            connection, _ = listener.accept()
            threading.Thread(
                target=relay, args=(connection, "/lead/forward.sock"), daemon=True
            ).start()
    listener.close()
    for name in list(os.environ):
        if name.startswith("LEAD_"):
            del os.environ[name]
    os.execv(binary, [binary, *args])


def command(binary, profile, assets, proxy_socket, args):
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
    )
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
