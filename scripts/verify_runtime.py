"""Live local end-to-end test with the configured model. Synthetic test businesses
are archived afterward and never contribute to live acquisition statistics.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services/api"))
import httpx
from lead_app.config import CONFIG, STATE
from temporalio.client import Client, WorkflowExecutionStatus


async def main():
    ids = []
    report = {"ok": False, "checks": []}
    started = time.monotonic()
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:3100",
        timeout=120,
        headers={"Origin": "http://127.0.0.1:3100", "X-Lead-Request": "1"},
        trust_env=False,
    ) as client:

        async def get(path):
            r = await client.get("/api" + path)
            r.raise_for_status()
            return r.json()

        async def post(path, body=None):
            r = await client.post("/api" + path, json=body or {})
            r.raise_for_status()
            return r.json()

        async def wait(aid, predicate, timeout=120):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                d = await get("/agents/" + aid)
                if predicate(d):
                    return d
                await asyncio.sleep(0.5)
            raise AssertionError("Timed out: " + aid)

        try:
            await post("/session")
            health = await get("/health")
            assert all(
                health[k] for k in ("database", "worker", "browser", "model_configured")
            ), health
            for _ in range(3):
                ids.append((await post("/agents"))["id"])
            descriptions = [
                "本地验收产品 Alpha：面向全球独立开发者的客户线索管理工具，帮助小团队整理销售机会。核心优势是简单、无需培训。请整理业务信息，后续内容用英文，不要编造任何价格或效果。",
                "本地验收产品 Beta：面向设计师的素材收藏工具，整理灵感和设计参考。核心优势是分类清晰。请整理业务信息，后续内容用中文，不要编造任何价格或效果。",
                "本地验收产品 Gamma：面向播客创作者的音频笔记工具，整理节目片段和笔记。请整理业务信息，后续内容用中文，不要编造任何价格或效果。",
            ]
            await asyncio.gather(
                *(
                    post(
                        "/agents/" + aid + "/messages",
                        {"text": text, "client_message_id": str(uuid4())},
                    )
                    for aid, text in zip(ids, descriptions)
                )
            )
            details = await asyncio.gather(
                *(
                    wait(
                        aid,
                        lambda d: (
                            d["business"]
                            and all(
                                m.get("job_status") == "complete"
                                for m in d["messages"]
                                if m["role"] == "user"
                            )
                        ),
                    )
                    for aid in ids
                )
            )
            assert any(
                term in details[0]["business"]["description"].lower()
                for term in ("客户", "customer", "sales", "lead management")
            )
            assert any(
                term in details[1]["business"]["description"].lower()
                for term in ("素材", "设计", "design", "inspiration")
            )
            assert any(
                term in details[2]["business"]["description"].lower()
                for term in ("音频", "播客", "audio", "podcast")
            )
            report["checks"].append("three_parallel_real_model_conversations")
            await post(
                "/agents/" + ids[0] + "/messages",
                {
                    "text": "请记住我的明确偏好：只用英文发布内容，语气克制，不夸大产品效果。",
                    "client_message_id": str(uuid4()),
                },
            )
            a = await wait(
                ids[0],
                lambda d: (
                    bool(d["memory"])
                    and all(
                        m.get("job_status") == "complete"
                        for m in d["messages"]
                        if m["role"] == "user"
                    )
                ),
            )
            b = await get("/agents/" + ids[1])
            assert any(
                any(
                    term in m["body"].get("text", "").lower()
                    for term in ("英文", "english")
                )
                for m in a["memory"]
            )
            assert not any("只用英文" in m["body"].get("text", "") for m in b["memory"])
            report["checks"].append("preference_persisted_and_agent_scoped")
            for aid in ids:
                await post("/agents/" + aid + "/lab/connect")
                await post("/agents/" + aid + "/control", {"action": "start"})
            results = await asyncio.gather(
                *(
                    wait(
                        aid,
                        lambda d: any(t["state"] == "succeeded" for t in d["tasks"]),
                        180,
                    )
                    for aid in ids
                )
            )
            for aid, d in zip(ids, results):
                await post("/agents/" + aid + "/control", {"action": "pause"})
                posts = await get("/agents/" + aid + "/lab/posts")
                assert len(posts) == 1
                assert (
                    d["metrics"]["published"] == 0
                    and d["metrics"]["qualified_visits"] is None
                )
            report["checks"].append(
                "autonomous_model_plan_to_real_browser_publish_to_confirmation"
            )
            report["checks"].append("test_activity_excluded_from_acquisition_metrics")
            report["ok"] = True
            report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        finally:
            for aid in ids:
                try:
                    await post("/agents/" + aid + "/control", {"action": "archive"})
                except Exception:
                    pass
            if report["ok"]:
                temporal = await Client.connect(CONFIG["temporal_address"])
                for _ in range(20):
                    states = await asyncio.gather(
                        *(
                            temporal.get_workflow_handle("supervisor:" + aid).describe()
                            for aid in ids
                        )
                    )
                    if all(
                        state.status == WorkflowExecutionStatus.COMPLETED
                        for state in states
                    ):
                        report["checks"].append(
                            "archived_supervisors_finish_without_further_work"
                        )
                        break
                    await asyncio.sleep(0.5)
                else:
                    report["ok"] = False
            (STATE / "runtime-verification.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2)
            )
    assert report["ok"], report
    print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
