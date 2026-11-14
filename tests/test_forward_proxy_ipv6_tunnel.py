"""Local wire tests for the operator-owned IPv6 tunnel (no external network)."""

import asyncio
import datetime
import ipaddress
import json
import socket
import ssl
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lead_app import forward_proxy
from lead_app.forward_proxy import BrowserForwardProxy

TUNNEL = "socks5+unix:///run/lead-egress/fixture.sock"
RESOLVER = "2606:4700:4700::1111"
DESTINATION = "2606:4700:4700::1001"
BOUND = {"server": "bound-ipv6", "source": DESTINATION}


@pytest.fixture
def tmp_path():
    # AF_UNIX paths have a small OS limit; macOS pytest roots exceed it.
    with tempfile.TemporaryDirectory(prefix="lead-tunnel-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    aid = str(uuid4())
    monkeypatch.setenv("LEAD_IPV6_TUNNEL_" + aid.replace("-", "").upper(), TUNNEL)
    monkeypatch.setattr(forward_proxy, "STATE", tmp_path)
    original = asyncio.open_unix_connection

    async def local_tunnel(path, *args, **kwargs):
        assert path == "/run/lead-egress/fixture.sock"
        return await original(str(tmp_path / "exit.sock"), *args, **kwargs)

    monkeypatch.setattr(asyncio, "open_unix_connection", local_tunnel)
    return aid


@asynccontextmanager
async def socks_server(path, *, tls=None, reject=False):
    requests, errors, tasks = [], [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        try:
            assert await reader.readexactly(3) == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            await writer.drain()
            assert await reader.readexactly(4) == b"\x05\x01\x00\x04"
            address = str(ipaddress.IPv6Address(await reader.readexactly(16)))
            port = int.from_bytes(await reader.readexactly(2), "big")
            requests.append((address, port))
            writer.write(
                b"\x05" + (b"\x05" if reject else b"\x00") + b"\x00\x04" + bytes(18)
            )
            await writer.drain()
            if reject:
                return
            if tls:
                await writer.start_tls(tls)
                request = await reader.readuntil(b"\r\n\r\n")
                requests.append(request)
                body = json.dumps(
                    {
                        "Status": 0,
                        "Answer": [{"type": 28, "data": DESTINATION, "TTL": 30}],
                    }
                ).encode()
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                    + body
                )
                await writer.drain()
            else:
                while data := await reader.read(1024):
                    writer.write(data)
                    await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(task)

    server = await asyncio.start_unix_server(handle, str(path))
    try:
        yield requests
    finally:
        server.close()
        await server.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*list(tasks), return_exceptions=True)
        assert not errors


@pytest.mark.parametrize("port", [80, 443])
@pytest.mark.parametrize("bracketed", [False, True])
async def test_ipv6_connect_uses_real_unix_socks_and_preserves_slots(
    configured, tmp_path, port, bracketed
):
    target = f"[{DESTINATION}]:{port}" if bracketed else f"{DESTINATION}:{port}"
    first = BrowserForwardProxy(configured, BOUND, slot_id="fixture-a")
    second = BrowserForwardProxy(configured, BOUND, slot_id="fixture-b")
    assert first.socket_path != second.socket_path
    assert first.password != second.password
    async with socks_server(tmp_path / "exit.sock") as requests:
        await first.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", first.port)
            writer.write(
                f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {second.auth}\r\n\r\n".encode()
            )
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 407")
            writer.close()
            await writer.wait_closed()
            assert requests == []
            reader, writer = await asyncio.open_connection("127.0.0.1", first.port)
            writer.write(
                f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {first.auth}\r\n\r\n".encode()
            )
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
            writer.write(b"opaque TLS bytes")
            await writer.drain()
            assert await reader.readexactly(16) == b"opaque TLS bytes"
            writer.close()
            await writer.wait_closed()
            assert requests == [(DESTINATION, port)]
        finally:
            await first.close()


@pytest.fixture
def tls_contexts(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local DNS fixture")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(RESOLVER))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert_path, key_path)
    client = ssl.create_default_context(cafile=str(cert_path))
    return server, client


async def test_dns_uses_same_authenticated_proxy_and_unix_tunnel(
    configured, tmp_path, monkeypatch, tls_contexts
):
    server_tls, client_tls = tls_contexts
    real_client = httpx.AsyncClient

    def trusted_fixture_client(**kwargs):
        assert kwargs["proxy"] is not None
        assert kwargs["transport"] is None
        assert kwargs["trust_env"] is False
        return real_client(verify=client_tls, **kwargs)

    monkeypatch.setattr(forward_proxy.httpx, "AsyncClient", trusted_fixture_client)
    proxy = BrowserForwardProxy(configured, BOUND, slot_id="fixture-dns")
    async with socks_server(tmp_path / "exit.sock", tls=server_tls) as requests:
        await proxy.start()
        try:
            assert (
                await asyncio.wait_for(proxy.resolve("example.com"), 5) == DESTINATION
            )
            assert requests[0] == (RESOLVER, 443)
            assert requests[1].startswith(
                b"GET /dns-query?name=example.com&type=AAAA HTTP/1.1\r\n"
            )
            assert proxy.connections == 1
            assert await proxy.resolve("example.com") == DESTINATION
            assert len(requests) == 2
        finally:
            await proxy.close()


@pytest.mark.parametrize("reject", [False, True])
async def test_tunnel_failure_never_falls_back_to_bound_address(
    configured, tmp_path, monkeypatch, reject
):
    calls = []

    async def forbidden_direct(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("direct network fallback")

    monkeypatch.setattr(asyncio, "open_connection", forbidden_direct)
    proxy = BrowserForwardProxy(configured, BOUND)
    if reject:
        async with socks_server(tmp_path / "exit.sock", reject=True) as requests:
            with pytest.raises(ConnectionError, match="SOCKS CONNECT failed"):
                await proxy.connect(DESTINATION, 443)
            assert requests == [(DESTINATION, 443)]
    else:
        with pytest.raises(FileNotFoundError):
            await proxy.connect(DESTINATION, 443)
    assert calls == []
    assert proxy.upstream_failures == 1


@pytest.mark.parametrize(
    "tunnel",
    [
        "http://127.0.0.1:9000",
        "socks5://127.0.0.1:9000",
        "socks5+unix:///tmp/exit.sock",
        "socks5+unix://user@/run/lead-egress/fixture.sock",
        TUNNEL + "?extra=1",
        "socks5+unix:///run/lead-egress/../fixture.sock",
    ],
)
def test_tunnel_configuration_rejects_non_dedicated_routes(monkeypatch, tunnel):
    aid = str(uuid4())
    monkeypatch.setenv("LEAD_IPV6_TUNNEL_" + aid.replace("-", "").upper(), tunnel)
    with pytest.raises(ValueError):
        BrowserForwardProxy(aid, BOUND)


@pytest.mark.parametrize("upstream", [None, {"server": "http://127.0.0.1:9000"}])
def test_tunnel_requires_existing_bound_ipv6_identity(configured, upstream):
    with pytest.raises(ValueError):
        BrowserForwardProxy(configured, upstream)


async def test_without_tunnel_keeps_existing_source_binding(monkeypatch):
    calls = []
    result = (object(), object())

    async def direct(*args, **kwargs):
        calls.append((args, kwargs))
        return result

    monkeypatch.setattr(asyncio, "open_connection", direct)
    proxy = BrowserForwardProxy(str(uuid4()), BOUND)
    assert await proxy.connect(DESTINATION, 443) == result
    assert calls == [
        (
            (DESTINATION, 443),
            {"family": socket.AF_INET6, "local_addr": (DESTINATION, 0)},
        )
    ]


@pytest.mark.parametrize(
    "target",
    [
        "1.1.1.1:443",
        "[::1]:443",
        "[::ffff:1.1.1.1]:443",
        "[2002:c000:0201::]:443",
        f"[{DESTINATION}]:81",
        f"{DESTINATION}:8443",
    ],
)
async def test_tunnel_connect_keeps_native_public_ipv6_and_port_policy(
    configured, tmp_path, target
):
    proxy = BrowserForwardProxy(configured, BOUND, slot_id="fixture-denied")
    async with socks_server(tmp_path / "exit.sock") as requests:
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: {proxy.auth}\r\n\r\n".encode()
            )
            await writer.drain()
            assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 502")
            writer.close()
            await writer.wait_closed()
            assert requests == []
        finally:
            await proxy.close()
