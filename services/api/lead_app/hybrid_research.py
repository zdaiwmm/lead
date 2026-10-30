"""Browser-use explores public pages; all UI actions use lead's read-only gateway.

Chromium/CDP runs in a separate, cookie-empty context. Camoufox keeps ownership of
social accounts. Neither browser-use nor its model receives publishing tools.
"""

import asyncio
import json
import os

import httpx

from .budget import reserve, settle
from .config import ROOT, STATE, env
from .db import uid
from .research import ResearchPage, begin_run, finish_run, public_url
from .security import internal_token

RUNTIME = STATE / "browser-use-venv" / "bin" / "python"


def available():
    return env().get("LEAD_BROWSER_USE_ENABLED") == "1" and RUNTIME.is_file()


class HybridResearchPage(ResearchPage):
    def __init__(self, actor, run_id):
        super().__init__(actor, run_id)
        self.playwright = None
        self.cdp_url = None

    async def ensure(self):
        self.community_discovery = self.gate()["mode"] == "community_discovery"
        if self.page and not self.page.is_closed():
            return
        from playwright.async_api import async_playwright

        folder = STATE / "browser-use-profiles" / self.run_id
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = await async_playwright().start()
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(folder),
                headless=True,
                accept_downloads=False,
                service_workers="block",
                viewport={"width": 1280, "height": 800},
                proxy=self.actor.forward_proxy.browser_proxy,
                args=[
                    "--remote-debugging-port=0",
                    "--remote-debugging-address=127.0.0.1",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            )
            self.page = self.context.pages[0]
            await self.context.route("**/*", self.read_only_route)

            async def deny_websocket(ws):
                await ws.close(code=1008, reason="Research is read-only")

            await self.context.route_web_socket("**/*", deny_websocket)
            self.page.set_default_timeout(10000)
            port = (folder / "DevToolsActivePort").read_text().splitlines()[0]
            if not port.isdigit():
                raise ValueError("探索浏览器尚未就绪")
            self.cdp_url = "http://127.0.0.1:" + port
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.context:
            await self.context.close()
            self.context = None
        if self.playwright:
            await self.playwright.stop()
            self.playwright = None


async def explore(aid, url, goal, *, mode="research", message_id=None):
    from .admin_settings import for_agent
    from .config import model_reference, resolve_model

    settings, settings_version = for_agent(aid, "models")
    if (
        not settings["tools"]["public_web"]
        or not settings["tools"]["advanced_research"]
        or not settings["max_tool_calls"]
    ):
        raise ValueError("进阶网页研究已在后台关闭")
    if not available():
        return {"error": "BROWSER_USE_UNAVAILABLE", "sources": []}
    connection = resolve_model(
        settings["task_models"]["research"] or settings["default_model"]
    )
    if not connection["key"]:
        raise ValueError("研究模型尚未配置服务端凭据")
    url = public_url(url)
    reservation = reserve(aid, uid(), 100000, model_reference(connection), connection["provider"])
    rid = begin_run(aid, goal, message_id=message_id, mode=mode)
    child = None
    result = None
    try:
        async with httpx.AsyncClient(timeout=60, trust_env=False) as client:
            headers = {"Authorization": "Bearer " + internal_token()}
            response = await client.post(
                f"http://127.0.0.1:8089/agents/{aid}/research/{rid}/hybrid",
                headers=headers,
            )
            response.raise_for_status()
            payload = {
                "agent_id": aid,
                "run_id": rid,
                "url": url,
                "goal": goal[:6000],
                "cdp_url": response.json()["cdp_url"],
                "gateway_token": internal_token(),
                "model": connection,
                "max_steps": min(
                    6, settings["max_tool_calls"], settings["max_model_requests"]
                ),
                "max_requests": settings["max_model_requests"],
                "max_output_tokens": settings["max_output_tokens"],
                "timeout_seconds": min(45, settings["timeout_seconds"]),
                "workdir": str(STATE / "browser-use-runs" / rid),
            }
            child = await asyncio.create_subprocess_exec(
                str(RUNTIME),
                str(ROOT / "scripts/browser_use_runner.py"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={
                    **os.environ,
                    "ANONYMIZED_TELEMETRY": "false",
                    "BROWSER_USE_CLOUD_SYNC": "false",
                    "BROWSER_USE_VERSION_CHECK": "false",
                    "BROWSER_USE_SETUP_LOGGING": "false",
                },
            )
            stdout, _ = await asyncio.wait_for(
                child.communicate(json.dumps(payload).encode()), timeout=100
            )
            if child.returncode:
                raise RuntimeError("browser_use_run_failed")
            result = json.loads(stdout)
            folder = STATE / "browser-use-runs" / rid
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False))
            usage = result.get("usage")
            if usage and usage["entry_count"]:
                settle(
                    aid,
                    reservation,
                    actual=usage["total_tokens"],
                    details={
                        "model": model_reference(connection),
                        "provider": connection["provider"],
                        "configuration_version": settings_version,
                        "input_tokens": usage["total_prompt_tokens"],
                        "output_tokens": usage["total_completion_tokens"],
                        "cache_read_tokens": usage["total_prompt_cached_tokens"],
                        "requests": usage["entry_count"],
                        "unmeasured_requests": usage.get("unmeasured_requests", 0),
                        "cache_breakdown_verified": usage.get(
                            "cache_breakdown_verified", False
                        ),
                    },
                    incomplete=bool(usage.get("unmeasured_requests")),
                )
            else:
                settle(
                    aid,
                    reservation,
                    known_unbilled=bool(usage and not usage.get("unmeasured_requests")),
                )
            sources = finish_run(
                aid, rid, "succeeded" if result["success"] else "failed"
            )
            return {
                "engine": "browser-use",
                "run_id": rid,
                "text": result["text"],
                "sources": sources,
                "read_only": True,
                "success": result["success"],
            }
    except BaseException:
        if child and child.returncode is None:
            child.kill()
            await child.wait()
        settle(aid, reservation)
        finish_run(aid, rid, "failed")
        raise
    finally:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            try:
                await client.post(
                    f"http://127.0.0.1:8089/agents/{aid}/research/{rid}/close",
                    headers={"Authorization": "Bearer " + internal_token()},
                )
            except httpx.HTTPError:
                pass
