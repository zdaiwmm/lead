"""Optional operator-owned, per-Agent routes to official model endpoints."""

from pathlib import PurePosixPath
from urllib.parse import urlsplit
from uuid import UUID

import httpx2


def model_http_client(agent_id, config, timeout, values):
    if agent_id is None:
        return None
    provider = config["provider"]
    try:
        agent_key = UUID(str(agent_id)).hex.upper()
    except ValueError:
        # Only real Agent UUIDs can select an operator-owned route. Leave
        # unconfigured callers (including test doubles) on their existing path.
        return None
    key = f"LEAD_MODEL_SOCKET_{agent_key}_{provider.upper()}"
    if key not in values:
        return None
    path = values[key]
    if not isinstance(path, str) or not path or "\0" in path:
        raise ValueError("模型专用通道路径无效")
    socket_path = PurePosixPath(path)
    if not socket_path.is_absolute() or ".." in socket_path.parts:
        raise ValueError("模型专用通道必须使用绝对路径")
    endpoints = {"openai": "api.openai.com", "deepseek": "api.deepseek.com"}
    endpoint = urlsplit(config["base_url"])
    if (
        provider not in endpoints
        or endpoint.scheme != "https"
        or endpoint.hostname != endpoints[provider]
        or endpoint.port not in (None, 443)
        or endpoint.username is not None
        or endpoint.password is not None
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("模型专用通道只允许对应服务的官方 HTTPS 地址")
    transport = httpx2.AsyncHTTPTransport(uds=path, trust_env=False, retries=0)
    return httpx2.AsyncClient(
        transport=transport, timeout=timeout, trust_env=False, follow_redirects=False
    )
