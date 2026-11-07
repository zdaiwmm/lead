"""Public-only, DNS-pinned product fetcher. Every redirect is checked again."""

import asyncio
import ipaddress
import re
import socket
import time
from functools import lru_cache
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup


@lru_cache(maxsize=512)
def public_addresses(hostname, port, minute):
    addresses = {
        a[4][0] for a in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    }
    if addresses and all(ipaddress.ip_address(ip).is_global for ip in addresses):
        return addresses
    # Local VPNs commonly return RFC 2544 fake IPs. These are NEVER accepted as
    # destinations. Resolve via TLS to a fixed public resolver, validate its A
    # answers, and pin the subsequent fetch to the real public address instead.
    fake_network = ipaddress.ip_network("198.18.0.0/15")
    if not addresses or not any(
        ipaddress.ip_address(ip) in fake_network for ip in addresses
    ):
        raise ValueError("只允许读取公开网站")
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.get(
            "https://1.1.1.1/dns-query",
            params={"name": hostname, "type": "A"},
            headers={"Accept": "application/dns-json"},
        )
        response.raise_for_status()
        body = response.json()
        if body.get("Status") != 0:
            raise ValueError("无法解析公开网址")
        public = {a["data"] for a in body.get("Answer", []) if a.get("type") == 1}
        if not public or any(not ipaddress.ip_address(ip).is_global for ip in public):
            raise ValueError("只允许读取公开网站")
        return public


def resolve_public(url):
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("只支持公开的 http/https 网址")
    if p.port not in (None, 80, 443):
        raise ValueError("网址端口不可用")
    host = p.hostname.lower().rstrip(".")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if (
        (literal and not literal.is_global)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal"))
        or "." not in host
    ):
        raise ValueError("只允许读取公开网站")
    addresses = public_addresses(
        host,
        p.port or (443 if p.scheme == "https" else 80),
        int(time.monotonic() // 60),
    )
    return p, sorted(addresses)[0]


def product_links(soup, url):
    """Prefer first-party pages describing actual products over generic navigation."""
    root = urlsplit(url).hostname.removeprefix("www.")
    ranked = {}
    for anchor in soup.select("a[href]"):
        try:
            link = urldefrag(urljoin(url, anchor["href"]))[0]
            parsed = urlsplit(link)
            host = parsed.hostname or ""
            if (
                parsed.scheme not in ("http", "https")
                or parsed.username
                or parsed.password
                or not (host == root or host.endswith("." + root))
                or link == urldefrag(url)[0]
                or len(link) > 2000
            ):
                continue
            label = (parsed.path + " " + anchor.get_text(" ", strip=True)).lower()
            for priority, pattern in enumerate(
                (
                    r"features?|products?|功能|产品",
                    r"docs?|help|guide|文档|指南|帮助",
                    r"solutions?|customers?|case-stud|方案|案例",
                )
            ):
                if re.search(pattern, label) or (
                    priority == 1 and host.startswith(("docs.", "help."))
                ):
                    ranked[link] = min(priority, ranked.get(link, priority))
                    break
        except ValueError:
            continue
    return sorted(ranked, key=ranked.get)[:3]


async def pinned_get(client, url):
    p, ip = await asyncio.to_thread(resolve_public, url)
    authority = ("[" + ip + "]") if ":" in ip else ip
    pinned = p._replace(
        netloc=authority + (":" + str(p.port) if p.port else "")
    ).geturl()
    request = client.build_request(
        "GET",
        pinned,
        headers={"Host": p.hostname, "User-Agent": "lead-product-reader/1.0"},
    )
    request.extensions["sni_hostname"] = p.hostname
    return await client.send(request, stream=True)


async def fetch_product(url, *, discover_links=False):
    async with httpx.AsyncClient(
        timeout=15, follow_redirects=False, trust_env=False
    ) as client:
        for _ in range(4):
            response = await pinned_get(client, url)
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("location", ""))
                    continue
                response.raise_for_status()
                if not any(
                    t in response.headers.get("content-type", "")
                    for t in ("text/html", "text/plain")
                ):
                    raise ValueError("该网址不是网页")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 2_000_000:
                        raise ValueError("网页内容过大")
                soup = BeautifulSoup(bytes(data), "html.parser")
                links = product_links(soup, url) if discover_links else []
                for tag in soup(["script", "style", "nav", "footer"]):
                    tag.decompose()
                return {
                    **({"links": links} if discover_links else {}),
                    "url": url,
                    "title": soup.title.get_text() if soup.title else "",
                    "text": soup.get_text(" ", strip=True)[:18000],
                }
            finally:
                await response.aclose()
    raise ValueError("网页跳转过多")
