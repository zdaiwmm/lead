"""Public reader protocol security boundaries; no model, database or public network."""

import asyncio
import importlib
import json
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def pr():
    # Removing the implementation must fail these transport regressions clearly.
    assert importlib.util.find_spec("lead_app.public_reader") is not None, (
        "the controlled public-reader transport is missing"
    )
    return importlib.import_module("lead_app.public_reader")


def encode(value):
    return json.dumps(value, ensure_ascii=True).encode() + b"\n"


OBSERVED = {
    "url": "https://example.com/product",
    "title": "Public product",
    "text": "Observed public page",
    "requires_javascript": False,
    "links": ["https://example.com/features"],
}


@pytest.fixture
def socket_path(monkeypatch, pr):
    directory = tempfile.TemporaryDirectory(prefix="lead-reader-", dir="/private/tmp")
    path = str(Path(directory.name) / "reader.sock")
    monkeypatch.setattr(pr, "PUBLIC_READER_SOCKET", path)
    # A normal developer cannot chown temp directories to root. Preserve real
    # socket mode/ownership while modeling root-owned parents for client tests.
    real_lstat = pr.os.lstat

    def lstat(candidate, *args, **kwargs):
        info = real_lstat(candidate, *args, **kwargs)
        if stat.S_ISDIR(info.st_mode):
            values = list(info)
            values[4] = 0
            values[0] &= ~0o022
            return os.stat_result(values)
        return info

    monkeypatch.setattr(pr.os, "lstat", lstat)
    yield path
    monkeypatch.setattr(pr.os, "lstat", real_lstat)
    directory.cleanup()


async def exchange(path, response, callback):
    seen = []

    async def serve(reader, writer):
        seen.append(await reader.readline())
        writer.write(response)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=path)
    os.chmod(path, 0o600)
    try:
        result = await callback()
        return result, seen
    finally:
        server.close()
        await server.wait_closed()
        Path(path).unlink(missing_ok=True)


async def test_read_transmits_only_fixed_read_fields(socket_path, pr):
    result, seen = await exchange(
        socket_path,
        encode({"v": 1, "ok": True, "observed": OBSERVED}),
        lambda: pr.read_via_reader(socket_path, "https://example.com/product", discover_links=True),
    )
    assert result == OBSERVED
    assert json.loads(seen[0]) == {
        "v": 1, "action": "read", "url": "https://example.com/product",
        "discover_links": True, "external_links": False,
    }


async def test_health_checks_full_revision_without_fetching(socket_path, pr):
    payload = {"v": 1, "ok": True, "protocol": 1, "revision": "a" * 40}
    result, seen = await exchange(socket_path, encode(payload), lambda: pr.reader_health(socket_path))
    assert result == {"protocol": 1, "revision": "a" * 40}
    assert json.loads(seen[0]) == {"v": 1, "action": "health"}


@pytest.mark.parametrize("payload", [
    {"v": 2, "ok": True, "observed": OBSERVED},
    {"v": True, "ok": True, "observed": OBSERVED},
    {"v": 1, "ok": 1, "observed": OBSERVED},
    {"v": 1, "ok": True, "observed": {**OBSERVED, "cookie": "secret"}},
    {"v": 1, "ok": True, "observed": {**OBSERVED, "url": "http://127.0.0.1"}},
    {"v": 1, "ok": True, "observed": {**OBSERVED, "text": "x" * 18001}},
    {"v": 1, "ok": True, "observed": {**OBSERVED, "title": "\ud800"}},
    {"v": 1, "ok": True, "observed": {**OBSERVED, "links": ["http://localhost/"]}},
    {"v": 1, "ok": False, "error": {"code": "arbitrary secret", "category": "transport"}},
    {"v": 1, "ok": False, "error": {"code": {}, "category": "transport"}},
    {"v": 1, "ok": False, "error": {"code": "source_timeout", "category": "policy"}},
    {"v": 1, "ok": False, "error": {"code": "source_http_error", "category": "http", "status": True}},
])
async def test_client_rejects_bad_response_schema(socket_path, pr, payload):
    with pytest.raises(pr.PublicReaderUnavailable) as exc:
        await exchange(socket_path, encode(payload), lambda: pr.read_via_reader(socket_path, "https://example.com"))
    assert exc.value.code == "reader_invalid_response"
    assert "secret" not in str(exc.value)


@pytest.mark.parametrize("response", [b"{}", b"not-json\n", b"{}\n{}\n", b"x" * (4 * 1024 * 1024 + 1)])
async def test_client_rejects_bad_frames(socket_path, pr, response):
    with pytest.raises(pr.PublicReaderUnavailable) as exc:
        await exchange(socket_path, response, lambda: pr.read_via_reader(socket_path, "https://example.com"))
    assert exc.value.code == "reader_invalid_response"


@pytest.mark.parametrize("code", ["source_dns_unavailable", "source_transport_unavailable", "source_timeout", "reader_busy", "reader_unavailable"])
async def test_transport_error_is_finite_and_does_not_echo_url(socket_path, pr, code):
    with pytest.raises(pr.PublicReaderUnavailable) as exc:
        await exchange(socket_path, encode({"v": 1, "ok": False, "error": {"code": code, "category": "transport"}}), lambda: pr.read_via_reader(socket_path, "https://example.com/?sensitive=query"))
    assert exc.value.code == code
    assert "sensitive" not in str(exc.value)


async def test_policy_and_http_errors_keep_existing_caller_semantics(socket_path, pr):
    with pytest.raises(pr.PublicReaderPolicyError):
        await exchange(socket_path, encode({"v": 1, "ok": False, "error": {"code": "source_policy_denied", "category": "policy"}}), lambda: pr.read_via_reader(socket_path, "https://example.com"))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        await exchange(socket_path, encode({"v": 1, "ok": False, "error": {"code": "source_http_error", "category": "http", "status": 403}}), lambda: pr.read_via_reader(socket_path, "https://example.com"))
    assert exc.value.response.status_code == 403


async def test_missing_socket_fails_closed(socket_path, pr):
    with pytest.raises(pr.PublicReaderUnavailable) as exc:
        await pr.read_via_reader(socket_path, "https://example.com")
    assert exc.value.code == "reader_unavailable"


async def test_wrong_path_and_world_writable_socket_are_rejected(socket_path, pr):
    with pytest.raises(pr.PublicReaderUnavailable):
        await pr.reader_health("/tmp/arbitrary.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    os.chmod(socket_path, 0o666)
    try:
        with pytest.raises(pr.PublicReaderUnavailable):
            await pr.reader_health(socket_path)
    finally:
        sock.close()


@pytest.mark.parametrize("change", [
    {"url": "http://127.0.0.1"}, {"url": "https://x.local"},
    {"url": "https://example.com:8080"}, {"url": "https://user:pass@example.com"},
    {"url": "x" * 8193}, {"discover_links": 1}, {"external_links": "true"},
    {"headers": {}}, {"method": "POST"}, {"v": True},
])
def test_server_rejects_request_expansion_and_non_public_url_shapes(pr, change):
    request = {"v": 1, "action": "read", "url": "https://example.com", "discover_links": False, "external_links": False, **change}
    with pytest.raises(pr.PublicReaderPolicyError):
        pr._validate_request(request)


async def run_service(path, pr, uid, callback):
    service = pr._ReaderServer(uid, "a" * 40)
    server = await asyncio.get_running_loop().create_unix_server(lambda: pr._ReaderProtocol(service), path=path)
    os.chmod(path, 0o600)
    try:
        return await callback(service)
    finally:
        server.close()
        await server.wait_closed()
        Path(path).unlink(missing_ok=True)


async def raw_request(path, frame):
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write(frame)
    await writer.drain()
    result = await reader.read()
    writer.close()
    await writer.wait_closed()
    return result


async def test_authorized_health_real_socket(socket_path, pr, monkeypatch):
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())
    result = await run_service(socket_path, pr, os.geteuid(), lambda _: raw_request(socket_path, encode({"v": 1, "action": "health"})))
    assert json.loads(result) == {"v": 1, "ok": True, "protocol": 1, "revision": "a" * 40}


async def test_peer_is_rejected_before_any_request_read(socket_path, pr, monkeypatch):
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())

    async def rejected(_):
        reader, writer = await asyncio.open_unix_connection(socket_path)
        # Send nothing: rejection must not wait for a frame or return details.
        result = await asyncio.wait_for(reader.read(), 0.5)
        writer.close()
        await writer.wait_closed()
        return result

    assert await run_service(socket_path, pr, os.geteuid() + 1, rejected) == b""


async def test_server_rejects_oversized_or_duplicate_frames(socket_path, pr, monkeypatch):
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())
    for frame in [b"x" * 16385 + b"\n", b'{"v":1,"v":1,"action":"health"}\n']:
        result = await run_service(socket_path, pr, os.geteuid(), lambda _: raw_request(socket_path, frame))
        assert json.loads(result)["error"] == {"code": "source_policy_denied", "category": "policy"}


async def test_server_busy_does_not_start_fifth_child(socket_path, pr, monkeypatch):
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())

    async def busy(service):
        service.active = 4
        return await raw_request(socket_path, encode({"v": 1, "action": "read", "url": "https://example.com", "discover_links": False, "external_links": False}))

    result = await run_service(socket_path, pr, os.geteuid(), busy)
    assert json.loads(result)["error"] == {"code": "reader_busy", "category": "transport"}


@pytest.mark.parametrize("kind,want", [("dns", "source_dns_unavailable"), ("timeout", "source_timeout"), ("transport", "source_transport_unavailable"), ("policy", "source_policy_denied"), ("unknown", "source_transport_unavailable")])
def test_child_error_mapping_is_finite(pr, kind, want):
    errors = {
        "dns": socket.gaierror(-3, "secret DNS detail"),
        "timeout": httpx.ReadTimeout("secret timeout"),
        "transport": httpx.ConnectError("secret connection"),
        "policy": ValueError("secret policy"), "unknown": RuntimeError("secret unexpected"),
    }
    result = pr._source_error(errors[kind])
    assert result["error"]["code"] == want
    assert "secret" not in json.dumps(result)


async def test_child_timeout_kills_own_process_group(pr, monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    script = tmp_path / "sleep.py"
    script.write_text("import os,time\nfrom pathlib import Path\nPath(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\ntime.sleep(10)\n")
    monkeypatch.setattr(pr, "__file__", str(script))
    monkeypatch.setattr(pr, "CHILD_TIMEOUT", 0.15)
    result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com", "discover_links": False, "external_links": False})
    assert result["error"]["code"] == "source_timeout"
    pid = int(pidfile.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_child_oversize_is_killed_before_forwarding(pr, monkeypatch, tmp_path):
    script = tmp_path / "large.py"
    script.write_text("import sys,time\nsys.stdout.buffer.write(b'x'*(4*1024*1024+2));sys.stdout.flush();time.sleep(10)\n")
    monkeypatch.setattr(pr, "__file__", str(script))
    result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com", "discover_links": False, "external_links": False})
    assert result["error"]["code"] == "reader_invalid_response"


def test_child_environment_drops_business_and_model_permissions(pr, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-read")
    monkeypatch.setenv("LEAD_STATE_DIR", "/private/business")
    monkeypatch.setenv("HTTP_PROXY", "http://private-proxy")
    env = pr._child_environment()
    assert set(env) == {"PATH", "LANG", "LD_LIBRARY_PATH"}
    assert env["LANG"] == "C.UTF-8"
    assert env["LD_LIBRARY_PATH"] == str(Path(sys.executable).parent.parent / "lib")


def test_socket_activation_rejects_foreign_process_or_extra_fds(pr, monkeypatch):
    monkeypatch.setenv("LISTEN_PID", str(os.getpid() + 1))
    monkeypatch.setenv("LISTEN_FDS", "1")
    with pytest.raises(pr.PublicReaderUnavailable):
        pr._activation_socket()
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    monkeypatch.setenv("LISTEN_FDS", "2")
    with pytest.raises(pr.PublicReaderUnavailable):
        pr._activation_socket()


async def test_client_timeout_is_bounded(socket_path, pr, monkeypatch):
    monkeypatch.setattr(pr, "CLIENT_TIMEOUT", 0.02)
    done = asyncio.Event()

    async def stall(reader, writer):
        await reader.readline()
        await done.wait()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(stall, path=socket_path)
    os.chmod(socket_path, 0o600)
    try:
        with pytest.raises(pr.PublicReaderUnavailable) as exc:
            await pr.read_via_reader(socket_path, "https://example.com")
        assert exc.value.code == "reader_unavailable"
    finally:
        done.set()
        server.close()
        await server.wait_closed()
        await asyncio.sleep(0)


async def test_stalled_handshake_is_bounded(socket_path, pr, monkeypatch):
    monkeypatch.setattr(pr, "HANDSHAKE_TIMEOUT", 0.02)
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())

    async def stall(_):
        reader, writer = await asyncio.open_unix_connection(socket_path)
        result = await asyncio.wait_for(reader.read(), 0.5)
        writer.close()
        await writer.wait_closed()
        return result

    result = await run_service(socket_path, pr, os.geteuid(), stall)
    assert json.loads(result)["error"] == {"code": "source_policy_denied", "category": "policy"}


async def test_child_timeout_kills_descendants_that_ignore_term(pr, monkeypatch, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable,'-c',"
        + repr("import os,signal,time\nfrom pathlib import Path\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath(" + repr(str(pidfile)) + ").write_text(str(os.getpid()))\ntime.sleep(10)")
        + "],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\ntime.sleep(10)\n"
    )
    monkeypatch.setattr(pr, "__file__", str(script))
    monkeypatch.setattr(pr, "CHILD_TIMEOUT", 0.3)
    try:
        result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com", "discover_links": False, "external_links": False})
        assert result["error"]["code"] == "source_timeout"
        pid = int(pidfile.read_text())
        await asyncio.sleep(0.05)
        # Linux may briefly retain an orphan zombie until PID 1 reaps it.
        procstat = Path(f"/proc/{pid}/stat")
        if procstat.exists() and procstat.read_text().split()[2] == "Z":
            return
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if pidfile.exists():
            try:
                os.kill(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass


async def test_real_read_child_only_loads_neighboring_pure_fetch_module(pr, monkeypatch, tmp_path):
    script = tmp_path / "public_reader.py"
    script.write_bytes(Path(pr.__file__).read_bytes())
    (tmp_path / "fetch.py").write_text(
        "import os\n"
        "async def _fetch_direct(url, *, discover_links, external_links):\n"
        "    assert set(os.environ) <= {'PATH','LANG','LD_LIBRARY_PATH','LC_CTYPE','__CF_USER_TEXT_ENCODING'}\n"
        "    assert url == 'https://example.com/product' and discover_links and not external_links\n"
        "    return " + repr(OBSERVED) + "\n"
    )
    monkeypatch.setattr(pr, "__file__", str(script))
    result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com/product", "discover_links": True, "external_links": False})
    assert result == {"v": 1, "ok": True, "observed": OBSERVED}


async def test_unauthorized_protocol_never_delivers_request_bytes(pr, monkeypatch):
    class Transport:
        def __init__(self):
            self.closed = False

        def get_extra_info(self, key):
            assert key == "socket"
            return object()

        def close(self):
            self.closed = True

    monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid() + 1)
    service = pr._ReaderServer(os.geteuid(), "a" * 40)
    protocol = pr._ReaderProtocol(service)
    transport = Transport()
    protocol.connection_made(transport)
    protocol.data_received(b"secret request bytes")
    assert transport.closed
    assert not protocol._stream_reader._buffer


async def test_service_enforces_four_real_children(socket_path, pr, monkeypatch, tmp_path):
    if not hasattr(socket, "SO_PEERCRED"):
        monkeypatch.setattr(pr, "_peer_uid", lambda sock: os.geteuid())
    script = tmp_path / "public_reader.py"
    script.write_bytes(Path(pr.__file__).read_bytes())
    release = tmp_path / "release"
    (tmp_path / "fetch.py").write_text(
        "import asyncio,os\nfrom pathlib import Path\n"
        "async def _fetch_direct(url, *, discover_links, external_links):\n"
        "    Path(" + repr(str(tmp_path / "started-")) + "+str(os.getpid())).touch()\n"
        "    while not Path(" + repr(str(release)) + ").exists():\n        await asyncio.sleep(.01)\n"
        "    return " + repr(OBSERVED) + "\n"
    )
    monkeypatch.setattr(pr, "__file__", str(script))

    async def exercise(_):
        tasks = [asyncio.create_task(pr.read_via_reader(socket_path, "https://example.com")) for _ in range(4)]
        try:
            async with asyncio.timeout(3):
                while len(list(tmp_path.glob("started-*"))) < 4:
                    await asyncio.sleep(0.01)
            with pytest.raises(pr.PublicReaderUnavailable) as exc:
                await pr.read_via_reader(socket_path, "https://example.com")
            assert exc.value.code == "reader_busy"
            release.touch()
            assert await asyncio.gather(*tasks) == [OBSERVED] * 4
            assert len(list(tmp_path.glob("started-*"))) == 4
        finally:
            release.touch()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    await run_service(socket_path, pr, os.geteuid(), exercise)


def test_socket_trust_rejects_writable_or_untrusted_parents(socket_path, pr, monkeypatch):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(socket_path)
    os.chmod(socket_path, 0o600)
    root_lstat = pr.os.lstat
    parent = Path(socket_path).parent
    try:
        for index, replacement in [(0, 0o40777), (4, os.geteuid() + 1000)]:
            def unsafe(candidate, *args, **kwargs):
                info = root_lstat(candidate, *args, **kwargs)
                if Path(candidate) == parent:
                    fields = list(info)
                    fields[index] = replacement
                    return os.stat_result(fields)
                return info

            monkeypatch.setattr(pr.os, "lstat", unsafe)
            with pytest.raises(pr.PublicReaderUnavailable):
                pr._trusted_socket(socket_path)
    finally:
        monkeypatch.setattr(pr.os, "lstat", root_lstat)
        sock.close()


def test_socket_trust_rejects_symlink_instead_of_socket(socket_path, pr):
    target = socket_path + ".real"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(target)
    os.chmod(target, 0o600)
    Path(socket_path).symlink_to(target)
    try:
        with pytest.raises(pr.PublicReaderUnavailable):
            pr._trusted_socket(socket_path)
    finally:
        sock.close()


async def test_read_child_filters_unsafe_candidate_links_without_losing_page(pr, monkeypatch, tmp_path):
    script = tmp_path / "public_reader.py"
    script.write_bytes(Path(pr.__file__).read_bytes())
    # Exercise the existing candidate extraction: these hrefs are observations,
    # not requests, so private/odd-port hrefs can legitimately appear here.
    hrefs = [
        "https://public.example.net/features", "http://localhost/help",
        "http://127.0.0.1/product", "http://192.168.1.9/docs",
        "https://public.example.net:8443/help",
    ]
    fetch_source = Path(pr.__file__).with_name("fetch.py").read_text()
    (tmp_path / "fetch.py").write_text(
        fetch_source + "\n\nasync def _fetch_direct(url, *, discover_links, external_links):\n"
        "    assert external_links and not discover_links\n"
        "    return {\"url\":url,\"title\":\"Public product\",\"text\":\"Observed public page\","
        "\"requires_javascript\":False,\"links\":observed_source_urls(" + repr(hrefs) + ",url)}\n"
    )
    monkeypatch.setattr(pr, "__file__", str(script))
    result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com/product", "discover_links": False, "external_links": True})
    assert result == {
        "v": 1, "ok": True,
        "observed": {
            "url": "https://example.com/product", "title": "Public product",
            "text": "Observed public page", "requires_javascript": False,
            "links": ["https://public.example.net/features"],
        },
    }


@pytest.mark.parametrize("change", [
    {"unexpected": "field"}, {"links": "https://public.example.net"},
    {"links": [123]}, {"links": ["https://public.example.net"] * 11},
])
async def test_read_child_candidate_filter_still_rejects_unknown_schema(pr, monkeypatch, tmp_path, change):
    script = tmp_path / "public_reader.py"
    script.write_bytes(Path(pr.__file__).read_bytes())
    (tmp_path / "fetch.py").write_text(
        "async def _fetch_direct(url, *, discover_links, external_links):\n"
        "    return " + repr({**OBSERVED, **change}) + "\n"
    )
    monkeypatch.setattr(pr, "__file__", str(script))
    result = await pr._run_child({"v": 1, "action": "read", "url": "https://example.com/product", "discover_links": False, "external_links": True})
    assert result == {"v": 1, "ok": False, "error": {"code": "source_policy_denied", "category": "policy"}}
