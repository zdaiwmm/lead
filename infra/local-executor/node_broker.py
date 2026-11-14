#!/usr/bin/env python3
"""Socket-activated machine credential broker. Never starts a model process.

Only the fixed worker unit's actual MainPID may use this protocol. The machine
credential is loaded by systemd into a different UID's private credential mount.
No request chooses an HTTP origin, header, tenant, unit or executable.
"""
import json
import os
import pwd
import re
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

MAX_FRAME = 16 * 1024 * 1024
SOCKET = "/run/lead-codex-node/broker.sock"
WORKER_UNIT = "lead-codex-worker.service"
WORKER_USER = "lead-codex"
ERRORS = {"executor_node_unavailable", "executor_node_epoch_changed", "executor_scope_forbidden", "executor_epoch_changed",
    "executor_scope_generation_changed", "executor_request_conflict", "executor_result_conflict",
    "executor_payload_invalid", "executor_capacity_unavailable", "executor_not_ready"}


class BrokerError(Exception):
    def __init__(self, code="executor_payload_invalid", status=422, retryable=False):
        self.code, self.status, self.retryable = code, status, retryable
        super().__init__(code)


def require(ok, code="executor_payload_invalid", status=422):
    if not ok:
        raise BrokerError(code, status)


def exact(value, required, optional=()):
    require(type(value) is dict and set(required) <= set(value) <= set(required) | set(optional))


def identifier(value):
    require(type(value) is str)
    try:
        require(str(uuid.UUID(value)) == value)
    except (ValueError, AttributeError):
        raise BrokerError() from None
    return value


def positive(value):
    require(type(value) is int and value > 0)
    return value


def unique(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result)
        result[key] = value
    return result


def decode_frame(raw):
    require(len(raw) <= MAX_FRAME)
    try:
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: require(False))
        require(type(value) is dict)
        return value
    except (ValueError, UnicodeError):
        raise BrokerError() from None


def worker_snapshot():
    """Fixed readonly argv. Query failure never degrades to UID-only trust."""
    try:
        result = subprocess.run(["/usr/bin/systemctl", "show", WORKER_UNIT,
            "--property=MainPID,ActiveState,InvocationID"], capture_output=True,
            text=True, timeout=3, check=True, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            close_fds=True)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        require(values.get("ActiveState") == "active", "broker_worker_changed", 409)
        pid = int(values.get("MainPID", "0"))
        invocation = values.get("InvocationID", "")
        require(pid > 1 and re.fullmatch(r"[a-f0-9]{32}", invocation), "broker_worker_changed", 409)
        return {"pid": pid, "invocation": invocation}
    except Exception:
        raise BrokerError("broker_worker_changed", 409) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NodeHTTP:
    def __init__(self, credential):
        exact(credential, ("base_url", "node_id", "node_token", "node_epoch", "instance_id",
            "credential_generation"))
        parsed = urllib.parse.urlsplit(credential["base_url"])
        require(parsed.scheme == "https" and parsed.hostname and parsed.netloc == parsed.hostname
            and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
        identifier(credential["node_id"])
        identifier(credential["instance_id"])
        positive(credential["node_epoch"])
        positive(credential["credential_generation"])
        token = credential["node_token"]
        require(type(token) is str and 32 <= len(token) <= 4096 and token.isascii()
            and not any(c.isspace() for c in token))
        self.base_url = credential["base_url"].rstrip("/")
        self.token = token
        # Lead transport is direct TLS. It never inherits model proxy routing.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __call__(self, path, body):
        require(path == "/executor/v2/nodes/poll" or re.fullmatch(
            r"/executor/v2/scopes/[a-f0-9-]{36}/(?:poll|events|tools)", path))
        request = urllib.request.Request(self.base_url + path, method="POST",
            data=json.dumps(body, allow_nan=False, separators=(",", ":")).encode(),
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=40) as response:
                value = decode_frame(response.read(MAX_FRAME + 1))
            return value
        except urllib.error.HTTPError as error:
            status = error.code
            code = {401: "executor_node_unavailable", 403: "executor_scope_forbidden",
                409: "executor_epoch_changed", 422: "executor_payload_invalid",
                429: "executor_capacity_unavailable"}.get(status, "executor_not_ready")
            try:
                value = decode_frame(error.read(65537))
                detail = value.get("detail", value)
                candidate = detail.get("code") if type(detail) is dict else detail
                if candidate in ERRORS:
                    code = candidate
            except Exception:
                pass
            finally:
                error.close()
            raise BrokerError(code, status if status in (401, 403, 409, 422, 429) else 503,
                status == 429 or status >= 500) from None
        except Exception:
            raise BrokerError("executor_not_ready", 503, True) from None


def scope_descriptor(value, revision):
    base = {"scope_ref", "scope_kind", "tenant_id", "epoch", "grant_generation"}
    kind = value.get("scope_kind") if type(value) is dict else None
    require(kind in ("project", "studio"))
    exact(value, base | ({"agent_id", "device_id"} if kind == "project" else {"studio_id"}),
        ("version", "transport", "policy_revision"))
    for key in ("scope_ref", "tenant_id", "agent_id", "device_id", "studio_id"):
        if key in value:
            identifier(value[key])
    positive(value["epoch"])
    positive(value["grant_generation"])
    require(value.get("version", 2) == 2 and value.get("transport", "unix-broker") == "unix-broker")
    revision = value.get("policy_revision", revision)
    require(type(revision) is str and 0 < len(revision) <= 200)
    return {**value, "version": 2, "transport": "unix-broker", "policy_revision": revision}


class Broker:
    def __init__(self, *, worker_uid, snapshot, post, node_epoch, instance_id):
        self.worker_uid, self.snapshot, self.post = worker_uid, snapshot, post
        self.node_epoch, self.instance_id = positive(node_epoch), identifier(instance_id)
        self.generation = None
        self.capabilities = {}
        self.scopes = {}
        self.revoked = False
        self.lock = threading.Lock()

    def authenticate(self, peer):
        observed = self.snapshot()
        require(peer[1] == self.worker_uid and peer[0] == observed["pid"], "broker_worker_changed", 409)
        return str(observed["pid"]) + ":" + observed["invocation"]

    def connection(self, peer):
        return Connection(self, peer, self.authenticate(peer))

    def dispatch(self, peer, request, *, connection_generation=None):
        request_id = None
        try:
            exact(request, ("v", "id", "method", "params"))
            require(type(request["v"]) is int and request["v"] == 1)
            request_id = identifier(request["id"])
            params = request["params"]
            require(type(params) is dict)
            generation = self.authenticate(peer)
            require(connection_generation is None or connection_generation == generation,
                "broker_worker_changed", 409)
            require(not self.revoked, "executor_node_unavailable", 401)
            method = request["method"]
            if method == "worker.hello":
                exact(params, ("instance_id", "capabilities"))
                identifier(params["instance_id"])
                require(type(params["capabilities"]) is dict)
                with self.lock:
                    if generation != self.generation:
                        self.scopes = {}
                    self.generation = generation
                    self.capabilities = params["capabilities"]
                result = {"worker_generation": generation}
            else:
                require(params.get("worker_generation") == generation == self.generation,
                    "broker_worker_changed", 409)
                if method == "scope.list":
                    exact(params, ("worker_generation",), ("capabilities",))
                    with self.lock:
                        if "capabilities" in params:
                            require(type(params["capabilities"]) is dict)
                            self.capabilities = params["capabilities"]
                        value = self.post("/executor/v2/nodes/poll", {"node_epoch": self.node_epoch,
                            "instance_id": self.instance_id, "capabilities": self.capabilities})
                        require(value.get("node_epoch") == self.node_epoch, "executor_epoch_changed", 409)
                        require(type(value.get("scopes")) is list)
                        scopes = [scope_descriptor(s, value.get("policy_revision")) for s in value["scopes"]]
                        require(len({s["scope_ref"] for s in scopes}) == len(scopes))
                        self.scopes = {s["scope_ref"]: s for s in scopes}
                    result = {"scopes": scopes, "policy_revision": value.get("policy_revision")}
                else:
                    result = self.scope_call(method, params)
                # Fence a worker replacement that happened during HTTP work.
                require(self.authenticate(peer) == generation, "broker_worker_changed", 409)
            return {"v": 1, "id": request_id, "result": result}
        except BrokerError as error:
            if error.code in ("executor_node_unavailable", "executor_node_epoch_changed"):
                self.revoked = True
            return {"v": 1, "id": request_id, "error": {"code": error.code,
                "status": error.status, "retryable": error.retryable}}
        except Exception:
            return {"v": 1, "id": request_id, "error": {"code": "executor_not_ready",
                "status": 503, "retryable": True}}

    def scope_call(self, method, params):
        extras = {"scope.poll": {"instance_id"}, "scope.events": {"events"},
            "request.tools": {"request_id", "name", "arguments"}}
        require(method in extras)
        exact(params, {"worker_generation", "scope_ref", "grant_generation", "epoch"} | extras[method])
        ref = identifier(params["scope_ref"])
        with self.lock:
            scope = self.scopes.get(ref)
        require(scope is not None, "executor_scope_forbidden", 403)
        require(positive(params["grant_generation"]) == scope["grant_generation"],
            "executor_scope_generation_changed", 409)
        require(positive(params["epoch"]) == scope["epoch"], "executor_epoch_changed", 409)
        if method == "scope.poll":
            identifier(params["instance_id"])
        elif method == "scope.events":
            require(type(params["events"]) is list and len(params["events"]) <= 1000)
        else:
            identifier(params["request_id"])
            require(type(params["name"]) is str and type(params["arguments"]) is dict)
        body = {key: params[key] for key in extras[method]}
        body.update(node_epoch=self.node_epoch, grant_generation=scope["grant_generation"], epoch=scope["epoch"])
        suffix = {"scope.poll": "poll", "scope.events": "events", "request.tools": "tools"}[method]
        return self.post("/executor/v2/scopes/" + ref + "/" + suffix, body)


class Connection:
    def __init__(self, broker, peer, generation):
        self.broker, self.peer, self.generation = broker, peer, generation

    def dispatch(self, request):
        return self.broker.dispatch(self.peer, request, connection_generation=self.generation)


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.connection.settimeout(90)
            pid, uid, gid = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET,
                socket.SO_PEERCRED, struct.calcsize("3i")))
            # Authenticate before reading an untrusted frame.
            connection = self.server.broker.connection((pid, uid, gid))
            while True:
                raw = self.rfile.readline(MAX_FRAME + 2)
                if not raw:
                    return
                require(raw.endswith(b"\n") and len(raw) <= MAX_FRAME + 1)
                request = decode_frame(raw[:-1])
                response = connection.dispatch(request)
                encoded = json.dumps(response, allow_nan=False, separators=(",", ":")).encode()
                require(len(encoded) <= MAX_FRAME)
                self.wfile.write(encoded + b"\n")
        except Exception:
            # No payload, header, credential or URL is ever written to logs.
            return


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 64


def load_credential():
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    require(directory == "/run/credentials/lead-codex-node.service")
    path = Path(directory) / "node.json"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
            and info.st_uid in (0, os.geteuid())
            and stat.S_IMODE(info.st_mode) in (0o400, 0o440)
            and (not info.st_mode & 0o040 or info.st_gid in (0, os.getegid())))
        return decode_frame(source.read(65537))


def main():
    try:
        require(len(sys.argv) == 1 and os.geteuid() != 0)
        credential = load_credential()
        post = NodeHTTP(credential)
        require(os.environ.get("LISTEN_PID") == str(os.getpid()) and os.environ.get("LISTEN_FDS") == "1")
        listener = socket.socket(fileno=3)
        listener.set_inheritable(False)
        require(listener.family == socket.AF_UNIX and listener.type == socket.SOCK_STREAM
            and listener.getsockname() == SOCKET and listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN))
        server = Server(SOCKET, Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = listener
        server.broker = Broker(worker_uid=pwd.getpwnam(WORKER_USER).pw_uid, snapshot=worker_snapshot,
            post=post, node_epoch=credential["node_epoch"], instance_id=credential["instance_id"])
        server.serve_forever(poll_interval=0.5)
    except Exception:
        print("shared executor broker unavailable", file=sys.stderr)
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
