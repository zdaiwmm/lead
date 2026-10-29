"""Exercise authenticated SSE through the real local Next.js proxy."""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
from lead_app.config import STATE


async def main():
    aid = None
    report = {"ok": False, "checks": []}
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:3100",
        timeout=15,
        trust_env=False,
        headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"},
    ) as client:
        try:
            assert (await client.get("/api/events")).status_code == 401
            (await client.post("/api/session")).raise_for_status()
            async with (
                asyncio.timeout(15),
                client.stream("GET", "/api/events") as response,
            ):
                response.raise_for_status()
                assert response.headers["content-type"].startswith("text/event-stream")
                initial = None
                async for line in response.aiter_lines():
                    if line.startswith("id: "):
                        current = line[4:]
                        if initial and initial != current:
                            report.update(
                                ok=True,
                                checks=[
                                    "unauthenticated_rejected",
                                    "sse_passes_next_proxy_without_buffering",
                                    "changes_delivered_over_open_stream",
                                ],
                            )
                            break
                        if initial is None:
                            initial = current
                            created = await client.post("/api/agents", json={})
                            created.raise_for_status()
                            aid = created.json()["id"]
                assert report["ok"]
        finally:
            if aid:
                response = await client.post(
                    f"/api/agents/{aid}/control", json={"action": "archive"}
                )
                response.raise_for_status()
            (STATE / "events-verification.json").write_text(
                json.dumps(report, indent=2)
            )
    print(json.dumps(report))


if __name__ == "__main__":
    asyncio.run(main())
