"""Exports and previews are read-only. Purge requires an exact explicit ID."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app import lifecycle
from lead_app.scope import tenant_scope


def main():
    p = argparse.ArgumentParser()
    p.add_argument(
        "action", choices=["export", "purge-preview", "purge", "finish-purge"]
    )
    p.add_argument("--tenant", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--output")
    p.add_argument("--confirm-agent")
    a = p.parse_args()
    if a.action == "export":
        if not a.output:
            raise ValueError("需要指定新的导出文件")
        with tenant_scope(a.tenant):
            data = lifecycle.export_archive(a.agent)
        with Path(a.output).open("xb") as f:
            import os

            os.chmod(a.output, 0o600)
            f.write(data)
        result = {"exported": True}
    elif a.action == "finish-purge":
        result = lifecycle.finish_purge(a.tenant, a.agent)
    else:
        if a.action == "purge" and a.confirm_agent != a.agent:
            raise ValueError("清除操作需要再次提供同一 Agent ID")
        result = lifecycle.purge(a.tenant, a.agent, apply=a.action == "purge")
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(
            json.dumps(
                {
                    "error": str(e)
                    if isinstance(e, (ValueError, LookupError))
                    else type(e).__name__
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from None
