"""Loopback-only HTTP proxy mechanics; fake upstream, no DB/model/live Board."""
import concurrent.futures
import hashlib
import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode

import local_codex_lost_ack_proxy as proxy
import pytest


@pytest.fixture
def rig(tmp_path, monkeypatch):
    seen, events = [], []
    gate = threading.Event()
    gate.set()
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.do_POST()
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append((self.command, self.path, self.headers.get("Host"), self.headers.get("Cookie"), body))
            gate.wait(3)
            if getattr(gate, "fail_transport", False):
                self.close_connection = True
                return
            self.send_response(getattr(gate, "response_status", 303))
            self.send_header("Set-Cookie", "session=retained; HttpOnly")
            self.send_header("Set-Cookie", "other=retained; HttpOnly")
            self.send_header("Location", "http://codex-beta.test/bulletin/7002")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"OK")
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    original = http.client.HTTPConnection
    def connect(host, port, **kwargs):
        assert (host, port) == ("127.0.0.1", 34577)
        return original("127.0.0.1", upstream.server_port, **kwargs)
    monkeypatch.setattr(proxy, "upstream_connection", connect)
    folder = tmp_path/"private"
    folder.mkdir(mode=0o700)
    store = proxy.ArmStore(folder/"arm.json")
    server = proxy.make_server(store, emit=events.append)
    threads = [threading.Thread(target=item.serve_forever, daemon=True) for item in (upstream, server)]
    for thread in threads:
        thread.start()
    def request(path="/desk/postbox", body="Authorized original body", *, method="POST", host="codex-beta.test", csrf="csrf-secret"):
        client = original("127.0.0.1", server.server_port, timeout=4)
        payload = urlencode({"entry_words": body, "posting_key": csrf}).encode() if method == "POST" else None
        try:
            client.request(method, path, body=payload, headers={"Host": host, "Cookie": "session=cookie-secret",
                "Content-Type": "application/x-www-form-urlencoded"})
            response = client.getresponse()
            return response.status, response.getheaders(), response.read()
        finally:
            client.close()
    try:
        yield store, request, seen, events, gate, folder
    finally:
        gate.set()
        for item in (server, upstream):
            item.shutdown()
            item.server_close()
        for thread in threads:
            thread.join(timeout=2)


def arm(store, body="Authorized original body"):
    store.arm(hashlib.sha256(body.encode()).hexdigest())


def test_cookie_headers_and_body_are_transparent_without_arm(rig):
    store, request, seen, events, _, _ = rig
    status, headers, body = request()
    assert status == 303 and body == b"OK"
    assert [value for key, value in headers if key.lower() == "set-cookie"] == [
        "session=retained; HttpOnly", "other=retained; HttpOnly"]
    assert seen[0][2:4] == ("codex-beta.test", "session=cookie-secret")
    assert b"posting_key=csrf-secret" in seen[0][4]
    encoded = json.dumps(events)
    assert not any(secret in encoded for secret in ("cookie-secret", "csrf-secret", "Authorized", "session="))
    assert not store.path.exists()


def test_matching_business_body_drops_only_after_upstream_success_and_blocks_repeat(rig):
    store, request, seen, events, _, _ = rig
    arm(store)
    with pytest.raises((http.client.RemoteDisconnected, ConnectionResetError)):
        request(csrf="new-csrf-secret")
    assert len(seen) == 1
    assert json.loads(store.path.read_text())["state"] == "consumed"
    assert request(csrf="another-csrf")[0] == 409
    assert len(seen) == 1 and sum(event["outcome"] == "dropped" for event in events) == 1
    with pytest.raises(ValueError, match="already"):
        arm(store)


def test_unmatched_method_path_or_body_is_not_consumed(rig):
    store, request, seen, _, _, _ = rig
    arm(store)
    assert request(body="Another body")[0] == 303
    assert request(path="/access/password")[0] == 303
    assert request(method="GET")[0] == 303
    assert request(path="/desk/postbox?query=secret")[0] == 400
    assert json.loads(store.path.read_text())["state"] == "armed"
    assert len(seen) == 3


def test_concurrent_exact_requests_cannot_forward_or_drop_twice(rig):
    store, request, seen, events, gate, _ = rig
    arm(store)
    gate.clear()
    def attempt():
        try:
            return request()[0]
        except (http.client.RemoteDisconnected, ConnectionResetError):
            return "dropped"
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(attempt) for _ in range(8)]
        # The first upstream stays in flight while competing requests claim.
        threading.Event().wait(.08)
        gate.set()
        results = [future.result(timeout=5) for future in futures]
    observed = {"client_results": results, "upstream_posts": len(seen),
        "proxy_dropped": sum(event["outcome"] == "dropped" for event in events),
        "proxy_blocked": sum(event["outcome"] == "blocked" for event in events),
        "proxy_errors": sum(event["outcome"] == "handler_error" for event in events)}
    assert observed["upstream_posts"] == observed["proxy_dropped"] == 1, observed
    assert observed["proxy_blocked"] == 7, observed
    assert results.count("dropped") == 1 and results.count(409) == 7, observed


def test_store_restart_preserves_inflight_and_consumed_guard(rig):
    store, _, _, _, _, _ = rig
    arm(store)
    digest = hashlib.sha256(b"Authorized original body").hexdigest()
    assert store.claim("POST", "/desk/postbox", digest) == "claimed"
    restarted = proxy.ArmStore(store.path)
    assert restarted.claim("POST", "/desk/postbox", digest) == "blocked"
    restarted.finish(303)
    assert proxy.ArmStore(store.path).claim("POST", "/desk/postbox", digest) == "blocked"


def test_host_absolute_target_and_framing_cannot_become_an_open_proxy(rig):
    _, request, seen, events, _, _ = rig
    assert request(host="example.com")[0] == 421
    assert request(path="http://example.com/secret?token=secret")[0] == 400
    assert seen == []
    assert "token=secret" not in json.dumps(events)


def test_private_arm_file_and_scope_are_mandatory(rig):
    store, _, _, _, _, folder = rig
    arm(store)
    record = json.loads(store.path.read_text())
    assert record["path"] == "/desk/postbox" and record["field"] == "entry_words"
    assert store.path.stat().st_mode & 0o777 == 0o600
    store.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        store.claim("POST", "/desk/postbox", record["body_sha256"])
    public = folder.parent/"public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ValueError):
        proxy.ArmStore(public/"not-private.json")


def _claim_in_process(path):
    return proxy.ArmStore(path).claim("POST", "/desk/postbox", hashlib.sha256(b"Authorized original body").hexdigest())


def test_atomic_file_claim_is_shared_across_processes(rig):
    store, _, _, _, _, _ = rig
    arm(store)
    with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_claim_in_process, [str(store.path)]*8))
    assert results.count("claimed") == 1 and results.count("blocked") == 7


@pytest.mark.parametrize("transport_failure", [False, True])
def test_unproven_or_rejected_upstream_never_drops_and_never_reposts(rig, transport_failure):
    store, request, seen, events, gate, _ = rig
    arm(store)
    gate.fail_transport = transport_failure
    gate.response_status = 403
    assert request()[0] == (502 if transport_failure else 403)
    assert json.loads(store.path.read_text())["state"] == ("uncertain" if transport_failure else "upstream_rejected")
    assert request()[0] == 409 and len(seen) == 1
    assert not any(event["outcome"] == "dropped" for event in events)


@pytest.mark.parametrize("raw,content_type", [
    (b"entry_words=one&entry_words=two", "application/x-www-form-urlencoded"),
    (b"entry_words=one", "application/json"),
    (b"entry_words=%FF", "application/x-www-form-urlencoded"),
    (b"posting_key=secret", "application/x-www-form-urlencoded"),
])
def test_content_digest_rejects_unprovable_form_fields(raw, content_type):
    with pytest.raises((ValueError, UnicodeError)):
        proxy.body_digest(raw, content_type)
