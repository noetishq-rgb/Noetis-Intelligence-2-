import time
from uuid import uuid4
import pytest
from app.services import crawl as module
from app.services.crawl import CrawlError, CrawledPage, Response, crawl, fetch_public, public_addresses, validate_url
from app.services.extraction import extract_profile
from app.models import OnboardingInput


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost", "https://user:secret@example.com", "http://example.com:8080", "https://example.com/#fragment", "https://example.com\\evil"])
def test_rejects_unsafe_url_forms(url):
    with pytest.raises(CrawlError):
        validate_url(url)


@pytest.mark.parametrize("host", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "2002:7f00:1::"])
def test_rejects_private_metadata_and_tunnel_addresses(host):
    with pytest.raises(CrawlError):
        public_addresses(host, 443)


def test_mixed_public_and_private_dns_answers_fail_closed(monkeypatch):
    class Resolver:
        def resolve(self, host, kind, **kwargs):
            return ["93.184.216.34"] if kind == "A" else ["::1"]
    monkeypatch.setattr(module.dns.resolver, "Resolver", Resolver)
    with pytest.raises(CrawlError, match="nonpublic_address"):
        public_addresses("example.com", 443)


def test_missing_ipv6_record_does_not_discard_a_public_ipv4_address(monkeypatch):
    class Resolver:
        def resolve(self, host, kind, **kwargs):
            if kind == 'AAAA':
                raise module.dns.resolver.NXDOMAIN()
            return ['93.184.216.34']
    monkeypatch.setattr(module.dns.resolver, 'Resolver', Resolver)
    assert public_addresses('example.com', 443) == ['93.184.216.34']


def test_dns_timeout_remains_fail_closed_and_is_traced(monkeypatch):
    class Resolver:
        def resolve(self, host, kind, **kwargs):
            if kind == 'AAAA':
                raise module.dns.exception.Timeout()
            return ['93.184.216.34']
    monkeypatch.setattr(module.dns.resolver, 'Resolver', Resolver)
    traces=[]
    with pytest.raises(CrawlError, match='dns_failed'):
        fetch_public('https://example.com/?private=query', 'example.com', time.monotonic()+10, trace=traces.append)
    assert traces[0]['error_code']=='dns_failed' and '?' not in traces[0]['endpoint']


def test_redirect_is_checked_before_second_request_and_ip_is_pinned(monkeypatch):
    connections = []
    class Sock:
        def close(self): pass
        def settimeout(self, value): pass
    class Reply:
        status = 302
        def getheaders(self): return [("Location", "/private")]
    class Connection:
        def __init__(self, host, port, timeout): self.sock = None
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Reply()
        def close(self): pass
    monkeypatch.setattr(module, "public_addresses", lambda *args: ["93.184.216.34"])
    monkeypatch.setattr(module.http.client, "HTTPConnection", Connection)
    def connect(address, timeout):
        connections.append(address)
        return Sock()
    monkeypatch.setattr(module.socket, "create_connection", connect)
    with pytest.raises(CrawlError, match="redirect_disallowed"):
        fetch_public("http://example.com/", "example.com", time.monotonic()+10,
                     allowed=lambda url: "/private" not in url)
    assert connections == [("93.184.216.34", 80)]


@pytest.mark.parametrize('declared', [True, False])
@pytest.mark.parametrize('size', [1_200_000, 2_000_000, 2_000_001])
def test_storefront_response_budget_is_enforced_with_or_without_content_length(monkeypatch, declared, size):
    import io

    body = b'x' * size
    class Sock:
        def close(self): pass
        def settimeout(self, value): pass
    class Reply:
        status = 200
        def __init__(self): self.stream = io.BytesIO(body)
        def getheaders(self):
            return [('Content-Length', str(size))] if declared else []
        def read(self, amount): return self.stream.read(amount)
    class Connection:
        def __init__(self, *args, **kwargs): self.sock = None
        def request(self, *args, **kwargs): pass
        def getresponse(self): return Reply()
        def close(self): pass
    monkeypatch.setattr(module, 'public_addresses', lambda *args: ['93.184.216.34'])
    monkeypatch.setattr(module.socket, 'create_connection', lambda *args, **kwargs: Sock())
    monkeypatch.setattr(module.http.client, 'HTTPConnection', Connection)
    if size > module.MAX_RESPONSE:
        with pytest.raises(CrawlError, match='response_too_large'):
            fetch_public('http://example.com/', 'example.com', time.monotonic()+10)
    else:
        assert len(fetch_public('http://example.com/', 'example.com', time.monotonic()+10).body) == size


def test_robots_page_bounds_and_no_script_extraction():
    calls = []
    def fetch(url, host, deadline, limit, allowed=None, trace=None):
        calls.append(url)
        if url.endswith("/robots.txt"):
            return Response(url, 200, {}, b"User-agent: *\nDisallow: /private\n")
        assert not allowed or allowed(url)
        return Response(url, 200, {"content-type": "text/html"}, b'<title>Public</title><meta name="description" content="Known description"><script>SECRET_SCRIPT</script><a href="/private">private</a><a href="/about">about</a>')
    pages = crawl("https://example.com/", 2, fetcher=fetch)
    assert len(pages) == 2
    assert not any(url.endswith("/private") for url in calls)
    assert all("SECRET_SCRIPT" not in page.excerpt for page in pages)
    assert pages[0].description == "Known description"


def test_extraction_leaves_pricing_audiences_and_margins_unknown():
    sid = uuid4()
    page = CrawledPage("https://example.com", 200, "A website", "A description.", "A price appears somewhere: INR 500", "b"*64)
    profile = extract_profile(uuid4(), 1, {"name": "Client supplied name", "website_url": page.url}, [(page,sid)], uuid4())
    assert profile.audience_description is None
    assert profile.budget_range is None
    assert profile.pricing_margin_notes is None
    assert profile.products_services == []
    assert profile.business_description.value == "A description."
    assert all(question.field in OnboardingInput.model_fields for question in profile.onboarding_questions)
