"""Single production service entry; migration and provisioning are separate jobs."""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.auth import settings
from lead_app.config import ROOT, STATE, env
from lead_app.temporal_client import connection_options


def preflight():
    values = env()
    if values.get("LEAD_DEPLOYMENT_MODE") != "production":
        raise ValueError("此入口只用于正式服务")
    settings()
    connection_options()
    if values.get("LEAD_DB_SSLMODE") != "verify-full":
        raise ValueError("正式数据库需要验证服务器证书")
    if not values.get("LEAD_BACKUP_S3_URI"):
        raise ValueError("正式服务需要已配置的异地加密备份")
    key = Path(
        values.get("LEAD_BACKUP_KEY_FILE") or STATE / "keys/backup.key"
    ).resolve()
    if key.is_relative_to(ROOT):
        raise ValueError("备份密钥需要独立托管")
    if os.geteuid() == 0:
        raise ValueError("正式服务需要独立的普通系统用户")


def main():
    preflight()
    name = sys.argv[1] if len(sys.argv) == 2 else ""
    commands = {
        "api": [
            sys.executable,
            "-m",
            "uvicorn",
            "lead_app.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8088",
            "--no-access-log",
        ],
        "browser": [
            sys.executable,
            "-m",
            "uvicorn",
            "lead_app.browser:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8089",
            "--no-access-log",
        ],
        "worker": [sys.executable, "-m", "lead_app.worker"],
        "maintenance": [sys.executable, "-m", "lead_app.maintenance"],
        "web": [
            "node",
            str(ROOT / "apps/web/node_modules/next/dist/bin/next"),
            "start",
            "--hostname",
            "127.0.0.1",
            "--port",
            "3100",
        ],
    }
    if name not in commands:
        raise ValueError("服务名称不可用")
    os.chdir(ROOT / "apps/web" if name == "web" else ROOT)
    os.environ["PYTHONPATH"] = str(ROOT / "services/api")
    os.execvp(commands[name][0], commands[name])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            str(error) if isinstance(error, ValueError) else type(error).__name__,
            file=sys.stderr,
        )
        raise SystemExit(78) from None
