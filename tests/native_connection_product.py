"""Real Linux manager/DB/SDK login with a deterministic local model transport.

This fixture proves mechanical execution and privacy, never model capability.
It permits browser traffic only to its own disposable HTTP board. The SDK gets
ordinary observed elements; only this test's scripted model knows fixture CSS.
"""

import asyncio
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from lead_app import browser, domain
from lead_app.account_connection import begin, execute_account
from lead_app.connection_control import LoginInput
from lead_app.db import one, tx
from lead_app.forward_proxy import BrowserForwardProxy
from lead_app.growth.contracts import TaskRef
from lead_app.growth.runtime import advance
from lead_app.growth.tools import AccountInput
from native_browser_fixture import BoardState, NativeBoard


async def verify_login(monkeypatch, *, mode="login"):
    from lead_app import native_runner

    model_calls = []

    class FakeModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            model_calls.append(
                {
                    "bytes": len(raw),
                    "fixture_only": True,
                    "username_literal_seen": board.state.username.encode() in raw,
                    "password_literal_seen": board.state.password.encode() in raw,
                }
            )
            request = json.loads(raw)
            parts = []
            for message in request.get("messages", []):
                value = message.get("content")
                if isinstance(value, str):
                    parts.append(value)
                elif isinstance(value, list):
                    parts.extend(
                        item.get("text", "") for item in value if isinstance(item, dict)
                    )
            text = "\n".join(parts)
            import re

            candidates = []
            for line in text.splitlines():
                index = re.search(r"\[(\d+)\]", line)
                if index and any(tag in line.lower() for tag in ("<input", "<button")):
                    candidates.append((int(index[1]), line))
            step = len(model_calls) - 1
            action = {
                "done": {
                    "text": "Controlled transport diagnostic only",
                    "success": True,
                }
            }
            if step in (0, 1):
                field = "username" if step == 0 else "password"
                matches = [
                    index
                    for index, line in candidates
                    if re.search(r"(?:name|type)=[\"\']?" + field + r"\b", line, re.I)
                ]
                if matches:
                    action = {
                        "guarded_input": {
                            "index": matches[-1],
                            "text": "saved_" + field,
                            "clear": True,
                        }
                    }
            elif step == 2:
                matches = [
                    index for index, line in candidates if "<button" in line.lower()
                ]
                if matches:
                    action = {"guarded_click": {"index": matches[-1]}}
            elif step == 3 and mode != "owner_otp":
                action = {
                    "inspect_self": {
                        "self_link_selector": "a[rel~=me]",
                        "acting_subject_selector": "main > p",
                        "self_subject_prefix": "Signed in as ",
                        "self_subject_suffix": " · Your account settings",
                    }
                }
            elif step == 4 and mode != "owner_otp":
                action = {"report_connection": {"subject_selector": "#subject"}}
            model_calls[-1]["action"] = list(action)[0]
            model_calls[-1]["elements"] = [
                {
                    "index": index,
                    "tag": "input" if "<input" in line.lower() else "button",
                }
                for index, line in candidates
            ]
            content = json.dumps({"action": [action]})
            tool = request.get("tools", [{"function": {"name": "AgentOutput"}}])[0][
                "function"
            ]["name"]
            response = json.dumps(
                {
                    "id": "synthetic-only",
                    "object": "chat.completion",
                    "model": "fixture-transport",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "synthetic-call",
                                        "type": "function",
                                        "function": {
                                            "name": tool,
                                            "arguments": content,
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    model_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
    threading.Thread(target=model_server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        native_runner,
        "resolve_model",
        lambda _: {
            "provider": "deepseek",
            "key": "synthetic-transport-only",
            "model": "fixture-transport",
            "base_url": f"http://127.0.0.1:{model_server.server_port}",
        },
    )
    board = NativeBoard(
        BoardState(
            host="93.184.215.14",
            username="member-" + os.urandom(4).hex(),
            password="secret-" + os.urandom(6).hex(),
            wrong_subject=mode == "wrong_subject",
            require_otp=mode == "owner_otp",
        ),
        public_origin="http://93.184.215.14",
    ).start()

    async def fixture_connect(self, ip, port):
        if ip != "93.184.215.14" or port != 80:
            raise ValueError("Fixture transport forbids other destinations")
        return await asyncio.open_connection("127.0.0.1", board.server.server_port)

    monkeypatch.setattr(BrowserForwardProxy, "connect", fixture_connect)
    with socket.socket() as chooser:
        chooser.bind(("127.0.0.1", 0))
        port = chooser.getsockname()[1]
    monkeypatch.setenv(
        "LEAD_BROWSER_TRANSPORTS", json.dumps({"shared": f"http://127.0.0.1:{port}"})
    )
    server = uvicorn.Server(
        uvicorn.Config(browser.app, host="127.0.0.1", port=port, log_level="error")
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    aid = None
    try:
        aid = domain.create_agent(runtime_engine="growth")
        with tx(aid) as c:
            c.execute(
                "UPDATE lead.agents SET desired_state='running' WHERE id=%s", (aid,)
            )
        pending = await begin(
            aid,
            "web",
            site_url=board.origin + "/login",
            login=LoginInput(
                username=board.state.username, password=board.state.password
            ),
        )
        with tx(aid) as c:
            task = one(
                c,
                "SELECT * FROM lead.tasks WHERE type='growth.account' AND input#>>'{spec,payload,takeover_id}'=%s",
                (str(pending["takeover_id"]),),
            )
        ref = TaskRef(
            agent_id=aid,
            task_id=task["id"],
            task_generation=task["input"]["task_generation"],
        )
        advance(ref)
        with tx(aid) as c:
            task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task["id"],))
        assert all(
            secret not in json.dumps(task["input"])
            for secret in (board.state.username, board.state.password)
        )
        result = await execute_account(
            aid, task, AccountInput.model_validate(task["input"]["spec"]["payload"])
        )
        probe_checks = None
        if mode == "login" and result.outcome == "completed":
            actor = await browser.gateway.get(
                aid, task["input"]["spec"]["payload"]["profile_id"]
            )
            probe, target = await actor.native_runtime.acquire_identity_probe(
                [board.origin + "/account", board.origin + "/login"]
            )
            try:
                assert await probe.context.cookies(board.origin) == []
                await probe.goto(
                    board.origin + "/account", wait_until="domcontentloaded"
                )
                assert probe.url == board.origin + "/login"
                before = len(board.oracle()["requests"])
                denied = await probe.evaluate("""async()=>{
                    try {await fetch('/session',{method:'POST',body:'username=probe&password=probe'});return false;}
                    catch {return true;}
                }""")
                assert denied and len(board.oracle()["requests"]) == before
                probe_checks = {
                    "cookie_empty": True,
                    "account_redirected_to_login": True,
                    "post_denied": True,
                }
            finally:
                await actor.native_runtime.release_page(target)
        with tx(aid) as c:
            final = one(
                c,
                "SELECT state,external_account_id FROM lead.social_accounts WHERE id=%s",
                (pending["account_id"],),
            )
            usage = c.execute(
                "SELECT state,units FROM lead.budget_reservations"
            ).fetchall()
            connection = one(
                c,
                "SELECT state,control_mode,challenge_version,encrypted_login FROM lead.takeover_sessions WHERE id=%s",
                (pending["takeover_id"],),
            )
        from lead_app.config import STATE

        result_path = (
            STATE
            / "browser-use-runs"
            / ("connection-" + str(pending["takeover_id"]))
            / "result.json"
        )
        worker = json.loads(result_path.read_text())
        return {
            "privacy": worker["credential_scan"],
            "identity_probe": probe_checks,
            "usage": worker["usage"],
            "agent_id": aid,
            "result": result.model_dump(mode="json"),
            "account": final,
            "billing": usage,
            "connection": {
                key: value
                for key, value in connection.items()
                if key != "encrypted_login"
            },
            "temporary_login_cleared": connection["encrypted_login"] is None,
            "synthetic_model_diagnostics": model_calls,
            "oracle": board.oracle(),
            "checks": {
                "actual_profile": pending["automatic_connection"],
                "real_connection": final["state"] == "connected",
                "synthetic_model_requests": len(model_calls),
                "mechanical_roundtrip": final["state"] == "connected"
                and len(model_calls) <= 10,
            },
        }
    finally:
        if aid:
            await browser.gateway.close(aid)
        server.should_exit = True
        await serving
        board.close()
        model_server.shutdown()
        model_server.server_close()
        if aid:
            domain.set_control(aid, "archive")
