"""Operator inventory; credentials arrive through stdin and are never printed."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import psycopg
from lead_app.config import database_kwargs
from lead_app.managed_network import add, inventory


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["add", "list", "disable", "enable", "quarantine"])
    p.add_argument("--id")
    args = p.parse_args()
    if args.action == "add":
        record = json.load(sys.stdin)
        result = add(**record)
    elif args.action == "list":
        result = inventory()
    else:
        from uuid import UUID

        with psycopg.connect(**database_kwargs(True)) as c:
            result = c.execute(
                "UPDATE lead.proxy_pool SET state=%s WHERE id=%s RETURNING id,label,state",
                (
                    {
                        "disable": "disabled",
                        "enable": "available",
                        "quarantine": "quarantined",
                    }[args.action],
                    str(UUID(args.id)),
                ),
            ).fetchone()
    print(json.dumps(result, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(
            json.dumps(
                {
                    "error": str(error)
                    if isinstance(error, ValueError)
                    else type(error).__name__
                }
            )
        )
        raise SystemExit(1) from None
