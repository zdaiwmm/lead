import asyncio
import os

import httpx
import pytest
from lead_app import domain
from lead_app.browser import Gateway
from lead_app.db import uid
from lead_app.forward_proxy import BrowserForwardProxy


@pytest.mark.asyncio
@pytest.mark.parametrize('target_port',[80,443])
async def test_old_tunnel_cannot_send_after_control_generation_changes(monkeypatch,target_port):
    received = bytearray()

    async def echo(reader, writer):
        try:
            while data := await reader.read(1024):
                received.extend(data)
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    generation = ["first"]
    proxy = BrowserForwardProxy(uid(), forward_guard=lambda: generation[0])
    await proxy.start()

    async def connect(ip, port):
        return await asyncio.open_connection("127.0.0.1", upstream.sockets[0].getsockname()[1])

    monkeypatch.setattr(proxy, "connect", connect)
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        writer.write((f"CONNECT 1.1.1.1:{target_port} HTTP/1.1\r\n"
                      f"Proxy-Authorization: {proxy.auth}\r\n\r\n").encode())
        await writer.drain()
        assert b"200" in await reader.readuntil(b"\r\n\r\n")
        writer.write(b"first")
        await writer.drain()
        assert await reader.readexactly(5) == b"first"
        generation[0] = "second"
        writer.write(b"stale")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert received == b"first"
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()
        upstream.close()
        await upstream.wait_closed()


async def test_node_api_request_posts_once_through_authenticated_public_http_tunnel(monkeypatch):
    from playwright.async_api import async_playwright

    bodies=[]
    async def origin(reader,writer):
        try:
            header=await reader.readuntil(b'\r\n\r\n')
            length=int(next(line.split(b':',1)[1] for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:')))
            bodies.append(await reader.readexactly(length))
            writer.write(b'HTTP/1.1 201 Created\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
            await writer.drain()
        finally:
            writer.close()
    server=await asyncio.start_server(origin,'127.0.0.1',0)
    proxy=await BrowserForwardProxy(uid()).start()
    destinations=[]
    async def connect(ip,port):
        assert ip=='1.1.1.1' and port==80
        destinations.append((ip,port))
        return await asyncio.open_connection('127.0.0.1',server.sockets[0].getsockname()[1])
    monkeypatch.setattr(proxy,'connect',connect)
    try:
        async with async_playwright() as manager:
            request=await manager.request.new_context(proxy=proxy.browser_proxy)
            try:
                response=await request.post('http://1.1.1.1/submission',data=b'one-original-body',max_retries=0,max_redirects=0,timeout=4000)
                assert response.status==201 and await response.text()=='OK'
            finally:
                await request.dispose()
        assert destinations==[('1.1.1.1',80)] and bodies==[b'one-original-body']
    finally:
        await proxy.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize('authority',['1.1.1.1:81','1.1.1.1:8443','127.0.0.1:80','[::1]:80','169.254.169.254:80'])
async def test_connect_rejects_private_and_non_web_ports_before_upstream(monkeypatch,authority):
    proxy=await BrowserForwardProxy(uid()).start()
    destinations=[]
    async def connect(ip,port):
        destinations.append((ip,port))
        raise AssertionError('A denied CONNECT must not reach its upstream')
    monkeypatch.setattr(proxy,'connect',connect)
    reader,writer=await asyncio.open_connection('127.0.0.1',proxy.port)
    try:
        writer.write((f'CONNECT {authority} HTTP/1.1\r\nProxy-Authorization: {proxy.auth}\r\n\r\n').encode())
        await writer.drain()
        assert not (await asyncio.wait_for(reader.read(1024),2)).startswith(b'HTTP/1.1 200')
        assert destinations==[]
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [False, True])
async def test_close_with_active_connect_and_partial_header(monkeypatch, connected):
    """Shutdown must close accepted sockets before waiting for the listener."""
    upstream_writers = []

    async def upstream(reader, writer):
        upstream_writers.append(writer)
        writer.write(b"BEFORE")
        await writer.drain()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    proxy = await BrowserForwardProxy(uid()).start()
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    try:
        if connected:
            async def connect(ip, requested_port):
                return await asyncio.open_connection("127.0.0.1", port)

            monkeypatch.setattr(proxy, "connect", connect)
            writer.write(("CONNECT 1.1.1.1:443 HTTP/1.1\r\n"
                          f"Proxy-Authorization: {proxy.auth}\r\n\r\n").encode())
            await writer.drain()
            assert b"200" in await reader.readuntil(b"\r\n\r\n")
            assert await reader.readexactly(6) == b"BEFORE"
        else:
            writer.write(b"CONNECT ")
            await writer.drain()
        for _ in range(100):
            if proxy.tasks:
                break
            await asyncio.sleep(.01)
        assert proxy.tasks
        await asyncio.wait_for(proxy.close(), 1)
        assert not proxy.tasks
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert reader.at_eof()
    finally:
        for task in list(proxy.tasks):
            task.cancel()
        await asyncio.gather(*list(proxy.tasks), return_exceptions=True)
        writer.close()
        await writer.wait_closed()
        for remote in upstream_writers:
            remote.close()
        await proxy.close()
        server.close()
        await server.wait_closed()


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


@pytest.mark.asyncio
async def test_idle_preconnect_closes_without_an_unsolicited_http_error(monkeypatch):
    # Accelerate the actual header deadline, not the network result or parser.
    original=asyncio.wait_for
    async def header_deadline(awaitable,timeout):
        if timeout==15 and getattr(getattr(awaitable,'cr_code',None),'co_name',None)=='readuntil':
            timeout=.03
        return await original(awaitable,timeout)
    monkeypatch.setattr(asyncio,'wait_for',header_deadline)
    proxy=await BrowserForwardProxy(uid()).start()
    reader,writer=await asyncio.open_connection('127.0.0.1',proxy.port)
    try:
        assert await asyncio.wait_for(reader.read(),1)==b''
        assert proxy.connections==0
    finally:
        writer.close()
        await writer.wait_closed()
        await proxy.close()
