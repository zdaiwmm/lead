"""Live acceptance through UI API, actual DeepSeek tools and public Camoufox pages.
No mocked website, canned extraction or external write. Test Agent is archived.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import httpx
from lead_app.config import STATE


async def main():
    report = {"ok": False, "checks": []}
    aid = None
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:3100",
        timeout=120,
        trust_env=False,
        headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"},
    ) as client:

        async def post(path, body=None):
            r = await client.post("/api" + path, json=body or {})
            r.raise_for_status()
            return r.json()

        async def read():
            r = await client.get("/api/agents/" + aid)
            r.raise_for_status()
            return r.json()

        async def ask(text):
            m = await post(
                "/agents/" + aid + "/messages",
                {"text": text, "client_message_id": str(uuid4())},
            )
            started = time.monotonic()
            while time.monotonic() - started < 180:
                d = await read()
                msg = next(x for x in d["messages"] if x["id"] == m["id"])
                if msg["job_status"] == "failed":
                    raise AssertionError(msg["error_code"])
                if msg["job_status"] == "complete":
                    return d
                await asyncio.sleep(0.5)
            raise AssertionError("Browser conversation timed out")

        try:
            await post("/session")
            aid = (await post("/agents"))["id"]
            first = await ask(
                "Https://seka.one 请用真实浏览器分析这个网站，提取产品名称、实际功能和受众。不要沿用其他行业的资料。"
            )
            assert first["business"] and first["business"]["description"]
            assert any(
                s["url"].startswith("https://seka.one")
                for s in first["messages"][-1]["source_refs"]
            )
            assert (
                "翻译" in first["business"]["description"]
                or "translation" in first["business"]["description"].lower()
            )
            report["checks"].append("uppercase_url_to_real_rendered_public_website")
            second = await ask(
                "继续实际点击官网的价格说明，查看计价信息。如果没有独立价格页，就根据实际网页说明，不要为了找到一个不存在的页面反复浏览，也不要让我复制网页。"
            )
            refs = second["messages"][-1]["source_refs"]
            assert refs, second["messages"][-1]["content"]["text"]
            runs = []
            for run in second["browser_runs"][:2]:
                r = await client.get(
                    "/api/agents/" + aid + "/browser-runs/" + run["id"]
                )
                r.raise_for_status()
                runs.append(r.json())
            steps = [s for r in runs for s in r["steps"]]
            assert any(
                s["operation"] == "click" and s["state"] == "completed" for s in steps
            ), [(s["operation"], s["state"]) for s in steps]
            assert any(
                "credit" in s.get("page", {}).get("text", "").lower() for s in steps
            ), [(s["operation"], s.get("page", {}).get("url")) for s in steps]
            screenshot = next(
                s["screenshot_url"] for s in reversed(steps) if s.get("screenshot_url")
            )
            r = await client.get(screenshot)
            assert r.status_code == 200 and r.headers["content-type"].startswith(
                "image/"
            )
            report["checks"] += [
                "followup_without_url_reuses_business_website",
                "deepseek_native_tool_call_clicks_real_price_information",
                "rendered_sources_and_browser_screenshots_are_inspectable",
            ]
            report.update(
                ok=True,
                run_ids=[r["id"] for r in runs],
                agent_id=aid,
                answer=second["messages"][-1]["content"]["text"],
            )
            print(json.dumps(report, ensure_ascii=False), flush=True)
        finally:
            if aid:
                try:
                    await post("/agents/" + aid + "/control", {"action": "archive"})
                except httpx.HTTPError:
                    pass
            (STATE / "public-browser-verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )


if __name__ == "__main__":
    asyncio.run(main())
