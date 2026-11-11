"""Decode actual UDP replies as evidence, without authorizing DNS changes."""

import importlib.util
import socket
import struct
import threading
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ci_dns_diagnostics", Path(__file__).resolve().parents[1] / ".github/scripts/ci_dns_diagnostics.py"
)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


@pytest.mark.parametrize("flags,answer_count,wrong_id,wrong_question", [
    (0x8180, 1, False, False),
    (0x81A0, 1, False, False),
    (0x8182, 0, False, False),
    (0x8380, 0, False, False),
    (0x8180, 1, True, True),
])
def test_udp_header_diagnostics_preserve_failure_evidence(flags, answer_count, wrong_id, wrong_question):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
        server.bind(("127.0.0.1", 0))
        server.settimeout(2)

        def respond():
            query, peer = server.recvfrom(4096)
            ident = struct.unpack("!H", query[:2])[0] ^ int(wrong_id)
            question = query[12:]
            if wrong_question:
                question = question.replace(b"example", b"changed")
            reply = struct.pack("!6H", ident, flags, 1, answer_count, 0, 0) + question
            server.sendto(reply, peer)

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            result = diagnostics.udp_probe("127.0.0.1", "example.com", port=server.getsockname()[1])
        finally:
            worker.join(timeout=3)
    assert result["id_match"] is (not wrong_id)
    assert result["question_match"] is (not wrong_question)
    assert result["flags"] == f"0x{flags:04x}"
    assert result["rcode"] == flags & 15
    assert result["truncated"] is bool(flags & 0x0200)
    assert result["counts"] == {"question": 1, "answer": answer_count, "authority": 0, "additional": 0}
    assert "accepted" not in result


def test_resolver_selection_uses_only_addresses_from_dns_listing():
    listing = "Global: 1.0.0.1\nLink 2 (eth0): 168.63.129.16 2001:4860:4860::8888\nLink 3 (fixture): not-an-address\n"
    assert diagnostics.dns_servers(listing) == ["1.0.0.1", "168.63.129.16"]
