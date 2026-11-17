"""Public-page collection and optional India-focused discovery; snippets are not evidence."""
from dataclasses import dataclass
from datetime import date
import hashlib
import time
from urllib.parse import urlsplit
import httpx
from app.config import settings
from app.research_models import SourceSeed
from app.services.crawl import crawl, CrawlError, validate_url


@dataclass
class CollectedSource:
    seed: SourceSeed
    url: str
    publisher: str | None
    published_at: date | None
    text: str
    content_sha256: str
    confidence_cap: float
    limitations: list[str]


class BraveDiscovery:
    def __init__(self, key: str, client=None):
        self.key, self.client = key, client

    def discover(self, query: str, trace=None) -> list[SourceSeed]:
        client = self.client or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        status, error = None, None
        started = time.monotonic()
        try:
            response = client.get("https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "country": "IN", "search_lang": "en", "count": 5, "safesearch": "strict"},
                headers={"X-Subscription-Token": self.key})
            status = response.status_code
            if status != 200 or len(response.content) > 500000:
                raise CrawlError("discovery_unavailable")
            hits = response.json().get("web", {}).get("results", [])
            results = []
            for hit in hits[:5]:
                try:
                    validate_url(hit["url"])
                    results.append(SourceSeed(url=hit["url"], area="market", geographic_scope="unknown"))
                except (KeyError, ValueError):
                    continue
            return results
        except (httpx.TransportError, ValueError, AttributeError) as exc:
            error = "discovery_unavailable"
            raise CrawlError(error) from exc
        finally:
            if trace:
                trace({"endpoint": "https://api.search.brave.com/res/v1/web/search", "method": "GET",
                    "response_status": status, "error_code": error, "latency_ms": int((time.monotonic()-started)*1000),
                    "request_sha256": hashlib.sha256(query.encode()).hexdigest()})
            if self.client is None:
                client.close()


def discovery_provider():
    config = settings()
    if not config.allow_search_calls or not config.brave_api_key:
        raise CrawlError("public_search_not_configured")
    return BraveDiscovery(config.brave_api_key.get_secret_value())


def collect(seeds: list[SourceSeed], queries: list[str], *, trace=None, crawler=crawl, discoverer=None):
    limitations, sources = [], []
    candidates = list(seeds)
    if queries:
        try:
            discoverer = discoverer or discovery_provider()
            for query in queries:
                candidates.extend(discoverer.discover(query, trace=trace))
        except CrawlError as exc:
            limitations.append(str(exc))
    seen = set()
    for seed in candidates:
        if len(sources) >= 8 or len(seen) >= 12:
            break
        key = str(seed.url).rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        try:
            pages = crawler(str(seed.url), 1, trace=trace)
            page = pages[0]
            if len(page.excerpt.strip()) < 80:
                raise CrawlError("insufficient_public_text")
            notes = ["Public HTML snapshot only; source claims require human assessment."]
            if seed.kind in {"social", "review", "search"}:
                notes.append("Partial public sample; account analytics, representative sentiment and market size are not available.")
            sources.append(CollectedSource(seed, page.url, urlsplit(page.url).hostname, None,
                page.excerpt[:12000], page.sha256, .85 if seed.kind == "public_web" else .55, notes))
        except (CrawlError, IndexError) as exc:
            limitations.append(urlsplit(str(seed.url)).hostname + ": " + str(exc))
    if len(candidates) > len(seen):
        limitations.append("Source limit reached; remaining candidates were not accessed.")
    return sources, limitations
