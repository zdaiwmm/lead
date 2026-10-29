import json
import os
from pathlib import Path

from dotenv import dotenv_values

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

ROOT = Path(__file__).resolve().parents[3]
STATE = ROOT / ".state"
CONFIG = json.loads((STATE / "config.json").read_text())
TENANT = str(__import__("uuid").UUID(CONFIG["owner_id"]))


def env():
    # Read on every call: filling in the key does not require a restart.
    return {**dotenv_values(ROOT / ".env"), **os.environ}


def model_config():
    values = env()
    return {
        "key": values.get("DEEPSEEK_API_KEY") or "",
        "base_url": values.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        "model": values.get("DEEPSEEK_MODEL") or "deepseek-flash",
    }


def database_kwargs(owner=False, auth=False):
    values = env()
    kind = "OWNER" if owner else "AUTH" if auth else ""
    username_key = "LEAD_DB_" + kind + "_USER" if kind else "LEAD_DB_USER"
    password_key = "LEAD_DB_" + kind + "_PASSWORD" if kind else "LEAD_DB_PASSWORD"
    default_user = "lead_owner" if owner else "lead_auth" if auth else "lead_runtime"
    config_key = (
        "owner_password" if owner else "auth_password" if auth else "runtime_password"
    )
    extra = (
        {"sslrootcert": values["LEAD_DB_CA_FILE"]}
        if values.get("LEAD_DB_CA_FILE")
        else {}
    )
    return dict(
        host=values.get("LEAD_DB_HOST") or "127.0.0.1",
        port=int(values.get("LEAD_DB_PORT") or 55432),
        dbname=values.get("LEAD_DB_NAME") or "lead",
        user=values.get(username_key) or default_user,
        password=values.get(password_key) or CONFIG[config_key],
        connect_timeout=5,
        sslmode=values.get("LEAD_DB_SSLMODE") or "prefer",
        **extra,
    )


def proxy_for(agent_id):
    value = env().get("LEAD_PROXY_" + str(agent_id).replace("-", "").upper())
    if not value:
        return None
    from urllib.parse import unquote, urlsplit

    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https", "socks5")
        or not parsed.hostname
        or not parsed.port
    ):
        raise ValueError("代理配置无效")
    proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
        proxy["password"] = unquote(parsed.password or "")
    return proxy
