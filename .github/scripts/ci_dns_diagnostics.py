"""Bounded read-only runner DNS evidence; package/test gates stay authoritative."""

import argparse
import ipaddress
import json
import secrets
import socket
import struct
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlencode

HOSTS = (
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "azure.archive.ubuntu.com",
    "packages.microsoft.com",
    "github.com",
)
LIMIT = 12000


def command(argv):
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, errors="replace", timeout=15,
            check=False,
        )
        return {
            "exit_code": result.returncode,
            "seconds": round(time.monotonic() - started, 3),
            "stdout": result.stdout[:LIMIT],
            "stderr": result.stderr[:LIMIT],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {
            "error": type(error).__name__,
            "seconds": round(time.monotonic() - started, 3),
        }


def resolve(host, method):
    if method == "system_getent":
        result = command(["getent", "ahostsv4", host])
    else:
        # Resolve dns.google normally: no fixed IP, resolver change or TLS bypass.
        # Failure to reach this control is evidence too, not permission to retry.
        url = "https://dns.google/resolve?" + urlencode({"name": host, "type": "A"})
        result = command([
            "curl", "--fail", "--silent", "--show-error", "--proto", "=https",
            "--connect-timeout", "5", "--max-time", "12", url,
        ])
        if result.get("exit_code") == 0:
            try:
                reply = json.loads(result.pop("stdout"))
                result["dns"] = {
                    key: reply.get(key) for key in ("Status", "Answer", "Authority")
                }
            except (ValueError, AttributeError):
                result["error"] = "invalid DNS JSON response"
    return {"probe": method, "host": host, **result}



def dns_servers(listing):
    servers = []
    for line in listing.splitlines():
        for value in line.partition(":")[2].split():
            try:
                address = str(ipaddress.IPv4Address(value))
            except ValueError:
                continue
            if address not in servers:
                servers.append(address)
    return servers[:4]


def udp_probe(server, host, *, port=53):
    started = time.monotonic()
    result = {"probe": "udp_dns_header", "server": server, "host": host}
    identifier = secrets.randbits(16)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".")) + b"\0\0\1\0\1"
    query = struct.pack("!6H", identifier, 0x0100, 1, 0, 0, 0) + question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
            connection.settimeout(3)
            connection.connect((server, port))
            connection.send(query)
            packet = connection.recv(4096)
        result["bytes"] = len(packet)
        if len(packet) < 12:
            result["error"] = "short DNS header"
        else:
            ident, flags, qd, an, ns, ar = struct.unpack("!6H", packet[:12])
            result.update(
                id_match=ident == identifier, flags=f"0x{flags:04x}",
                rcode=flags & 15, opcode=(flags >> 11) & 15,
                response=bool(flags & 0x8000), truncated=bool(flags & 0x0200),
                counts={"question": qd, "answer": an, "authority": ns, "additional": ar},
                question_match=packet[12:12 + len(question)] == question,
                question_hex=packet[12:12 + len(question)].hex(),
                expected_question_hex=question.hex(),
            )
    except OSError as error:
        result["error"] = type(error).__name__
    result["seconds"] = round(time.monotonic() - started, 3)
    return result


def mirror_probe(suite):
    # Temporary downloads only: no APT sources, trust keys or DNS settings change.
    result = {"probe": "signed_kernel_mirror", "suite": suite}
    with tempfile.TemporaryDirectory(prefix="lead-ci-mirror-") as folder:
        path = Path(folder) / "InRelease"
        result["https"] = command([
            "curl", "--fail", "--silent", "--show-error", "--proto", "=https",
            "--location", "--proto-redir", "=https", "--connect-timeout", "5",
            "--max-time", "12", "--max-filesize", "1048576", "--output", str(path),
            f"https://mirrors.edge.kernel.org/ubuntu/dists/{suite}/InRelease",
        ])
        if result["https"].get("exit_code") == 0:
            result["signature"] = command([
                "gpgv", "--keyring", "/usr/share/keyrings/ubuntu-archive-keyring.gpg", str(path),
            ])
            fields = dict(line.split(": ", 1) for line in path.read_text(errors="replace").splitlines()
                          if line.startswith(("Origin: ", "Suite: ", "Codename: ")))
            result["metadata"] = fields
            result["verified"] = (result["signature"].get("exit_code") == 0
                                  and fields == {"Origin": "Ubuntu", "Suite": suite, "Codename": "noble"})
        else:
            result["verified"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before-compose", "before-apt", "after-recovery", "after-restore"))
    phase = parser.parse_args().phase

    def emit(value):
        print(json.dumps({"phase": phase, **value}, ensure_ascii=True), flush=True)

    try:
        emit({"probe": "resolv.conf", "contents": Path("/etc/resolv.conf").read_text()[:LIMIT]})
    except OSError as error:
        emit({"probe": "resolv.conf", "error": type(error).__name__})
    upstream = command(["resolvectl", "dns"])
    emit({"probe": "configured_dns_servers", **upstream})
    servers = sorted(set(dns_servers(upstream.get("stdout", ""))) | {"8.8.8.8", "8.8.4.4", "1.1.1.1"})
    # Concurrent, individually bounded probes keep even total DNS loss brief.
    # Print no environment, credentials, application state or proxy settings.
    with ThreadPoolExecutor(max_workers=16) as pool:
        pending = {
            pool.submit(command, argv): {"probe": name}
            for name, argv in (
                ("resolver_status", ["resolvectl", "status", "--no-pager"]),
                ("ipv4_routes", ["ip", "-4", "route", "show"]),
                ("ipv6_routes", ["ip", "-6", "route", "show"]),
            )
        }
        pending.update({
            pool.submit(resolve, host, method): {}
            for host in HOSTS
            for method in ("system_getent", "google_doh")
        })
        pending.update({pool.submit(udp_probe, server, host): {}
                        for server in servers for host in HOSTS})
        pending[pool.submit(resolve, "mirrors.edge.kernel.org", "system_getent")] = {}
        pending.update({pool.submit(mirror_probe, suite): {}
                        for suite in ("noble", "noble-updates", "noble-security", "noble-backports")})
        for future in as_completed(pending):
            emit({**pending[future], **future.result()})
    # Probe failures are recorded, never substituted for the existing apt gate.


if __name__ == "__main__":
    main()
