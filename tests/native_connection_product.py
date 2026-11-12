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
from lead_app.growth.runtime import advance, finish
from lead_app.growth.tools import AccountInput
from native_browser_fixture import BoardState, NativeBoard


async def verify_login(monkeypatch, *, mode="login", owner_hook=None):
    from lead_app import native_runner

    model_calls = []
    workflow = {"kind": "login", "step": 0}

    class FakeModel(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            model_calls.append(
                {
                    "bytes": len(raw),
                    "synthetic_otp_literal_seen": b"135790" in raw,
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
            step = workflow["step"]
            workflow["step"] += 1
            action = {
                "done": {
                    "text": "Controlled transport diagnostic only",
                    "success": True,
                }
            }
            if workflow["kind"] == "resume":
                if step == 0:
                    action = {
                        "inspect_self": {
                            "self_link_selector": "a[rel~=me]",
                            "acting_subject_selector": "main > p",
                            "self_subject_prefix": "Signed in as ",
                            "self_subject_suffix": " · Your account settings",
                        }
                    }
                elif step == 1:
                    action = {"report_connection": {"subject_selector": "#subject"}}
            elif workflow["kind"] == "discovery":
                if step == 0:
                    action = {"guarded_navigate": {"url": board.origin + "/home"}}
                elif step == 1:
                    action = {
                        "propose_method": {
                            "target_url": board.origin + "/home",
                            "operation": "publish",
                            "selectors": {
                                "self_subject": "main > p",
                                "editor_target": "#editor-target",
                                "editor_body": "textarea[name=body]",
                                "submit": "form button",
                            },
                            "self_subject_prefix": "Signed in as ",
                            "self_subject_suffix": " · Your account settings",
                            "permission_selector": "section p",
                            "permission_quote": "You are an ordinary member. Members may publish text posts and reply to existing posts. "
                            "Automated browser tools are permitted for these member actions. You have no administrator privileges.",
                        }
                    }
            elif step in (0, 1):
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
            elif step == 3 and (mode != "owner_otp" or workflow.get("allow_identity")):
                action = {
                    "inspect_self": {
                        "self_link_selector": "a[rel~=me]",
                        "acting_subject_selector": "main > p",
                        "self_subject_prefix": "Signed in as ",
                        "self_subject_suffix": " · Your account settings",
                    }
                }
            elif step == 4 and (mode != "owner_otp" or workflow.get("allow_identity")):
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
    boards = {"93.184.215.14": board}

    async def fixture_connect(self, ip, port):
        if ip not in boards or port != 80:
            raise ValueError("Fixture transport forbids other destinations")
        return await asyncio.open_connection("127.0.0.1", boards[ip].server.server_port)

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
        claim = advance(ref)["claim"]
        with tx(aid) as c:
            task = one(c, "SELECT * FROM lead.tasks WHERE id=%s", (task["id"],))
        assert all(
            secret not in json.dumps(task["input"])
            for secret in (board.state.username, board.state.password)
        )
        result = await execute_account(
            aid, task, AccountInput.model_validate(task["input"]["spec"]["payload"])
        )
        finish(claim, result)
        owner_check = (
            await owner_hook(
                {
                    "aid": aid,
                    "task": task,
                    "pending": pending,
                    "claim": claim,
                    "result": result,
                    "board": board,
                    "boards": boards,
                    "workflow": workflow,
                    "model_calls": model_calls,
                    "gateway_endpoint": f"http://127.0.0.1:{port}",
                }
            )
            if owner_hook
            else None
        )
        probe_checks = None
        discovery = None
        if mode == "login" and result.outcome == "completed":
            actor = await browser.gateway.get(
                aid, task["input"]["spec"]["payload"]["profile_id"]
            )
            await actor.connection_pages[str(pending["takeover_id"])].reconcile()
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
            from datetime import timedelta

            from lead_app.account_connection import discover
            from lead_app.growth.contracts import TaskSpec, utcnow
            from lead_app.growth.store import submit_work
            from lead_app.native_task import NativeTask

            first_board = board
            board = NativeBoard(
                BoardState(
                    host="93.184.215.15",
                    username="member-" + os.urandom(4).hex(),
                    password="secret-" + os.urandom(6).hex(),
                ),
                public_origin="http://93.184.215.15",
            ).start()
            boards["93.184.215.15"] = board
            workflow.update(kind="login", step=0)
            second = await begin(
                aid,
                "web",
                site_url=board.origin + "/login",
                login=LoginInput(
                    username=board.state.username, password=board.state.password
                ),
            )
            with tx(aid) as c:
                second_task = one(
                    c,
                    "SELECT * FROM lead.tasks WHERE input#>>'{spec,payload,takeover_id}'=%s",
                    (str(second["takeover_id"]),),
                )
            second_claim = advance(
                TaskRef(
                    agent_id=aid,
                    task_id=second_task["id"],
                    task_generation=second_task["input"]["task_generation"],
                )
            )["claim"]
            with tx(aid) as c:
                second_task = one(
                    c, "SELECT * FROM lead.tasks WHERE id=%s", (second_task["id"],)
                )
            second_result = await execute_account(
                aid,
                second_task,
                AccountInput.model_validate(second_task["input"]["spec"]["payload"]),
            )
            assert second_result.outcome == "completed"
            finish(second_claim, second_result)
            assert str(second_task["input"]["spec"]["payload"]["profile_id"]) == str(
                actor.profile["id"]
            )
            await actor.connection_pages[str(second["takeover_id"])].reconcile()
            board = first_board
            controllers = []
            works = []
            work_claims = []
            try:
                for number in range(2):
                    subject = pending if number == 0 else second
                    site = board if number == 0 else boards["93.184.215.15"]
                    with tx(aid) as c:
                        work = submit_work(
                            c,
                            aid,
                            TaskSpec(
                                kind="account",
                                purpose="discover",
                                account_id=subject["account_id"],
                                payload={"target_urls": [site.origin + "/home"]},
                                review_at=utcnow() + timedelta(hours=1),
                                dedupe_key="native-two-workers-" + str(number),
                            ),
                        )
                    admitted = advance(work)
                    assert admitted["state"] == "ready"
                    work_claims.append(admitted["claim"])
                    works.append(work)
                    controller = NativeTask(actor, work["task_id"])
                    await controller.start()
                    controllers.append(controller)
                await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            controller.verify_private_self()
                            for controller in controllers
                        )
                    ),
                    8,
                )
                # The editor still displays A while the private session becomes B.
                saved = dict(board.state.sessions)
                board.state.sessions = {
                    key: "different-cookie-subject" for key in saved
                }
                try:
                    import pytest

                    with pytest.raises(ValueError, match="native_self_subject_changed"):
                        await controllers[0].verify_private_self()
                finally:
                    board.state.sessions = saved
            finally:
                for controller in controllers:
                    await controller.close()
            workflow.update(kind="discovery", step=0)
            with tx(aid) as c:
                discovery_task = one(
                    c, "SELECT * FROM lead.tasks WHERE id=%s", (works[0]["task_id"],)
                )
            discovery = await discover(aid, discovery_task)
            finish(work_claims[0], discovery)
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

        with tx(aid) as c:
            task_state = one(
                c, "SELECT state FROM lead.tasks WHERE id=%s", (task["id"],)
            )["state"]
            learned = one(
                c,
                "SELECT verified_permissions FROM lead.account_targets WHERE account_id=%s AND target_type='web'",
                (pending["account_id"],),
            )
            if learned:
                assert (
                    learned["verified_permissions"]["browser_methods"]["publish"][
                        "receipt_state"
                    ]
                    == "unobserved"
                )
                assert not learned["verified_permissions"].get(
                    "browser_verified_operations"
                )

        result_path = (
            STATE
            / "browser-use-runs"
            / ("connection-" + str(pending["takeover_id"]))
            / "result.json"
        )
        worker = json.loads(result_path.read_text())
        return {
            "privacy": worker["credential_scan"],
            "owner_check": owner_check,
            "identity_probe": probe_checks,
            "task_state": task_state,
            "discovery": discovery.model_dump(mode="json") if discovery else None,
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
                and worker["usage"]["request_count"] <= 8,
            },
        }
    finally:
        if aid:
            await browser.gateway.close(aid)
        server.should_exit = True
        await serving
        for site in boards.values():
            site.close()
        model_server.shutdown()
        model_server.server_close()
        if aid:
            domain.set_control(aid, "archive")
