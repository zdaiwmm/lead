"""A cancelled established CONNECT closes TLS without injecting HTTP bytes."""
import asyncio
from contextlib import suppress

import pytest
from lead_app.db import uid
from lead_app.forward_proxy import BrowserForwardProxy
from test_native_manager_death_product import tls_contexts


@pytest.mark.asyncio
async def test_close_of_established_connect_is_tls_eof_not_a_second_http_response(monkeypatch, tmp_path):
    server_tls, client_tls = tls_contexts(tmp_path)
    finished = asyncio.Event()

    async def source(reader, writer):
        try:
            assert await reader.readline() == b'BEFORE\n'
            writer.write(b'BEFORE\n')
            await writer.drain()
            await reader.read()
        except OSError:
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            finished.set()

    server = await asyncio.start_server(source, '127.0.0.1', 0, ssl=server_tls)
    proxy = await BrowserForwardProxy(uid(), forward_guard=lambda: 'original-control').start()

    async def connect(ip, port):
        assert ip == '93.184.215.230' and port == 443
        return await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])

    monkeypatch.setattr(proxy, 'connect', connect)
    reader, writer = await asyncio.open_connection('127.0.0.1', proxy.port)
    try:
        writer.write(('CONNECT 93.184.215.230:443 HTTP/1.1\r\n'
            'Host: 93.184.215.230:443\r\nProxy-Authorization: '+proxy.auth+'\r\n\r\n').encode())
        await writer.drain()
        assert b'200 Connection Established' in await reader.readuntil(b'\r\n\r\n')
        await writer.start_tls(client_tls, server_hostname='stream.fixture.test')
        writer.write(b'BEFORE\n')
        await writer.drain()
        assert await asyncio.wait_for(reader.readline(), 2) == b'BEFORE\n'
        await asyncio.wait_for(proxy.close(), 2)
        assert not proxy.tasks
        # An HTTP 502 after CONNECT 200 is plaintext inside the TLS stream. The
        # actual TLS reader must see EOF, without accepting an SSL exception.
        assert await asyncio.wait_for(reader.read(1), 2) == b''
        await asyncio.wait_for(finished.wait(), 2)
    finally:
        writer.close()
        with suppress(Exception):
            await writer.wait_closed()
        await proxy.close()
        server.close()
        await server.wait_closed()
