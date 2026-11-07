"""Offline wire-level tests for a remotely bound Telegram IPv6 exit."""
import asyncio
import ipaddress
import tempfile
from pathlib import Path

import httpx
import pytest
from lead_app.forward_proxy import BrowserForwardProxy

AID = "66000000-0000-4000-8000-000000000003"
SETTING = "LEAD_IPV6_TUNNEL_" + AID.replace("-", "").upper()
SOCKET = "/run/lead-egress/tokyo-fixture.sock"
SOURCE = {"server": "bound-ipv6", "source": "2001:db8::23"}
DESTINATION = "2606:4700:4700::1001"


def test_tunnel_requires_original_binding_and_private_socket(monkeypatch):
    monkeypatch.setenv(SETTING, "socks5+unix://" + SOCKET)
    with pytest.raises(ValueError):
        BrowserForwardProxy(AID, {"server": "socks5://127.0.0.1:1080"})
    for invalid in ("socks5://127.0.0.1:1080", "socks5+unix:///tmp/bad.sock"):
        monkeypatch.setenv(SETTING, invalid)
        with pytest.raises(ValueError):
            BrowserForwardProxy(AID, SOURCE)
    monkeypatch.delenv(SETTING)
    proxy = BrowserForwardProxy(AID, SOURCE)
    assert proxy.bound_ipv6 and not proxy.ipv6_tunnel


@pytest.fixture
def socket_dir():
    # Linux bounds sockaddr_un to 108 bytes, including the per-profile UUID.
    with tempfile.TemporaryDirectory(prefix="tg-relay-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.mark.asyncio
async def test_tunnel_sends_numeric_ipv6_for_https_and_dns_and_fails_closed(monkeypatch, socket_dir):
    monkeypatch.setenv(SETTING, "socks5+unix://" + SOCKET)
    monkeypatch.setattr("lead_app.forward_proxy.STATE", socket_dir)
    path = socket_dir / "socks"
    addresses = []
    unix_connect = asyncio.open_unix_connection

    async def fixture_socket(name):
        assert name == SOCKET
        return await unix_connect(str(path))

    async def socks(reader, writer):
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            request = await reader.readexactly(22)
            assert request[:4] == b"\x05\x01\x00\x04" and request[-2:] == (443).to_bytes(2, "big")
            address = str(ipaddress.IPv6Address(request[4:20]))
            addresses.append(address)
            if address == DESTINATION:
                writer.write(b"\x05\x00\x00\x04" + bytes(18))
                await writer.drain()
                assert await reader.readexactly(4) == b"PING"
                writer.write(b"PONG")
            else:
                writer.write(b"\x05\x04\x00\x04" + bytes(18))
            await writer.drain()
        finally:
            writer.close()

    relay = await asyncio.start_unix_server(socks, str(path))
    monkeypatch.setattr(asyncio, "open_unix_connection", fixture_socket)
    proxy = await BrowserForwardProxy(AID, SOURCE).start()
    try:
        reader, writer = await proxy.connect(DESTINATION, 443)
        writer.write(b"PING")
        await writer.drain()
        assert await reader.readexactly(4) == b"PONG"
        writer.close()
        await writer.wait_closed()
        with pytest.raises(ValueError):
            await proxy.connect("1.1.1.1", 443)
        with pytest.raises(httpx.ProxyError):
            await proxy.resolve("example.org")
        assert addresses == [DESTINATION, "2606:4700:4700::1111"]
        relay.close()
        await relay.wait_closed()
        with pytest.raises(OSError):
            await proxy.connect(DESTINATION, 443)
        assert proxy.bound_ipv6 and proxy.upstream_failures == 2
    finally:
        relay.close()
        await relay.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
async def test_tunnel_uses_aaaa_and_keeps_only_public_ipv6(monkeypatch):
    monkeypatch.setenv(SETTING, "socks5+unix://" + SOCKET)
    proxy = BrowserForwardProxy(AID, SOURCE)
    proxy.port = 12345
    client_factory = httpx.AsyncClient
    answers = [
        {"type": 1, "data": "93.184.216.34", "TTL": 30},
        {"type": 28, "data": DESTINATION, "TTL": 30},
    ]

    async def reply(request):
        assert request.url.host == "2606:4700:4700::1111"
        assert request.url.params["type"] == "AAAA"
        return httpx.Response(200, json={"Status": 0, "Answer": answers})

    def client_factory_for_test(**kwargs):
        assert kwargs["transport"] is None and kwargs["proxy"].endswith("@127.0.0.1:12345")
        assert not kwargs["trust_env"]
        return client_factory(transport=httpx.MockTransport(reply), trust_env=False)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory_for_test)
    assert await proxy.resolve("fixture.example") == DESTINATION
    answers.pop()
    with pytest.raises(ValueError, match="no address"):
        await proxy.resolve("v4-only.example")
    for ip in ("::1", "fc00::1", "169.254.169.254", "1.1.1.1"):
        with pytest.raises(ValueError):
            await proxy.resolve(ip)
