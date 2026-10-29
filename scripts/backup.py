"""Operator CLI: encrypted backups and non-overwriting recovery."""

import argparse
import json
import signal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.backup_crypto import key_init, load_key
from lead_app.backups import (
    backup_database,
    backup_profile,
    prune_managed,
    restore_database,
    restore_profile,
    send_offsite,
)
from lead_app.config import STATE, env


def interrupted(*args):
    raise KeyboardInterrupt


def main():
    p = argparse.ArgumentParser(description="lead encrypted backup and recovery")
    p.add_argument(
        "action",
        choices=[
            "key-init",
            "database",
            "profile",
            "restore-database",
            "restore-profile",
        ],
    )
    p.add_argument(
        "--key-file",
        default=env().get("LEAD_BACKUP_KEY_FILE") or str(STATE / "keys/backup.key"),
    )
    p.add_argument("--folder", default=str(STATE / "backups"))
    p.add_argument("--agent")
    p.add_argument("--source")
    p.add_argument("--database")
    p.add_argument("--managed", action="store_true")
    p.add_argument("--s3-uri", default=env().get("LEAD_BACKUP_S3_URI"))
    args = p.parse_args()
    if args.action == "key-init":
        result = {"key_id": key_init(args.key_file), "created": True}
    else:
        key = load_key(args.key_file)
        if args.action == "database":
            result = backup_database(args.folder, key, managed=args.managed)
        elif args.action == "profile":
            result = backup_profile(
                args.agent, args.folder, key, automated=args.managed
            )
        elif args.action == "restore-database":
            result = restore_database(args.source, key, args.database or "")
        else:
            result = restore_profile(args.source, key)
        if args.action in ("database", "profile"):
            if args.s3_uri:
                result["remote"] = send_offsite(result, args.folder, args.s3_uri)
            if args.managed:
                result["expired_count"] = len(prune_managed(args.folder))
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print(json.dumps({"error": "interrupted"}))
        raise SystemExit(130) from None
    except Exception as error:
        print(
            json.dumps(
                {
                    "error_code": getattr(error, "code", type(error).__name__),
                    "error": str(error)
                    if isinstance(error, (ValueError, RuntimeError))
                    else type(error).__name__,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
