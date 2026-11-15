"""Narrow, socket-activated public-page reader. Never load business configuration.

The API keeps its private network namespace. This endpoint accepts only one GET
page description and executes the DNS-pinned fetcher in a short-lived child.
No protocol error includes a URL, page content, or exception detail.
"""

import argparse
import asyncio
import importlib.util
import ipaddress
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
from pathlib import Path
from urllib.parse import urlsplit

PUBLIC_READER_SOCKET = "/run/lead-public-reader/reader.sock"
PROTOCOL = 1
MAX_REQUEST = 16 * 1024
MAX_RESPONSE = 4 * 1024 * 1024
HANDSHAKE_TIMEOUT = 3
CHILD_TIMEOUT = 30
CLIENT_TIMEOUT = 35
MAX_CHILDREN = 4
TRANSPORT_CODES = frozenset({
    "source_dns_unavailable", "source_transport_unavailable", "source_timeout",
    "reader_busy", "reader_unavailable", "reader_invalid_response",
})


class PublicReaderUnavailable(OSError):
    """Finite transport status, deliberately without the underlying exception."""

    def __init__(self, code="reader_unavailable"):
        self.code = code if isinstance(code, str) and code in TRANSPORT_CODES else "reader_unavailable"
        super().__init__(self.code)


class PublicReaderPolicyError(ValueError):
    def __init__(self):
        super().__init__("公开网页读取被策略拒绝")


def _error(code, category="transport", status=None):
    error = {"code": code, "category": category}
    if status is not None:
        error["status"] = status
    return {"v": PROTOCOL, "ok": False, "error": error}


def _public_url(url, maximum=8192):
    if (not isinstance(url, str) or not url or len(url) > maximum
            or any(ord(c) <= 32 or ord(c) == 127 for c in url) or "\\" in url):
        raise PublicReaderPolicyError()
    try:
        url.encode("utf-8")
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in ("http", "https") or not host
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 80, 443)
                or host == "localhost" or host.endswith((".localhost", ".local", ".internal"))
                or "." not in host):
            raise PublicReaderPolicyError()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise PublicReaderPolicyError()
    except (ValueError, UnicodeError):
        raise PublicReaderPolicyError() from None
    return url


def _validate_request(value):
    if (not isinstance(value, dict) or type(value.get("v")) is not int
            or value["v"] != PROTOCOL):
        raise PublicReaderPolicyError()
    if value.get("action") == "health" and set(value) == {"v", "action"}:
        return value
    if (set(value) != {"v", "action", "url", "discover_links", "external_links"}
            or value.get("action") != "read"
            or type(value["discover_links"]) is not bool
            or type(value["external_links"]) is not bool):
        raise PublicReaderPolicyError()
    _public_url(value["url"])
    return value


def _observed(value):
    required = {"url", "title", "text", "requires_javascript"}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {"links"}
            or not isinstance(value["title"], str) or len(value["title"]) > 2_000_000
            or not isinstance(value["text"], str) or len(value["text"]) > 18000
            or type(value["requires_javascript"]) is not bool):
        raise PublicReaderPolicyError()
    _public_url(value["url"])
    try:
        value["title"].encode("utf-8")
        value["text"].encode("utf-8")
    except UnicodeError:
        raise PublicReaderPolicyError() from None
    if "links" in value:
        if not isinstance(value["links"], list) or len(value["links"]) > 10:
            raise PublicReaderPolicyError()
        for link in value["links"]:
            _public_url(link, maximum=2000)
    return value


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise PublicReaderPolicyError()
        value[key] = item
    return value


def _decode(frame, maximum):
    if len(frame) > maximum or not frame.endswith(b"\n") or b"\n" in frame[:-1]:
        raise PublicReaderPolicyError()
    try:
        return json.loads(frame[:-1], object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise PublicReaderPolicyError() from None


def _encode(value, maximum):
    try:
        frame = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise PublicReaderPolicyError() from None
    if len(frame) > maximum:
        raise PublicReaderPolicyError()
    return frame


def _response(value, *, health=False):
    if (not isinstance(value, dict) or type(value.get("v")) is not int
            or value["v"] != PROTOCOL or type(value.get("ok")) is not bool):
        raise PublicReaderPolicyError()
    if value["ok"]:
        if health:
            if (set(value) != {"v", "ok", "protocol", "revision"}
                    or type(value["protocol"]) is not int or value["protocol"] != PROTOCOL
                    or not isinstance(value["revision"], str)
                    or re.fullmatch(r"[0-9a-f]{40}", value["revision"]) is None):
                raise PublicReaderPolicyError()
        elif set(value) != {"v", "ok", "observed"}:
            raise PublicReaderPolicyError()
        else:
            _observed(value["observed"])
        return value
    error = value.get("error")
    if (set(value) != {"v", "ok", "error"} or not isinstance(error, dict)
            or not isinstance(error.get("code"), str)):
        raise PublicReaderPolicyError()
    if error.get("category") == "transport":
        valid = set(error) == {"code", "category"} and error.get("code") in TRANSPORT_CODES
    elif error.get("category") == "policy":
        valid = set(error) == {"code", "category"} and error.get("code") == "source_policy_denied"
    elif error.get("category") == "http":
        valid = (set(error) == {"code", "category", "status"}
                 and error.get("code") == "source_http_error"
                 and type(error.get("status")) is int and 100 <= error["status"] <= 599)
    else:
        valid = False
    if not valid:
        raise PublicReaderPolicyError()
    return value


def _trusted_socket(path):
    if not isinstance(path, str) or path != PUBLIC_READER_SOCKET:
        raise PublicReaderUnavailable()
    try:
        location = Path(path)
        for parent in location.parents:
            info = os.lstat(parent)
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
                raise PublicReaderUnavailable()
        info = os.lstat(location)
        if (not stat.S_ISSOCK(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid not in (0, os.geteuid())):
            raise PublicReaderUnavailable()
    except OSError:
        raise PublicReaderUnavailable() from None


def _raise_error(value, url):
    error = value["error"]
    if error["category"] == "transport":
        raise PublicReaderUnavailable(error["code"])
    if error["category"] == "policy":
        raise PublicReaderPolicyError()
    import httpx

    request = httpx.Request("GET", url)
    response = httpx.Response(error["status"], request=request)
    raise httpx.HTTPStatusError("public source HTTP status", request=request, response=response)


async def _client(path, request):
    _validate_request(request)
    frame = _encode(request, MAX_REQUEST)
    _trusted_socket(path)
    writer = None
    health = request["action"] == "health"
    try:
        async with asyncio.timeout(5 if health else CLIENT_TIMEOUT):
            reader, writer = await asyncio.open_unix_connection(path, limit=MAX_RESPONSE + 1)
            writer.write(frame)
            await writer.drain()
            try:
                received = await reader.readuntil(b"\n")
                if await reader.read(1):
                    raise PublicReaderPolicyError()
                value = _response(_decode(received, MAX_RESPONSE), health=health)
            except (PublicReaderPolicyError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                raise PublicReaderUnavailable("reader_invalid_response") from None
    except TimeoutError:
        raise PublicReaderUnavailable("reader_unavailable") from None
    except OSError as exc:
        if isinstance(exc, PublicReaderUnavailable):
            raise
        raise PublicReaderUnavailable() from None
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass
    if not value["ok"]:
        _raise_error(value, request.get("url", "https://example.invalid"))
    return {"protocol": value["protocol"], "revision": value["revision"]} if health else value["observed"]


async def read_via_reader(socket_path, url, *, discover_links=False, external_links=False):
    return await _client(socket_path, {
        "v": PROTOCOL, "action": "read", "url": url,
        "discover_links": discover_links, "external_links": external_links,
    })


async def reader_health(socket_path):
    return await _client(socket_path, {"v": PROTOCOL, "action": "health"})


def _source_error(exc):
    import httpx

    current = exc
    for _ in range(8):
        if isinstance(current, socket.gaierror):
            return _error("source_dns_unavailable")
        current = current.__cause__ or current.__context__
        if current is None:
            break
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return _error("source_timeout")
    if isinstance(exc, httpx.HTTPStatusError):
        return _error("source_http_error", "http", exc.response.status_code)
    if isinstance(exc, (ValueError, PermissionError)):
        return _error("source_policy_denied", "policy")
    return _error("source_transport_unavailable")


def _child_environment():
    prefix = Path(sys.executable).parent.parent
    return {"PATH": "/python/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LD_LIBRARY_PATH": str(prefix / "lib")}


async def _stop_child(process):
    # Each child has its own session. Never signal a shared worker/process group.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 0.5)
    except TimeoutError:
        pass
    # The session leader can exit while a descendant ignores SIGTERM and has
    # already closed its inherited pipes. Reap that entire owned group anyway.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 1)
    except TimeoutError:
        pass


async def _run_child(request):
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-B", str(Path(__file__).resolve()), "--read",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=_child_environment(), start_new_session=True,
        )
        async with asyncio.timeout(CHILD_TIMEOUT):
            process.stdin.write(_encode(request, MAX_REQUEST))
            await process.stdin.drain()
            process.stdin.close()
            data = bytearray()
            while True:
                chunk = await process.stdout.read(min(65536, MAX_RESPONSE + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_RESPONSE:
                    await _stop_child(process)
                    return _error("reader_invalid_response")
            await process.wait()
            if process.returncode != 0:
                return _error("reader_unavailable")
            try:
                return _response(_decode(bytes(data), MAX_RESPONSE))
            except PublicReaderPolicyError:
                return _error("reader_invalid_response")
    except TimeoutError:
        if process is not None:
            await _stop_child(process)
        return _error("source_timeout")
    except asyncio.CancelledError:
        if process is not None:
            await _stop_child(process)
        raise
    except (OSError, ValueError):
        if process is not None:
            await _stop_child(process)
        return _error("reader_unavailable")


def _peer_uid(sock):
    credentials = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1]


class _ReaderServer:
    def __init__(self, client_uid, revision):
        self.client_uid = client_uid
        self.revision = revision
        self.active = 0

    async def handle(self, reader, writer):
        try:
            # Identity is checked before touching any untrusted request bytes.
            if _peer_uid(writer.get_extra_info("socket")) != self.client_uid:
                return
            try:
                frame = await asyncio.wait_for(reader.readuntil(b"\n"), HANDSHAKE_TIMEOUT)
                request = _validate_request(_decode(frame, MAX_REQUEST))
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                value = _error("source_policy_denied", "policy")
            else:
                if request["action"] == "health":
                    value = {"v": PROTOCOL, "ok": True, "protocol": PROTOCOL, "revision": self.revision}
                elif self.active >= MAX_CHILDREN:
                    value = _error("reader_busy")
                else:
                    self.active += 1
                    try:
                        value = await _run_child(request)
                    finally:
                        self.active -= 1
            writer.write(_encode(value, MAX_RESPONSE))
            await asyncio.wait_for(writer.drain(), HANDSHAKE_TIMEOUT)
        except (OSError, ValueError, TimeoutError, AttributeError):
            pass
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 1)
            except (OSError, TimeoutError):
                pass



class _ReaderProtocol(asyncio.StreamReaderProtocol):
    """Authenticate synchronously, before the transport can deliver a frame."""

    def __init__(self, service):
        self._service = service
        self._authorized = False
        super().__init__(asyncio.StreamReader(limit=MAX_REQUEST + 1), service.handle)

    def connection_made(self, transport):
        try:
            if _peer_uid(transport.get_extra_info("socket")) != self._service.client_uid:
                transport.close()
                return
        except (OSError, AttributeError):
            transport.close()
            return
        self._authorized = True
        super().connection_made(transport)

    def data_received(self, data):
        if self._authorized:
            super().data_received(data)


def _activation_socket():
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        raise PublicReaderUnavailable()
    try:
        listener = socket.socket(fileno=3)
        if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM
                or listener.getsockname() != PUBLIC_READER_SOCKET
                or listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) != 1):
            listener.close()
            raise PublicReaderUnavailable()
        listener.setblocking(False)
        return listener
    except OSError:
        raise PublicReaderUnavailable() from None


async def _serve(uid, revision):
    listener = _activation_socket()
    handler = _ReaderServer(uid, revision)
    loop = asyncio.get_running_loop()
    async with await loop.create_unix_server(lambda: _ReaderProtocol(handler), sock=listener) as server:
        await server.serve_forever()


def _read_main():
    try:
        request = _validate_request(_decode(sys.stdin.buffer.readline(MAX_REQUEST + 1), MAX_REQUEST))
        if request["action"] != "read":
            raise PublicReaderPolicyError()
        # Load only this pure module. Importing lead_app/config would expose
        # business state and model credentials to a public-network process.
        spec = importlib.util.spec_from_file_location("_lead_public_fetch", Path(__file__).with_name("fetch.py"))
        fetch = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fetch)
        observed = asyncio.run(fetch._fetch_direct(
            request["url"], discover_links=request["discover_links"], external_links=request["external_links"],
        ))
        value = {"v": PROTOCOL, "ok": True, "observed": _observed(observed)}
        frame = _encode(value, MAX_RESPONSE)
    except Exception as exc:
        frame = _encode(_source_error(exc), MAX_RESPONSE)
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()
    return 0


def main():
    if sys.argv[1:] == ["--read"]:
        return _read_main()
    parser = argparse.ArgumentParser(description="Controlled public-page reader")
    sub = parser.add_subparsers(dest="mode", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--client-uid", type=int, required=True)
    serve.add_argument("--revision", required=True)
    args = parser.parse_args()
    if args.client_uid < 0 or re.fullmatch(r"[0-9a-f]{40}", args.revision) is None:
        return 2
    try:
        asyncio.run(_serve(args.client_uid, args.revision))
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
