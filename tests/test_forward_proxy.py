import asyncio
import os

import httpx
import pytest
from lead_app import domain
from lead_app.browser import Gateway
from lead_app.db import uid
from lead_app.forward_proxy import BrowserForwardProxy


@pytest.mark.asyncio
async def test_proxy_denies_missing_credentials_private_targets_and_wrong_profile():
    aid = uid()
    proxy = await BrowserForwardProxy(aid).start()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(b"CONNECT 1.1.1.1:443 HTTP/1.1\r\nHost: 1.1.1.1\r\n\r\n")
        await writer.drain()
        assert (await reader.read(1024)).startswith(b"HTTP/1.1 407")
        writer.close()
        for url in [
            "https://127.0.0.1/",
            "https://169.254.169.254/",
            "http://127.0.0.1:8088/api/health",
            f"http://127.0.0.1:8088/lab/{uid()}/",
        ]:
            async with httpx.AsyncClient(
                proxy=f"http://{aid}:{proxy.password}@127.0.0.1:{proxy.port}",
                timeout=4,
                trust_env=False,
            ) as client:
                try:
                    response = await client.get(url)
                except httpx.ProxyError:
                    pass
                else:
                    assert response.status_code == 502
        assert proxy.connections == 0 and proxy.bytes_forwarded == 0
    finally:
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("LEAD_BROWSER_TESTS") != "1",
    reason="actual browser fail-closed integration",
)
async def test_actual_browser_never_falls_back_after_bound_proxy_failure(monkeypatch):
    monkeypatch.setattr(domain, "emit", lambda *a, **k: None)
    aid = domain.create_agent()
    gateway = Gateway()
    # Reserve then close a port so the configured upstream is genuinely unavailable.
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    monkeypatch.setenv(
        "LEAD_PROXY_" + aid.replace("-", "").upper(), f"http://127.0.0.1:{port}"
    )
    try:
        actor = await gateway.get(aid)
        with pytest.raises(Exception):
            await actor.page.goto(
                "https://seka.one", wait_until="domcontentloaded", timeout=15000
            )
        assert actor.forward_proxy.upstream_failures > 0
        assert actor.forward_proxy.connections == 0
        assert actor.forward_proxy.bytes_forwarded == 0
        assert not actor.forward_proxy.cache
    finally:
        await gateway.close(aid)
        domain.set_control(aid, "archive")


@pytest.mark.asyncio
async def test_namespace_proof_requires_private_transport_and_matching_nonce():
    import hashlib
    import hmac
    import json

    proxy = await BrowserForwardProxy(uid()).start()
    payload = json.dumps(
        {
            "netns": "fixture-namespace",
            "only_loopback": True,
            "direct_udp_blocked": True,
        }
    ).encode()
    signature = hmac.new(
        bytes.fromhex(proxy.boundary_nonce), payload, hashlib.sha256
    ).hexdigest()
    try:
        # Even the correct nonce cannot attest through a browser-accessible TCP port.
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
        writer.write(
            f"POST /__lead/attest HTTP/1.1\r\nContent-Length: {len(payload)}\r\nX-Lead-Proof: {signature}\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        assert not (await reader.read(1024)).startswith(b"HTTP/1.1 200")
        writer.close()
        await writer.wait_closed()
        # A private connection with a forged signature also cannot set the status.
        reader, writer = await asyncio.open_unix_connection(str(proxy.socket_path))
        writer.write(
            f"POST /__lead/attest HTTP/1.1\r\nContent-Length: {len(payload)}\r\nX-Lead-Proof: wrong\r\n\r\n".encode()
            + payload
        )
        await writer.drain()
        assert not (await reader.read(1024)).startswith(b"HTTP/1.1 200")
        writer.close()
        await writer.wait_closed()
        assert proxy.boundary is None
    finally:
        await proxy.close()
