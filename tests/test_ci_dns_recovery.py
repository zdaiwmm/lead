"""Recovery decisions and exact rollback, without changing host DNS."""

import importlib.util
import json
import os
import socket
import struct
import threading
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_dns_recovery", Path(__file__).resolve().parents[1] / ".github/scripts/ci_dns_recovery.py"
)
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)


@pytest.fixture
def node(monkeypatch, tmp_path):
    monkeypatch.setattr(recovery, "guard", lambda: None)
    monkeypatch.setattr(recovery, "owner_uid", lambda: os.getuid())
    monkeypatch.setattr(recovery, "RECEIPT", tmp_path / "receipt.json")
    monkeypatch.setattr(recovery, "boot_id", lambda: "fixture-boot")
    monkeypatch.setattr(recovery, "default_link", lambda: {"name": "eth0", "index": 2})
    current = ["168.63.129.16", "10.0.0.2"]
    writes = []

    def set_dns(link, servers):
        assert link == {"name": "eth0", "index": 2}
        writes.append(list(servers))
        current[:] = servers

    monkeypatch.setattr(recovery, "link_dns", lambda link: list(current))
    monkeypatch.setattr(recovery, "set_dns", set_dns)
    monkeypatch.setattr(recovery, "flush", lambda: None)
    monkeypatch.setattr(recovery, "probe_public", lambda: None)
    return current, writes


def test_working_system_dns_is_noop(node, monkeypatch):
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: True)
    monkeypatch.setattr(recovery, "probe_public", lambda: pytest.fail("unneeded public probe"))
    assert recovery.apply() == "unchanged"
    assert node[1] == []
    assert not recovery.RECEIPT.exists()
    assert recovery.restore() == "unchanged"


def test_invalid_environment_refuses_before_dns_reads(monkeypatch):
    monkeypatch.setenv("GITHUB_ACTIONS", "false")
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: pytest.fail("DNS touched"))
    with pytest.raises(RuntimeError, match="hosted Linux"):
        recovery.apply()


def test_public_dns_unavailable_changes_nothing(node, monkeypatch):
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: False)

    def unavailable():
        raise RuntimeError("public resolver unavailable")

    monkeypatch.setattr(recovery, "probe_public", unavailable)
    with pytest.raises(RuntimeError, match="public resolver"):
        recovery.apply()
    assert node[1] == []
    assert not recovery.RECEIPT.exists()


def test_success_has_private_receipt_and_restores_exact_previous_order(node, monkeypatch):
    checks = iter([False, True])
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: next(checks))
    assert recovery.apply() == "recovered"
    assert node[0] == list(recovery.PUBLIC_DNS)
    record = json.loads(recovery.RECEIPT.read_text())
    assert record["previous_dns"] == ["168.63.129.16", "10.0.0.2"]
    assert record["state"] == "active"
    assert recovery.RECEIPT.stat().st_mode & 0o777 == 0o600
    # Only the test filesystem ownership differs from the root-only real runner.
    monkeypatch.setattr(recovery, "owner_uid", lambda: os.getuid())
    assert recovery.restore() == "restored"
    assert node[1] == [list(recovery.PUBLIC_DNS), record["previous_dns"]]
    assert recovery.restore() == "restored"
    assert len(node[1]) == 2


def test_postchange_dns_failure_restores_before_returning_failure(node, monkeypatch):
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: False)
    with pytest.raises(RuntimeError, match="post-change"):
        recovery.apply()
    assert node[1] == [list(recovery.PUBLIC_DNS), ["168.63.129.16", "10.0.0.2"]]
    assert json.loads(recovery.RECEIPT.read_text())["state"] == "restored"


def test_link_change_refuses_restore(node, monkeypatch):
    checks = iter([False, True])
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: next(checks))
    recovery.apply()
    monkeypatch.setattr(recovery, "owner_uid", lambda: os.getuid())
    monkeypatch.setattr(recovery, "default_link", lambda: {"name": "other0", "index": 3})
    with pytest.raises(RuntimeError, match="link"):
        recovery.restore()
    assert len(node[1]) == 1


@pytest.mark.parametrize("mode", ["success", "wrong_id", "refused", "truncated", "no_answer"])
def test_actual_udp_probe_rejects_invalid_dns_response(mode):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(2)

        def respond():
            query, peer = server.recvfrom(4096)
            ident = struct.unpack("!H", query[:2])[0]
            flags, count = 0x8180, 1
            if mode == "wrong_id":
                ident ^= 1
            if mode == "refused":
                flags |= 5
            if mode == "truncated":
                flags |= 0x0200
            if mode == "no_answer":
                count = 0
            packet = struct.pack("!6H", ident, flags, 1, count, 0, 0) + query[12:]
            if count:
                packet += b"\xc0\x0c" + struct.pack("!HHIH", 1, 1, 30, 4) + socket.inet_aton("1.1.1.1")
            server.sendto(packet, peer)

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            if mode == "success":
                assert recovery.probe_dns("127.0.0.1", "example.com", port=server.getsockname()[1]) == ["1.1.1.1"]
            else:
                with pytest.raises(RuntimeError, match="DNS"):
                    recovery.probe_dns("127.0.0.1", "example.com", port=server.getsockname()[1])
        finally:
            worker.join(timeout=3)


def test_failed_mutation_can_be_restored_even_if_set_command_raises(node, monkeypatch):
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: False)
    original = recovery.set_dns

    def partial(link, servers):
        original(link, servers)
        if list(servers) == list(recovery.PUBLIC_DNS):
            raise RuntimeError("resolvectl uncertain result")

    monkeypatch.setattr(recovery, "set_dns", partial)
    with pytest.raises(RuntimeError, match="uncertain result"):
        recovery.apply()
    assert node[0] == ["168.63.129.16", "10.0.0.2"]
    assert json.loads(recovery.RECEIPT.read_text())["state"] == "restored"


@pytest.mark.parametrize("key,value", [("GITHUB_ACTIONS", "false"), ("RUNNER_OS", "macOS"), ("RUNNER_ENVIRONMENT", "self-hosted")])
def test_guard_rejects_non_disposable_context(monkeypatch, key, value):
    monkeypatch.setattr(recovery.sys, "platform", "linux")
    monkeypatch.setattr(recovery.os, "geteuid", lambda: 0)
    for name, content in {"GITHUB_ACTIONS": "true", "RUNNER_OS": "Linux", "RUNNER_ENVIRONMENT": "github-hosted"}.items():
        monkeypatch.setenv(name, content)
    monkeypatch.setenv(key, value)
    with pytest.raises(RuntimeError, match="hosted Linux"):
        recovery.guard()


def test_changed_dns_during_preflight_is_not_overwritten(node, monkeypatch):
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: False)

    def change_during_probe():
        node[0][:] = ["10.0.0.9"]

    monkeypatch.setattr(recovery, "probe_public", change_during_probe)
    with pytest.raises(RuntimeError, match="changed during preflight"):
        recovery.apply()
    assert node[1] == []
    assert not recovery.RECEIPT.exists()


@pytest.mark.parametrize("state", ["active", "pending"])
def test_restore_refuses_unexpected_current_dns_without_overwriting_receipt(node, monkeypatch, state):
    checks = iter([False, True])
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: next(checks))
    recovery.apply()
    record = json.loads(recovery.RECEIPT.read_text())
    record["state"] = state
    recovery.write_receipt(record)
    before = recovery.RECEIPT.read_bytes()
    node[0][:] = ["10.0.0.9"]
    with pytest.raises(RuntimeError, match="unexpected DNS"):
        recovery.restore()
    assert node[0] == ["10.0.0.9"]
    assert len(node[1]) == 1
    assert recovery.RECEIPT.read_bytes() == before


def test_already_original_dns_is_marked_restored_without_rewriting(node, monkeypatch):
    checks = iter([False, True])
    monkeypatch.setattr(recovery, "system_dns", lambda hosts: next(checks))
    recovery.apply()
    record = json.loads(recovery.RECEIPT.read_text())
    node[0][:] = record["previous_dns"]
    monkeypatch.setattr(recovery, "flush", lambda: pytest.fail("unnecessary resolver mutation"))
    assert recovery.restore() == "restored"
    assert len(node[1]) == 1
    assert json.loads(recovery.RECEIPT.read_text())["state"] == "restored"
