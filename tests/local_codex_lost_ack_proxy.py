#!/usr/bin/env python3
"""Acceptance-only fixed Board proxy. No Board restart, DB, model or live setup.

Serve as root with --arm-file PRIVATE/arm.json --ready-file PRIVATE/ready.json
--listen-port 0. In a separate root invocation, --arm-body-sha256 HASH writes
one exclusive 0600 arm for the original Action.intent.body UTF-8 hash. CSRF and
cookies pass through unchanged and are never written to an event or arm file.
Only POST /desk/postbox, entry_words, codex-beta.test -> 127.0.0.1:34577 can drop
an ACK. Successful 2xx/3xx response must be completely read first. The matching
request is durably claimed before forwarding; concurrent/repeated matches are
409 without another upstream POST, including after process loss or failure.
"""
import argparse
import fcntl
import hashlib
import hmac
import http.client
import json
import os
import re
import signal
import socket
import stat
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOST = "codex-beta.test"
UPSTREAM = ("127.0.0.1", 34577)
POST_PATH = "/desk/postbox"
CONTENT_FIELD = "entry_words"
MAX_BODY = 32768
MAX_RESPONSE = 4*1024*1024
HOP = {"connection", "proxy-connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade",
       "proxy-authorization", "proxy-authenticate"}
# This is a test seam, not a configurable address: every call uses UPSTREAM.
upstream_connection = http.client.HTTPConnection


def private_directory(path):
    info = path.lstat()
    if path.resolve() != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("private owned directory required")


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise ValueError("private owned regular file required")
        if info.st_size > 2048:
            raise ValueError("invalid private record")
        return json.load(stream)


def atomic_private(path, value, *, exclusive=False):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        pending = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o600)
            stream.write((json.dumps(value, sort_keys=True)+"\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            pending.unlink(missing_ok=True)
            raise
    try:
        if exclusive:
            os.link(pending, path)
        else:
            os.replace(pending, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        pending.unlink(missing_ok=True)


class ArmStore:
    def __init__(self, path):
        self.path = Path(path).absolute()
        private_directory(self.path.parent)
        self.mutex = threading.Lock()

    @contextmanager
    def locked(self):
        with self.mutex:
            lock = self.path.with_name(self.path.name+".lock")
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise ValueError("private lock required")
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def read(self):
        value = private_read(self.path)
        if (not isinstance(value, dict) or set(value)-{"version", "method", "path", "field", "body_sha256", "state", "upstream_status"}
                or value.get("version") != 1 or value.get("method") != "POST" or value.get("path") != POST_PATH
                or value.get("field") != CONTENT_FIELD or not re.fullmatch("[a-f0-9]{64}", value.get("body_sha256", ""))
                or value.get("state") not in {"armed", "forwarding", "consumed", "upstream_rejected", "uncertain"}):
            raise ValueError("invalid fixed arm scope")
        return value

    def arm(self, digest):
        if not re.fullmatch("[a-f0-9]{64}", digest):
            raise ValueError("original body SHA256 required")
        with self.locked():
            if os.path.lexists(self.path):
                raise ValueError("arm already exists; inspect original state")
            atomic_private(self.path, {"version": 1, "method": "POST", "path": POST_PATH, "field": CONTENT_FIELD,
                "body_sha256": digest, "state": "armed"}, exclusive=True)

    def claim(self, method, path, digest):
        if method != "POST" or path != POST_PATH or digest is None:
            return "pass"
        with self.locked():
            if not os.path.lexists(self.path):
                return "pass"
            value = self.read()
            if not hmac.compare_digest(value["body_sha256"], digest):
                return "pass"
            if value["state"] != "armed":
                return "blocked"
            atomic_private(self.path, {**value, "state": "forwarding"})
            return "claimed"

    def finish(self, status):
        with self.locked():
            value = self.read()
            if value["state"] != "forwarding":
                raise ValueError("original forwarded request required")
            state = "consumed" if status is not None and 200 <= status < 400 else "upstream_rejected" if status else "uncertain"
            atomic_private(self.path, {**value, "state": state, "upstream_status": status})


def body_digest(raw, content_type):
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("fixed form content type required")
    fields = parse_qs(raw.decode("utf-8", "strict"), keep_blank_values=True, max_num_fields=12, errors="strict")
    if any(len(values) != 1 for values in fields.values()) or CONTENT_FIELD not in fields:
        raise ValueError("one original content field required")
    body = fields[CONTENT_FIELD][0]
    if not body.strip() or len(body) > 6000:
        raise ValueError("invalid original content field")
    return hashlib.sha256(body.encode()).hexdigest()


def safe_log_path(path):
    try:
        selected = urlsplit(path).path
    except ValueError:
        return "/<other>"
    if selected in {"/", "/access", "/access/password", "/access/verify", "/house-rules", "/commons", "/desk", "/desk/account", POST_PATH, "/discussion/replies"}:
        return selected
    if re.fullmatch(r"/bulletin/[0-9]+", selected):
        return selected
    return "/<other>"


def make_server(store, *, listen_port=0, emit=None):
    output_lock = threading.Lock()
    count = 0
    def record(method, path, status, outcome):
        nonlocal count
        with output_lock:
            count += 1
            value = {"count": count, "method": method, "path": safe_log_path(path), "status": status, "outcome": outcome}
            if emit is not None:
                emit(value)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        def log_message(self, *args):
            pass
        def reject(self, status):
            self.send_response_only(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            record(self.command, self.path, status, "rejected")
        def proxy(self):
            claimed = False
            connection = None
            try:
                hosts = self.headers.get_all("Host", [])
                if hosts != [HOST]:
                    return self.reject(421)
                target = urlsplit(self.path)
                if (not self.path.startswith("/") or self.path.startswith("//") or target.scheme or target.netloc
                        or target.fragment or self.command == "POST" and target.path == POST_PATH and target.query):
                    return self.reject(400)
                lengths = self.headers.get_all("Content-Length", [])
                if (self.headers.get("Transfer-Encoding") or self.headers.get("Expect") or len(lengths) > 1
                        or lengths and not re.fullmatch(r"[0-9]+", lengths[0])):
                    return self.reject(400)
                length = int(lengths[0]) if lengths else 0
                if length > MAX_BODY:
                    return self.reject(413)
                self.connection.settimeout(10)
                body = self.rfile.read(length)
                if len(body) != length:
                    return self.reject(400)
                digest = body_digest(body, self.headers.get_content_type()) if self.command == "POST" and self.path == POST_PATH else None
                outcome = store.claim(self.command, self.path, digest)
                if outcome == "blocked":
                    record(self.command, self.path, 409, "blocked")
                    self.send_response_only(409)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                claimed = outcome == "claimed"
                connection = upstream_connection(*UPSTREAM, timeout=10)
                connection.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                excluded = HOP | {"host", "content-length"} | {
                    part.strip().lower() for part in self.headers.get("Connection", "").split(",")}
                connection.putheader("Host", HOST)
                for name, value in self.headers.items():
                    if name.lower() not in excluded:
                        connection.putheader(name, value)
                connection.putheader("Content-Length", str(length))
                connection.putheader("Connection", "close")
                connection.endheaders(body)
                response = connection.getresponse()
                payload = response.read(MAX_RESPONSE+1)
                if len(payload) > MAX_RESPONSE or response.length not in (None, 0):
                    raise ValueError("complete bounded fixture response required")
                if claimed:
                    store.finish(response.status)
                    claimed = False
                    if 200 <= response.status < 400:
                        record(self.command, self.path, response.status, "dropped")
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                self.send_response_only(response.status, response.reason)
                excluded = HOP | {"content-length"} | {
                    part.strip().lower() for part in response.getheader("Connection", "").split(",")}
                for name, value in response.getheaders():
                    if name.lower() not in excluded:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)
                record(self.command, self.path, response.status, "forwarded")
            except (OSError, ValueError, http.client.HTTPException):
                if claimed:
                    try:
                        store.finish(None)
                    except (OSError, ValueError):
                        pass  # Original forwarding marker still forbids another send.
                try:
                    self.reject(502)
                except OSError:
                    pass
            finally:
                if connection is not None:
                    connection.close()
                self.close_connection = True
        do_GET = do_POST = do_HEAD = proxy

    class Server(ThreadingHTTPServer):
        # TCPServer defaults to five queued connects. A legitimate eight-way
        # claim race must reach the handler and receive seven explicit 409s,
        # rather than produce extra transport failures at the listen queue.
        request_queue_size = 64

        def handle_error(self, request, client_address):
            # Never print a request traceback or header/body-derived error text.
            record("UNKNOWN", "/<other>", 500, "handler_error")

    server = Server(("127.0.0.1", listen_port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm-file", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path)
    parser.add_argument("--listen-port", type=int, default=0)
    parser.add_argument("--arm-body-sha256")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root required to bypass the fixed mijiu-only redirect")
    store = ArmStore(args.arm_file)
    if args.arm_body_sha256:
        store.arm(args.arm_body_sha256)
        print(json.dumps({"armed": True, "path": POST_PATH}), flush=True)
        return
    if args.ready_file is None or not 0 <= args.listen_port <= 65535 or args.listen_port == UPSTREAM[1]:
        parser.error("private ready-file and distinct loopback listen port required")
    private_directory(args.ready_file.absolute().parent)
    server = make_server(store, listen_port=args.listen_port,
        emit=lambda value: print(json.dumps(value, sort_keys=True), flush=True))
    try:
        atomic_private(args.ready_file.absolute(), {"pid": os.getpid(), "uid": os.geteuid(), "host": "127.0.0.1", "port": server.server_port,
            "upstream_host": UPSTREAM[0], "upstream_port": UPSTREAM[1], "http_host": HOST}, exclusive=True)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
