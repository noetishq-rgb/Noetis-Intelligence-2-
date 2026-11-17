"""Bounded public HTML crawler with DNS pinning and per-redirect SSRF checks."""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import socket
import ssl
import time
import dns.exception
import dns.resolver
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

USER_AGENT = "MarketingIntelligenceResearchBot/1.0"
MAX_RESPONSE = 1_000_000
MAX_TOTAL = 5_000_000
MAX_SECONDS = 60


class CrawlError(ValueError):
    pass


def validate_url(url: str) -> tuple[str, str, int]:
    if len(url) > 2048 or any(ord(c) < 33 for c in url) or "\\" in url:
        raise CrawlError("invalid_url")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, UnicodeError) as exc:
        raise CrawlError("invalid_url") from exc
    if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password or parts.fragment:
        raise CrawlError("unsupported_url")
    if port != (443 if parts.scheme == "https" else 80):
        raise CrawlError("nonstandard_port")
    if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
        raise CrawlError("nonpublic_host")
    return parts.scheme, host, port


def public_addresses(host: str, port: int) -> list[str]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal else []
    try:
        if literal is None:
            resolver = dns.resolver.Resolver()
            for record_type in ("A", "AAAA"):
                try:
                    addresses.extend(str(item) for item in resolver.resolve(host, record_type, lifetime=2, search=False))
                except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                    continue
    except dns.exception.DNSException as exc:
        raise CrawlError("dns_failed") from exc
    def allowed(address):
        ip = ipaddress.ip_address(address)
        return ip.is_global and not (isinstance(ip, ipaddress.IPv6Address) and (ip.sixtofour or ip.teredo))
    if not addresses or any(not allowed(a) for a in addresses):
        raise CrawlError("nonpublic_address")
    return list(dict.fromkeys(addresses))


def same_site(url: str, root_host: str) -> bool:
    _, host, _ = validate_url(url)
    # Exact hostname and its www variant only. Arbitrary subdomains are not authorised.
    return host.removeprefix("www.") == root_host.removeprefix("www.")


@dataclass
class Response:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes


def fetch_public(url: str, root_host: str, deadline: float, limit: int = MAX_RESPONSE, allowed=None, trace=None) -> Response:
    for _ in range(4):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CrawlError("time_budget_exhausted")
        scheme, host, port = validate_url(url)
        if not same_site(url, root_host):
            raise CrawlError("cross_site_redirect")
        if allowed is not None and not allowed(url):
            raise CrawlError("redirect_disallowed_by_robots")
        started = time.monotonic()
        try:
            ip = public_addresses(host, port)[0]
        except CrawlError as exc:
            if trace:
                parts = urlsplit(url)
                trace({"endpoint": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")),
                    "response_status": None, "error_code": str(exc), "latency_ms": int((time.monotonic()-started)*1000)})
            raise
        # Pin the validated address. No second DNS lookup occurs in the HTTP stack.
        connection = http.client.HTTPConnection(host, port, timeout=min(8, remaining))
        sock = None
        requested_url, started, response_status, failure = url, time.monotonic(), None, None
        try:
            sock = socket.create_connection((ip, port), timeout=min(8, remaining))
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection.sock = sock
            parts = urlsplit(url)
            path = urlunsplit(("", "", parts.path or "/", parts.query, ""))
            connection.request("GET", path, headers={"Host": host, "User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.8", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            response_status = response.status
            headers = {k.lower(): v for k, v in response.getheaders()}
            if response.status in {301, 302, 303, 307, 308}:
                location = headers.get("location")
                if not location:
                    raise CrawlError("redirect_missing_location")
                target = urljoin(url, location)
                if scheme == "https" and urlsplit(target).scheme != "https":
                    raise CrawlError("https_downgrade")
                url = target
                continue
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise CrawlError("unsupported_content_encoding")
            if int(headers.get("content-length", "0")) > limit:
                raise CrawlError("response_too_large")
            chunks, size = [], 0
            while True:
                if time.monotonic() >= deadline:
                    raise CrawlError("time_budget_exhausted")
                sock.settimeout(max(.1, min(8, deadline - time.monotonic())))
                chunk = response.read(min(65536, limit + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise CrawlError("response_too_large")
            return Response(url, response.status, headers, b"".join(chunks))
        except (OSError, http.client.HTTPException, ValueError) as exc:
            failure = str(exc) if isinstance(exc, CrawlError) else "fetch_failed"
            if isinstance(exc, CrawlError):
                raise
            raise CrawlError("fetch_failed") from exc
        finally:
            connection.close()
            if sock is not None:
                sock.close()
            if trace:
                parts = urlsplit(requested_url)
                trace({"endpoint": urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")), "response_status": response_status,
                       "error_code": failure, "latency_ms": int((time.monotonic() - started) * 1000)})
    raise CrawlError("redirect_limit")


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts, self.text_parts, self.links = [], [], []
        self.description = None
        self.in_title = False
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip_depth += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta" and (data.get("name", "").lower() == "description" or data.get("property", "").lower() == "og:description"):
            self.description = (data.get("content") or "")[:2000]
        if tag == "a" and data.get("href"):
            self.links.append(data["href"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in {"script", "style", "noscript", "svg"} and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data)
        if not self.skip_depth:
            self.text_parts.append(data)


@dataclass
class CrawledPage:
    url: str
    status: int
    title: str | None
    description: str | None
    excerpt: str
    sha256: str


def crawl(url: str, max_pages: int = 5, fetcher=fetch_public, trace=None) -> list[CrawledPage]:
    if not 1 <= max_pages <= 8:
        raise CrawlError("invalid_page_limit")
    _, host, _ = validate_url(url)
    deadline = time.monotonic() + MAX_SECONDS
    robots_url = urljoin(url, "/robots.txt")
    response = fetcher(robots_url, host, deadline, 128000, trace=trace)
    if response.status not in {200, 404, 410}:
        raise CrawlError("robots_denied" if response.status in {401, 403} else "robots_unavailable")
    robots = RobotFileParser()
    robots.parse(response.body.decode("utf-8", errors="replace").splitlines() if response.status == 200 else [])
    delay = robots.crawl_delay(USER_AGENT) or robots.crawl_delay("*") or 0
    if delay > 5:
        raise CrawlError("robots_delay_exceeds_budget")
    queue, visited, result, total = [(url, 0)], set(), [], len(response.body)
    last_request = 0.0
    while queue and len(result) < max_pages:
        page_url, depth = queue.pop(0)
        parts = urlsplit(page_url)
        page_url = urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "", ""))
        if page_url in visited or not same_site(page_url, host) or not robots.can_fetch(USER_AGENT, page_url):
            continue
        visited.add(page_url)
        # Respect crawl-delay without holding a database transaction.
        wait = max(0, delay - (time.monotonic() - last_request))
        if time.monotonic() + wait >= deadline:
            raise CrawlError("time_budget_exhausted")
        if wait:
            time.sleep(wait)
        response = fetcher(page_url, host, deadline, min(MAX_RESPONSE, MAX_TOTAL - total), allowed=lambda target: robots.can_fetch(USER_AGENT, target), trace=trace)
        last_request = time.monotonic()
        if not robots.can_fetch(USER_AGENT, response.url):
            raise CrawlError("redirect_disallowed_by_robots")
        total += len(response.body)
        if total > MAX_TOTAL:
            raise CrawlError("total_byte_budget_exhausted")
        if response.status != 200 or "text/html" not in response.headers.get("content-type", ""):
            continue
        parser = PageParser()
        parser.feed(response.body.decode("utf-8", errors="replace"))
        result.append(CrawledPage(response.url, response.status, " ".join(parser.title_parts).strip()[:300] or None, parser.description, " ".join(" ".join(parser.text_parts).split())[:12000], hashlib.sha256(response.body).hexdigest()))
        if depth < 2:
            for link in parser.links[:100]:
                target = urljoin(response.url, link)
                if len(queue) >= 100:
                    break
                try:
                    if same_site(target.split("#")[0], host):
                        queue.append((target.split("#")[0], depth + 1))
                except CrawlError:
                    continue
    if not result:
        raise CrawlError("no_accessible_html_pages")
    return result
