"""Small opt-in live DeepSeek smoke test; never prints a credential/provider body."""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.config import STATE, model_config
from lead_app.model import Decision, run_model


async def main():
    if not model_config()["key"]:
        raise SystemExit("DeepSeek key is not configured.")
    started = time.monotonic()
    try:
        result = await run_model(
            Decision,
            {
                "messages": [
                    {
                        "role": "user",
                        "text": "我想推广一个面向全球独立开发者的客户线索管理工具。产品叫 Orbit，主要帮助小团队整理销售机会。请先了解我的业务，不要连接社媒账号。",
                    }
                ],
                "server_context": {
                    "desired_state": "draft",
                    "accounts": [],
                    "metrics": {"visits": None},
                },
            },
        )
    except Exception as e:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(e).__name__,
                    "status_code": getattr(e, "status_code", None),
                }
            )
        )
        raise SystemExit(1) from None
    evidence = {
        "ok": True,
        "model": model_config()["model"],
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "decision": result.model_dump(),
    }
    (STATE / "model-verification.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    print(json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
