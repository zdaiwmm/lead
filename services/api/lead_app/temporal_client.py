"""Shared local/production Temporal connection without plaintext production RPC."""

from pathlib import Path

from temporalio.client import Client
from temporalio.service import TLSConfig

from .config import CONFIG, env


def connection_options():
    values = env()
    address = values.get("LEAD_TEMPORAL_ADDRESS") or CONFIG["temporal_address"]
    production = values.get("LEAD_DEPLOYMENT_MODE") == "production"
    enabled = values.get("LEAD_TEMPORAL_TLS") == "1"
    if production and not enabled:
        raise ValueError("生产任务服务需要 TLS 连接")
    options = {
        "target_host": address,
        "namespace": values.get("LEAD_TEMPORAL_NAMESPACE") or "default",
    }
    cert, key = (
        values.get("LEAD_TEMPORAL_CERT_FILE"),
        values.get("LEAD_TEMPORAL_KEY_FILE"),
    )
    if bool(cert) != bool(key):
        raise ValueError("任务服务证书和私钥需要成对配置")
    if enabled:
        ca = values.get("LEAD_TEMPORAL_CA_FILE")
        options["tls"] = TLSConfig(
            server_root_ca_cert=Path(ca).read_bytes() if ca else None,
            client_cert=Path(cert).read_bytes() if cert else None,
            client_private_key=Path(key).read_bytes() if key else None,
        )
    if api_key := values.get("LEAD_TEMPORAL_API_KEY"):
        if not enabled:
            raise ValueError("任务服务凭据不能通过明文连接发送")
        options["api_key"] = api_key
    return options


async def connect():
    return await Client.connect(**connection_options())
