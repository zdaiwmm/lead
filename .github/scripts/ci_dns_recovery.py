"""Reversible DNS recovery solely for disposable GitHub-hosted Linux runners."""

import argparse
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
import struct
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ARCHIVES = ("archive.ubuntu.com", "security.ubuntu.com", "azure.archive.ubuntu.com")
HOSTS = (*ARCHIVES, "packages.microsoft.com", "github.com")
PUBLIC_DNS = ("8.8.8.8", "8.8.4.4")
RECEIPT = Path("/run/lead-ci-dns-recovery.json")


def owner_uid():
    return 0


def guard():
    if (sys.platform != "linux" or os.geteuid() != 0
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_OS") != "Linux"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"):
        raise RuntimeError("DNS recovery requires a root GitHub-hosted Linux runner")


def run(*args):
    return subprocess.run(
        args, capture_output=True, text=True, check=True, timeout=12,
        env={**os.environ, "LC_ALL": "C"},
    ).stdout.strip()


def boot_id():
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def system_dns(hosts):
    def resolved(host):
        try:
            rows = run("/usr/bin/getent", "ahostsv4", host).splitlines()
            return bool(rows) and all(ipaddress.IPv4Address(row.split()[0]).is_global for row in rows)
        except (OSError, subprocess.SubprocessError, ValueError):
            return False

    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        results = dict(zip(hosts, pool.map(resolved, hosts)))
    print(json.dumps({"system_dns": results}), flush=True)
    return all(results.values())


def probe_dns(server, host, *, port=53):
    """One bounded UDP A query; validate response identity and a public A answer."""
    identifier = secrets.randbits(16)
    question = b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split(".")) + b"\0\0\1\0\1"
    query = struct.pack("!6H", identifier, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(3)
        connection.connect((server, port))
        connection.send(query)
        packet = connection.recv(4096)
    if len(packet) < 12:
        raise RuntimeError("short DNS response")
    ident, flags, questions, answers, _, _ = struct.unpack("!6H", packet[:12])
    if (ident != identifier or flags & 0x8000 == 0 or flags & 0x7A0F
            or questions != 1 or not answers or packet[12:12 + len(question)] != question):
        raise RuntimeError("invalid DNS response")
    offset, addresses = 12 + len(question), []
    try:
        for _ in range(answers):
            while packet[offset]:
                length = packet[offset]
                if length & 0xC0 == 0xC0:
                    if offset + 1 >= len(packet):
                        raise ValueError
                    offset += 2
                    break
                if length > 63:
                    raise ValueError
                offset += 1 + length
            else:
                offset += 1
            kind, cls, _, size = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            data = packet[offset:offset + size]
            if len(data) != size:
                raise ValueError
            if kind == 1 and cls == 1 and size == 4:
                address = ipaddress.IPv4Address(data)
                if address.is_global:
                    addresses.append(str(address))
            offset += size
    except (IndexError, ValueError, struct.error):
        raise RuntimeError("malformed DNS response") from None
    if not addresses:
        raise RuntimeError("DNS response has no public IPv4 answer")
    return addresses


def probe_public():
    # Both proposed servers must work before changing any resolver settings.
    pairs = [(server, host) for server in PUBLIC_DNS for host in HOSTS]
    def verified(pair):
        try:
            return probe_dns(*pair)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"public UDP DNS probe failed: {pair[0]} {pair[1]}") from error

    with ThreadPoolExecutor(max_workers=len(pairs)) as pool:
        results = list(pool.map(verified, pairs))
    print(json.dumps({"public_dns_verified": len(results)}), flush=True)


def default_link():
    routes = json.loads(run("/usr/sbin/ip", "-json", "-4", "route", "show", "default"))
    if len(routes) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]+", routes[0].get("dev", "")):
        raise RuntimeError("ambiguous default IPv4 link")
    name = routes[0]["dev"]
    links = json.loads(run("/usr/sbin/ip", "-json", "link", "show", "dev", name))
    if len(links) != 1 or not isinstance(links[0].get("ifindex"), int) or links[0]["ifindex"] <= 0:
        raise RuntimeError("invalid default IPv4 link")
    return {"name": name, "index": links[0]["ifindex"]}


def link_dns(link):
    value = run("/usr/bin/resolvectl", "dns", str(link["index"]))
    prefix = f"Link {link['index']} ({link['name']}):"
    if not value.startswith(prefix):
        raise RuntimeError("unexpected link DNS response")
    servers = value[len(prefix):].split()
    # Refuse unsupported port/SNI configurations rather than lose their meaning.
    if not servers:
        raise RuntimeError("default link has no restorable DNS servers")
    for server in servers:
        ipaddress.ip_address(server)
    return servers


def set_dns(link, servers):
    run("/usr/bin/resolvectl", "dns", str(link["index"]), *servers)


def flush():
    run("/usr/bin/resolvectl", "flush-caches")


def write_receipt(record):
    parent = RECEIPT.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != owner_uid() or parent.st_mode & 0o022:
        raise RuntimeError("unsafe DNS receipt directory")
    temporary = RECEIPT.with_name(".lead-ci-dns-" + secrets.token_hex(8))
    try:
        with open(temporary, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, RECEIPT)
    finally:
        temporary.unlink(missing_ok=True)


def read_receipt():
    info = RECEIPT.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != owner_uid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("unsafe DNS recovery receipt")
    record = json.loads(RECEIPT.read_text())
    if (record.get("version") != 1 or record.get("state") not in {"pending", "active", "restored"}
            or record.get("boot_id") != boot_id() or record.get("link") != default_link()
            or record.get("replacement_dns") != list(PUBLIC_DNS)):
        raise RuntimeError("DNS recovery receipt does not match this boot/link")
    if not isinstance(record.get("previous_dns"), list) or not record["previous_dns"]:
        raise RuntimeError("DNS recovery receipt lacks original servers")
    for server in record["previous_dns"]:
        ipaddress.ip_address(server)
    return record


def rollback(record):
    if record["boot_id"] != boot_id() or record["link"] != default_link():
        raise RuntimeError("default link changed; cannot restore DNS")
    current = link_dns(record["link"])
    if current == record["previous_dns"]:
        pass
    elif current == record["replacement_dns"]:
        set_dns(record["link"], record["previous_dns"])
        flush()
        if link_dns(record["link"]) != record["previous_dns"]:
            raise RuntimeError("original link DNS was not restored")
    else:
        raise RuntimeError("unexpected DNS on recovery link; preserving current settings and receipt")
    record["state"] = "restored"
    write_receipt(record)


def apply():
    guard()
    if os.path.lexists(RECEIPT):
        raise RuntimeError("DNS receipt already exists; inspect and restore first")
    if system_dns(ARCHIVES):
        return "unchanged"
    link = default_link()
    previous = link_dns(link)
    probe_public()
    record = {"version": 1, "state": "pending", "boot_id": boot_id(), "link": link,
              "previous_dns": previous, "replacement_dns": list(PUBLIC_DNS)}
    if link != default_link() or previous != link_dns(link):
        raise RuntimeError("link DNS changed during preflight")
    write_receipt(record)
    try:
        set_dns(link, PUBLIC_DNS)
        flush()
        if link_dns(link) != list(PUBLIC_DNS) or not system_dns(HOSTS):
            raise RuntimeError("post-change DNS verification failed")
        record["state"] = "active"
        write_receipt(record)
    except BaseException:
        rollback(record)
        raise
    return "recovered"


def restore():
    guard()
    if not os.path.lexists(RECEIPT):
        return "unchanged"
    record = read_receipt()
    if record["state"] == "restored":
        if link_dns(record["link"]) != record["previous_dns"]:
            raise RuntimeError("restored link DNS changed afterwards")
    else:
        rollback(record)
    return "restored"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "restore"))
    action = parser.parse_args().action
    result = apply() if action == "apply" else restore()
    print(json.dumps({"action": action, "result": result, "receipt": str(RECEIPT),
                      "restore_command": "sudo --preserve-env=GITHUB_ACTIONS,RUNNER_OS,RUNNER_ENVIRONMENT /usr/bin/python3 -IB .github/scripts/ci_dns_recovery.py restore"}))


if __name__ == "__main__":
    main()
