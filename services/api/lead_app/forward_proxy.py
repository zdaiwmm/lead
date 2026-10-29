"""Per-profile HTTP CONNECT proxy, with DNS pinning and no upstream failover.

TLS stays between the actual browser and website. This proxy forwards encrypted
bytes; it never replaces a social webpage with an HTTP-client implementation.
"""

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import ssl
import time
from urllib.parse import quote, urlsplit

import httpx

from .config import CONFIG


class BrowserForwardProxy:
    def __init__(self, aid, upstream=None):
        self.aid = aid
        self.upstream = upstream
        self.server = None
        self.port = None
        self.password = hmac.new(
            CONFIG["session_secret"].encode(),
            ("forward:" + aid).encode(),
            hashlib.sha256,
        ).hexdigest()
        self.cache = {}
        self.dns_locks = {}
        self.tasks = set()
        self.bytes_forwarded = 0
        self.connections = 0
        self.denied = 0
        self.upstream_failures = 0

    @property
    def auth(self):
        return (
            "Basic "
            + base64.b64encode((self.aid + ":" + self.password).encode()).decode()
        )

    @property
    def browser_proxy(self):
        return {
            "server": f"http://127.0.0.1:{self.port}",
            "username": self.aid,
            "password": self.password,
        }

    async def start(self):
        self.server = await asyncio.start_server(
            self.handle, "127.0.0.1", 0, limit=65536
        )
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*list(self.tasks), return_exceptions=True)

    @staticmethod
    def public_ip(value):
        ip = ipaddress.ip_address(value)
        if not ip.is_global:
            raise ValueError("Private browser destination is forbidden")
        return ip

    async def resolve(self, host):
        host = host.lower().rstrip(".")
        try:
            return str(self.public_ip(host))
        except ValueError:
            try:
                ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                raise
        if (
            "." not in host
            or host.endswith((".local", ".localhost", ".internal"))
            or host == "localhost"
        ):
            raise ValueError("Private browser hostname is forbidden")
        async with self.dns_locks.setdefault(host, asyncio.Lock()):
            cached = self.cache.get(host)
            if cached and cached[1] > time.monotonic():
                return cached[0]
            # DNS uses the SAME profile proxy/upstream. The resolver is a fixed
            # public IP, so its CONNECT does not recurse into name resolution.
            proxy_url = (
                f"http://{quote(self.aid)}:{self.password}@127.0.0.1:{self.port}"
            )
            async with httpx.AsyncClient(
                proxy=proxy_url, timeout=12, trust_env=False
            ) as client:
                response = await client.get(
                    "https://1.1.1.1/dns-query",
                    params={"name": host, "type": "A"},
                    headers={"Accept": "application/dns-json"},
                )
                response.raise_for_status()
                body = response.json()
            if body.get("Status") != 0:
                raise ValueError("Public hostname resolution failed")
            answers = [a for a in body.get("Answer", []) if a.get("type") == 1]
            if not answers:
                raise ValueError("Public hostname has no IPv4 address")
            ips = [str(self.public_ip(a["data"])) for a in answers]
            ttl = max(1, min(60, min(a.get("TTL", 30) for a in answers)))
            self.cache[host] = (ips[0], time.monotonic() + ttl)
            return ips[0]

    async def validate_url(self, url):
        p = urlsplit(url)
        if (
            p.scheme not in ("http", "https")
            or not p.hostname
            or p.username
            or p.password
            or p.port not in (None, 80, 443)
        ):
            raise ValueError("Unsupported public browser URL")
        return await self.resolve(p.hostname)

    async def connect(self, ip, port):
        self.public_ip(ip)
        if not self.upstream:
            return await asyncio.wait_for(asyncio.open_connection(ip, port), 15)
        upstream = urlsplit(self.upstream["server"])
        # There is intentionally no direct-connection fallback in any error path.
        try:
            tls = ssl.create_default_context() if upstream.scheme == "https" else None
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    upstream.hostname,
                    upstream.port,
                    ssl=tls,
                    server_hostname=upstream.hostname if tls else None,
                ),
                15,
            )
            if upstream.scheme in ("http", "https"):
                host = "[" + ip + "]" if ":" in ip else ip
                headers = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                if self.upstream.get("username"):
                    credentials = base64.b64encode(
                        (
                            self.upstream["username"]
                            + ":"
                            + self.upstream.get("password", "")
                        ).encode()
                    ).decode()
                    headers += "Proxy-Authorization: Basic " + credentials + "\r\n"
                writer.write((headers + "\r\n").encode())
                await writer.drain()
                result = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
                if result.split(b" ", 2)[1] != b"200":
                    raise ConnectionError("Upstream proxy rejected CONNECT")
            elif upstream.scheme == "socks5":
                auth = bool(self.upstream.get("username"))
                writer.write(b"\x05\x01" + (b"\x02" if auth else b"\x00"))
                await writer.drain()
                method = await asyncio.wait_for(reader.readexactly(2), 10)
                if method != b"\x05" + (b"\x02" if auth else b"\x00"):
                    raise ConnectionError("Upstream SOCKS authentication unavailable")
                if auth:
                    user = self.upstream["username"].encode()
                    password = self.upstream.get("password", "").encode()
                    if max(len(user), len(password)) > 255:
                        raise ValueError("Invalid SOCKS credentials length")
                    writer.write(
                        b"\x01"
                        + bytes([len(user)])
                        + user
                        + bytes([len(password)])
                        + password
                    )
                    await writer.drain()
                    if await reader.readexactly(2) != b"\x01\x00":
                        raise ConnectionError("Upstream SOCKS authentication failed")
                address = ipaddress.ip_address(ip)
                writer.write(
                    b"\x05\x01\x00"
                    + (b"\x01" if address.version == 4 else b"\x04")
                    + address.packed
                    + port.to_bytes(2, "big")
                )
                await writer.drain()
                reply = await asyncio.wait_for(reader.readexactly(4), 10)
                if reply[:2] != b"\x05\x00":
                    raise ConnectionError("Upstream SOCKS CONNECT failed")
                size = (
                    4
                    if reply[3] == 1
                    else 16
                    if reply[3] == 4
                    else (await reader.readexactly(1))[0]
                )
                await reader.readexactly(size + 2)
            else:
                raise ValueError("Unsupported upstream proxy")
            return reader, writer
        except BaseException:
            self.upstream_failures += 1
            if "writer" in locals():
                writer.close()
            raise

    async def pipe(self, reader, writer):
        try:
            while data := await reader.read(65536):
                self.bytes_forwarded += len(data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        remote = None
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15)
            lines = header.decode("iso-8859-1").split("\r\n")
            method, target, version = lines[0].split(" ", 2)
            headers = {
                k.strip().lower(): v.strip()
                for line in lines[1:]
                if ":" in line
                for k, v in [line.split(":", 1)]
            }
            if not hmac.compare_digest(
                headers.get("proxy-authorization", ""), self.auth
            ):
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="lead"\r\nContent-Length: 0\r\n\r\n'
                )
                await writer.drain()
                return
            if method == "CONNECT":
                authority = urlsplit("//" + target)
                host, port = authority.hostname, authority.port or 443
                if port != 443 or not host:
                    raise ValueError("Unsupported CONNECT target")
                ip = await self.resolve(host)
                up_reader, remote = await self.connect(ip, port)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                p = urlsplit(target)
                if p.scheme != "http" or not p.hostname or p.username or p.password:
                    raise ValueError("Invalid HTTP proxy target")
                port = p.port or 80
                if (
                    p.hostname == "127.0.0.1"
                    and port == 8088
                    and p.path.startswith("/lab/" + self.aid + "/")
                ):
                    up_reader, remote = await asyncio.open_connection("127.0.0.1", 8088)
                else:
                    if port != 80:
                        raise ValueError("Unsupported public HTTP port")
                    ip = await self.resolve(p.hostname)
                    up_reader, remote = await self.connect(ip, port)
                path = p.path or "/"
                if p.query:
                    path += "?" + p.query
                outgoing = f"{method} {path} HTTP/1.1\r\n"
                outgoing += "".join(
                    line + "\r\n"
                    for line in lines[1:]
                    if line
                    and not line.lower().startswith(
                        ("proxy-authorization:", "proxy-connection:", "connection:")
                    )
                )
                remote.write(
                    (outgoing + "Connection: close\r\n\r\n").encode("iso-8859-1")
                )
                await remote.drain()
            self.connections += 1
            pipes = [
                asyncio.create_task(self.pipe(reader, remote)),
                asyncio.create_task(self.pipe(up_reader, writer)),
            ]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            for p in pipes:
                p.cancel()
            await asyncio.gather(*pipes, return_exceptions=True)
        except (Exception, asyncio.CancelledError):
            self.denied += 1
            try:
                writer.write(
                    b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
        finally:
            if remote:
                remote.close()
            writer.close()
            self.tasks.discard(task)
