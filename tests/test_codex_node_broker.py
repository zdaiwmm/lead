"""Synthetic broker trust boundaries; never opens production credentials."""
import importlib.util
import io
import json
import os
import socket
import stat
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "infra/local-executor/node_broker.py"
spec = importlib.util.spec_from_file_location("codex_node_broker", SCRIPT)
broker = importlib.util.module_from_spec(spec)
if SCRIPT.exists():
    spec.loader.exec_module(broker)


def uid():
    return str(uuid4())


def frame(method, **params):
    return {"v": 1, "id": uid(), "method": method, "params": params}


@pytest.fixture
def setup():
    assert callable(getattr(broker, "Broker", None)), "shared node broker is missing"
    generation = {"pid": 123, "invocation": "a" * 32}
    scope = {"scope_ref": uid(), "scope_kind": "project", "tenant_id": uid(),
        "agent_id": uid(), "device_id": uid(), "epoch": 1, "grant_generation": 2}
    calls = []

    def post(path, body):
        calls.append((path, body))
        if path == "/executor/v2/nodes/poll":
            return {"node_epoch": 3, "policy_revision": "execution/default@4", "scopes": [scope]}
        return {"epoch": body["epoch"], "commands": []}

    service = broker.Broker(worker_uid=974, snapshot=lambda: dict(generation), post=post,
        node_epoch=3, instance_id=uid())
    peer = (123, 974, 968)
    hello = service.dispatch(peer, frame("worker.hello", instance_id=uid(), capabilities={"ready": True}))
    token = hello["result"]["worker_generation"]
    return service, peer, token, scope, generation, calls


def test_other_uid_or_same_uid_child_cannot_obtain_directory(setup):
    service, _, token, _, _, calls = setup
    for peer in [(123, 975, 968), (456, 974, 968)]:
        result = service.dispatch(peer, frame("scope.list", worker_generation=token))
        assert result["error"]["code"] == "broker_worker_changed"
    assert calls == []


def test_worker_restart_invalidates_existing_generation(setup):
    service, peer, token, _, generation, calls = setup
    generation["invocation"] = "b" * 32
    result = service.dispatch(peer, frame("scope.list", worker_generation=token))
    assert result["error"]["code"] == "broker_worker_changed"
    assert calls == []


def test_old_connection_cannot_hello_into_a_new_invocation(setup):
    service, peer, token, _, generation, calls = setup
    assert callable(getattr(service, "connection", None)), "connection generation fencing is missing"
    connection = service.connection(peer)
    generation["invocation"] = "c" * 32
    result = connection.dispatch(frame("worker.hello", instance_id=uid(), capabilities={}))
    assert result["error"]["code"] == "broker_worker_changed"
    assert service.generation == token
    assert calls == []


def test_scope_directory_and_fenced_poll_use_only_authorized_server_scope(setup):
    service, peer, token, scope, _, calls = setup
    result = service.dispatch(peer, frame("scope.list", worker_generation=token))
    assert result["result"]["scopes"][0] == {**scope, "policy_revision": "execution/default@4",
        "version": 2, "transport": "unix-broker"}
    params = {"worker_generation": token, "scope_ref": scope["scope_ref"],
        "grant_generation": 2, "epoch": 1, "instance_id": uid()}
    assert service.dispatch(peer, frame("scope.poll", **params))["result"] == {"epoch": 1, "commands": []}
    assert calls[-1][0] == "/executor/v2/scopes/" + scope["scope_ref"] + "/poll"
    assert calls[-1][1] == {"node_epoch": 3, "grant_generation": 2, "epoch": 1,
        "instance_id": params["instance_id"]}


@pytest.mark.parametrize("extra", [{"url": "https://evil.invalid"}, {"tenant_id": "other"},
    {"headers": {"Authorization": "x"}}, {"node_token": "secret"}])
def test_caller_cannot_supply_routing_or_credentials(setup, extra):
    service, peer, token, scope, _, calls = setup
    service.dispatch(peer, frame("scope.list", worker_generation=token))
    before = len(calls)
    result = service.dispatch(peer, frame("scope.poll", worker_generation=token,
        scope_ref=scope["scope_ref"], grant_generation=2, epoch=1, instance_id=uid(), **extra))
    assert result["error"]["code"] == "executor_payload_invalid"
    assert len(calls) == before


def test_unknown_or_stale_scope_is_never_forwarded(setup):
    service, peer, token, scope, _, calls = setup
    service.dispatch(peer, frame("scope.list", worker_generation=token))
    before = len(calls)
    for ref, grant in [(uid(), 2), (scope["scope_ref"], 1)]:
        result = service.dispatch(peer, frame("scope.poll", worker_generation=token,
            scope_ref=ref, grant_generation=grant, epoch=1, instance_id=uid()))
        assert result["error"]["code"] in {"executor_scope_forbidden", "executor_scope_generation_changed"}
    assert len(calls) == before


def test_transport_exception_does_not_disclose_private_input(setup):
    service, peer, token, _, _, _ = setup

    def fail(path, body):
        raise RuntimeError("Bearer SYNTHETIC_NODE_SECRET https://private.invalid")

    service.post = fail
    result = service.dispatch(peer, frame("scope.list", worker_generation=token))
    assert result["error"] == {"code": "executor_not_ready", "status": 503, "retryable": True}
    assert "SECRET" not in json.dumps(result)


def test_node_epoch_failure_keeps_its_global_error_code(monkeypatch):
    client = broker.NodeHTTP({"base_url": "https://example.invalid", "node_id": uid(),
        "node_token": "SYNTHETIC_NODE_CREDENTIAL_0123456789", "node_epoch": 1,
        "instance_id": uid(), "credential_generation": 1})

    def fail(request, timeout):
        raise urllib.error.HTTPError("https://example.invalid", 409, "conflict", {},
            io.BytesIO(b'{"detail":"executor_node_epoch_changed"}'))

    monkeypatch.setattr(client.opener, "open", fail)
    with pytest.raises(broker.BrokerError) as raised:
        client("/executor/v2/nodes/poll", {})
    assert raised.value.code == "executor_node_epoch_changed"


@pytest.mark.parametrize("mode,owner,group,allowed", [
    (0o440, 0, 0, True), (0o400, 58942, 58942, True),
    (0o440, 58942, 58942, True), (0o444, 0, 0, False),
    (0o460, 0, 0, False), (0o440, 0, 974, False), (0o400, 974, 0, False),
])
def test_systemd_credential_read_mask_allows_only_root_or_service_group(monkeypatch, tmp_path, mode, owner, group, allowed):
    path = tmp_path / "fake-credential.json"
    path.write_text('{"synthetic":true}')
    with path.open("rb") as fixture:
        with monkeypatch.context() as patch:
            patch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/lead-codex-node.service")
            patch.setattr(broker.os, "open", lambda *_: os.dup(fixture.fileno()))
            patch.setattr(broker.os, "fstat", lambda _: SimpleNamespace(st_mode=stat.S_IFREG | mode,
                st_uid=owner, st_gid=group, st_nlink=1))
            patch.setattr(broker.os, "geteuid", lambda: 58942)
            patch.setattr(broker.os, "getegid", lambda: 58942)
            if allowed:
                assert broker.load_credential() == {"synthetic": True}
            else:
                with pytest.raises(broker.BrokerError):
                    broker.load_credential()


def test_duplicate_keys_and_oversized_frames_are_rejected():
    assert callable(getattr(broker, "decode_frame", None)), "broker frame decoder is missing"
    for raw in (b'{"v":1,"v":1}', b'{"x":NaN}', b"x" * (16 * 1024 * 1024 + 1)):
        with pytest.raises(broker.BrokerError):
            broker.decode_frame(raw)


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="requires Linux kernel peer credentials")
def test_real_unix_peer_main_process_allowed_same_uid_child_rejected(tmp_path):
    def snapshot():
        return {"pid": os.getpid(), "invocation": "d" * 32}
    service = broker.Broker(worker_uid=os.getuid(), snapshot=snapshot,
        post=lambda *_: {}, node_epoch=1, instance_id=uid())
    path = str(tmp_path / "broker.sock")
    server = broker.Server(path, broker.Handler)
    server.broker = service
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(3)
            client.connect(path)
            client.sendall(json.dumps(frame("worker.hello", instance_id=uid(), capabilities={})).encode() + b"\n")
            assert "worker_generation" in json.loads(client.recv(4096))["result"]
            # A model child has exactly the same Unix UID. Kernel PID differs.
            code = """import socket,sys
s=socket.socket(socket.AF_UNIX);s.settimeout(3);s.connect(sys.argv[1])
try:
 s.sendall(b'{"v":1}\\n');print(repr(s.recv(4096)))
except (ConnectionResetError,BrokenPipeError):print("b''")
"""
            child = subprocess.run([sys.executable, "-I", "-c", code, path],
                capture_output=True, text=True, timeout=5, close_fds=True, check=True)
            assert child.stdout.strip() == "b''"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
