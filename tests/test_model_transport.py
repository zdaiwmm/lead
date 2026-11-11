"""Dedicated model routes preserve TLS and fail without a direct-network fallback."""

import asyncio
import socket
from uuid import uuid4

import httpx2
import pytest
from lead_app.model_transport import model_http_client
from openai import AsyncOpenAI


def settings(aid, path, provider="openai"):
    return {f"LEAD_MODEL_SOCKET_{aid.hex.upper()}_{provider.upper()}": path}


def endpoint(provider="openai", url=None):
    return {"provider": provider, "base_url": url or f"https://api.{provider}.com/v1"}


def test_configuration_is_scoped_to_agent_and_provider():
    aid = uuid4()
    values = settings(aid, "/run/model.sock")
    assert model_http_client(uuid4(), endpoint(), 1, values) is None
    assert model_http_client(aid, endpoint("deepseek"), 1, values) is None
    assert model_http_client(None, endpoint(), 1, values) is None
    assert model_http_client(aid, endpoint(), 1, {}) is None


@pytest.mark.parametrize("path", [None, "", "relative.sock", "/run/../private.sock", "/run/bad\0.sock"])
def test_invalid_explicit_route_never_uses_default_transport(path):
    aid = uuid4()
    with pytest.raises(ValueError):
        model_http_client(aid, endpoint(), 1, settings(aid, path))


@pytest.mark.parametrize("url", [
    "http://api.openai.com/v1", "https://api.deepseek.com/v1",
    "https://other.example/v1", "https://api.openai.com:8443/v1",
    "https://user:password@api.openai.com/v1", "https://api.openai.com/v1?secret=x",
    "https://api.openai.com/v1#fragment",
])
def test_rejects_other_destinations_before_creating_client(url):
    aid = uuid4()
    with pytest.raises(ValueError):
        model_http_client(aid, endpoint(url=url), 1, settings(aid, "/run/model.sock"))


@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_missing_socket_fails_without_dns_or_environment_proxy(tmp_path, monkeypatch, provider):
    aid = uuid4()
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")

    def forbidden_dns(*args, **kwargs):
        raise AssertionError("Dedicated model route attempted direct DNS")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    client = model_http_client(aid, endpoint(provider), 1, settings(aid, str(tmp_path / "missing.sock"), provider))
    async with client:
        assert client.follow_redirects is False
        with pytest.raises(httpx2.ConnectError):
            await client.get(endpoint(provider)["base_url"] + "/models")


async def test_sdk_accepts_dedicated_client_and_closes_it():
    aid = uuid4()
    client = model_http_client(aid, endpoint(), 1, settings(aid, "/run/not-connected.sock"))
    async with AsyncOpenAI(api_key="fixture-only", http_client=client) as sdk:
        assert sdk._client is client
    assert client.is_closed


async def test_untrusted_socket_server_receives_tls_only(tmp_path):
    aid = uuid4()
    # macOS has a 104-byte Unix socket path limit.
    import tempfile

    with tempfile.TemporaryDirectory(prefix="lead-model-", dir="/tmp") as directory:
        path = directory + "/tls.sock"
        received = asyncio.get_running_loop().create_future()

        async def handler(reader, writer):
            payload = await reader.read(4096)
            if not received.done():
                received.set_result(payload)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(handler, path)
        async with server:
            async with model_http_client(aid, endpoint(), 2, settings(aid, path)) as client:
                with pytest.raises(httpx2.ConnectError):
                    await client.get("https://api.openai.com/v1/models", headers={"Authorization": "Bearer fixture-secret"})
            payload = await asyncio.wait_for(received, timeout=2)
            assert payload.startswith(b"\x16\x03")
            assert b"fixture-secret" not in payload
            assert b"GET /" not in payload
