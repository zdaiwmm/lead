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


def database_kwargs(owner=False):
    return dict(
        host="127.0.0.1",
        port=55432,
        dbname="lead",
        user="lead_owner" if owner else "lead_runtime",
        password=CONFIG["owner_password" if owner else "runtime_password"],
        connect_timeout=5,
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
