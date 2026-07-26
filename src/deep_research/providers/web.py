from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import html
import http.client
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Mapping

from ..cache import FileCache
from .base import ResourceLimitExceededError
from ..schemas import Page, Query, SearchResult


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
)
MAX_FETCH_BYTES = 2_500_000
MAX_PDF_FETCH_BYTES = 15_000_000
MAX_SEARCH_BYTES = 2_000_000
BRAVE_SEARCH_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
BRAVE_MAX_RESULTS = 20
OPENALEX_WORKS_ENDPOINT = "https://api.openalex.org/works"
OPENALEX_MAX_RESULTS = 20
OPENALEX_MIN_INTERVAL_SECONDS = 0.15
OPENALEX_SELECT_FIELDS = (
    "id,doi,display_name,publication_year,cited_by_count,type,open_access,"
    "primary_location,best_oa_location,abstract_inverted_index"
)
MAX_PDF_OUTPUT_BYTES = 400_000
NETWORK_MAX_ATTEMPTS = 4
NETWORK_CONCURRENCY = 2
ARXIV_MIN_INTERVAL_SECONDS = 3.0
_PINNED_ADDRESSES_ATTR = "_deep_research_pinned_addresses"
_RFC2544_PROXY_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
_BLOCKED_PROXY_HOST_SUFFIXES = (
    ".home.arpa",
    ".internal",
    ".lan",
    ".local",
    ".localhost",
)
_PAPER_HOSTS = {
    "aclanthology.org",
    "arxiv.org",
    "dl.acm.org",
    "ieeexplore.ieee.org",
    "openreview.net",
    "pubmed.ncbi.nlm.nih.gov",
}
_OPEN_RESEARCH_HOSTS = {
    "aclanthology.org",
    "arxiv.org",
    "openaccess.thecvf.com",
    "openreview.net",
    "pmc.ncbi.nlm.nih.gov",
    "pubmed.ncbi.nlm.nih.gov",
}
_RESTRICTED_RESEARCH_HOSTS = {
    "link.springer.com",
    "researchgate.net",
    "sciencedirect.com",
}
_ARXIV_ATOM_NAMESPACE = "http://www.w3.org/2005/Atom"
_ARXIV_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "can",
    "does",
    "for",
    "from",
    "how",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "under",
    "why",
    "with",
}
# Research-agent prompts often carry procedural words such as "evidence" or
# "counterexample" after the actual topic. OpenAlex treats them as ordinary
# search terms, which can overwhelm a narrow academic subject such as ReID.
_OPENALEX_QUERY_NOISE_TERMS = {
    "abstract",
    "advance",
    "advances",
    "analysis",
    "art",
    "assumptions",
    "bias",
    "caveats",
    "competing",
    "context",
    "counterexample",
    "critical",
    "does",
    "empirical",
    "error",
    "evidence",
    "explanation",
    "failure",
    "findings",
    "latest",
    "leaderboards",
    "limitations",
    "method",
    "methods",
    "model",
    "models",
    "negative",
    "noise",
    "object",
    "paper",
    "papers",
    "recent",
    "refer",
    "replication",
    "report",
    "representation",
    "results",
    "result",
    "review",
    "route",
    "sensitivity",
    "state-of-the-art",
    "state",
    "study",
    "target",
    "technical",
    "unreliable",
    "versus",
    "what",
    "which",
}
_OPENALEX_REID_MARKER = re.compile(
    r"(?i)(?<![a-z0-9])re(?:[\s_-]?id|[\s-]?identification)(?![a-z0-9])"
)
_REFERENCE_HOSTS = {"britannica.com", "wikipedia.org"}
_GOVERNMENT_SUFFIXES = (
    ".gov",
    ".gov.cn",
    ".gov.uk",
    ".go.jp",
    ".gouv.fr",
)


class DuckDuckGoSearchProvider:
    """Keyless web search with an official arXiv metadata fallback."""

    supports_ssrf_guard = True

    def __init__(
        self,
        cache: FileCache,
        *,
        allow_rfc2544_proxy_fake_ip: bool = False,
    ) -> None:
        self.cache = cache
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)
        self._network_slots = threading.BoundedSemaphore(NETWORK_CONCURRENCY)
        self._arxiv_rate_lock = threading.Lock()
        self._last_arxiv_request_at = 0.0

    def validate_public_url(self, url: str) -> None:
        _validate_public_url(
            url,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )

    def _pin(self, request: urllib.request.Request) -> urllib.request.Request:
        if not self.allow_rfc2544_proxy_fake_ip:
            return _pin_request(request)
        return _pin_request(request, allow_rfc2544_proxy_fake_ip=True)

    def _opener(
        self,
        *,
        allow_redirects: bool = True,
    ) -> urllib.request.OpenerDirector:
        return _public_opener(
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
            allow_redirects=allow_redirects,
        )

    async def search(self, query: Query, limit: int = 5) -> list[SearchResult]:
        key = f"ddg-arxiv-ranked-v3|{query.text}|{limit}"
        cached = self.cache.get_json("search", key)
        if cached is not None:
            return [SearchResult(**item) for item in cached]
        results = await asyncio.to_thread(self._search_sync, query.text, limit)
        if results:
            self.cache.put_json(
                "search",
                key,
                [
                    {
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "source_type": item.source_type,
                    }
                    for item in results
                ],
            )
        return results

    async def fetch(self, result: SearchResult) -> Page:
        cached = self.cache.get_json("pages", result.url)
        if cached is not None:
            page = Page(**cached)
            page.cache_hit = True
            page.provenance_signals = _unique(
                [
                    *page.provenance_signals,
                    _source_classification_signal(page.url, page.source_type),
                ]
            )
            return page
        page = await asyncio.to_thread(self._fetch_sync, result)
        self.cache.put_json(
            "pages",
            result.url,
            {
                "url": page.url,
                "title": page.title,
                "text": page.text,
                "source_type": page.source_type,
                "content_hash": page.content_hash,
                "content_hash_scope": page.content_hash_scope,
                "fetched_at": page.fetched_at,
                "http_status": page.http_status,
                "content_type": page.content_type,
                "parser_version": page.parser_version,
                "bytes_read": page.bytes_read,
                "cache_hit": False,
                "canonical_url": page.canonical_url,
                "publisher_name": page.publisher_name,
                "publisher_url": page.publisher_url,
                "author_names": page.author_names,
                "site_name": page.site_name,
                "upstream_urls": page.upstream_urls,
                "provenance_signals": page.provenance_signals,
            },
        )
        return page

    def _search_sync(self, query: str, limit: int) -> list[SearchResult]:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        duckduckgo_error: Exception | None = None
        try:
            raw, _final_url, _status, _content_type = self._read_url_sync(
                url,
                default_byte_limit=MAX_SEARCH_BYTES,
            )
            if len(raw) > MAX_SEARCH_BYTES:
                raise ResourceLimitExceededError(
                    "resource_limit_exceeded: search response exceeded the 2 MB limit"
                )
            body = raw.decode("utf-8", errors="replace")
            if not _is_duckduckgo_challenge(body):
                results = _parse_duckduckgo_results(body, limit)
                if results:
                    return results
        except ResourceLimitExceededError:
            raise
        except Exception as error:
            duckduckgo_error = error

        try:
            return self._search_arxiv_sync(query, limit)
        except Exception as arxiv_error:
            if duckduckgo_error is not None:
                raise RuntimeError(
                    "public search providers failed; "
                    f"DuckDuckGo: {duckduckgo_error}; arXiv: {arxiv_error}"
                ) from arxiv_error
            raise

    def _search_arxiv_sync(self, query: str, limit: int) -> list[SearchResult]:
        expression = _arxiv_search_expression(query)
        if not expression:
            return []
        url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(
            {
                "search_query": expression,
                "start": 0,
                "max_results": max(limit * 2, 6),
                "sortBy": "relevance",
                "sortOrder": "descending",
            }
        )
        with self._arxiv_rate_lock:
            delay = (
                self._last_arxiv_request_at
                + ARXIV_MIN_INTERVAL_SECONDS
                - time.monotonic()
            )
            if delay > 0:
                time.sleep(delay)
            self._last_arxiv_request_at = time.monotonic()
            raw, final_url, _status, _content_type = self._read_url_sync(
                url,
                default_byte_limit=MAX_SEARCH_BYTES,
            )
        if len(raw) > MAX_SEARCH_BYTES:
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: arXiv response exceeded the 2 MB limit"
            )
        try:
            return _parse_arxiv_results(raw, limit)
        except ET.ParseError as error:
            raise RuntimeError(f"Invalid arXiv Atom response: {final_url}") from error

    def _fetch_sync(self, result: SearchResult) -> Page:
        raw, final_url, http_status, content_type = self._read_url_sync(
            result.url,
            default_byte_limit=MAX_FETCH_BYTES,
            pdf_byte_limit=MAX_PDF_FETCH_BYTES,
        )
        is_pdf = content_type == "application/pdf" or final_url.lower().endswith(".pdf")
        fetch_limit = MAX_PDF_FETCH_BYTES if is_pdf else MAX_FETCH_BYTES
        fallback_signal = ""
        if len(raw) > fetch_limit and is_pdf:
            abstract_url = _arxiv_abstract_url(final_url)
            if abstract_url:
                raw, final_url, http_status, content_type = self._read_url_sync(
                    abstract_url,
                    default_byte_limit=MAX_FETCH_BYTES,
                )
                is_pdf = False
                fetch_limit = MAX_FETCH_BYTES
                fallback_signal = (
                    "fetch-fallback-v1: official arXiv abstract page used because "
                    "the PDF exceeded the 15 MB transport limit"
                )
        if len(raw) > fetch_limit:
            limit_label = "15 MB PDF" if is_pdf else "2.5 MB"
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: "
                f"response exceeded the {limit_label} limit: {final_url}"
            )
        if content_type not in {
            "text/html",
            "application/xhtml+xml",
            "text/plain",
            "application/pdf",
        }:
            raise RuntimeError(f"Unsupported content type {content_type}: {final_url}")
        if is_pdf:
            text = _extract_pdf(raw)
            digest = hashlib.sha256(text.encode()).hexdigest()
            source_type = _classify_source(final_url)
            return Page(
                url=_canonical_url(final_url),
                title=result.title,
                text=text[:100_000],
                source_type=source_type,
                content_hash=digest,
                content_hash_scope="full_extracted_text",
                fetched_at=datetime.now(UTC).isoformat(),
                http_status=http_status,
                content_type=content_type,
                parser_version="pdftotext-layout-v1",
                bytes_read=len(raw),
                provenance_signals=[
                    _source_classification_signal(final_url, source_type)
                ],
            )
        decoded = raw.decode("utf-8", errors="replace")
        provenance = _extract_provenance_metadata(decoded, final_url)
        extractor = _TextExtractor()
        extractor.feed(decoded)
        text = extractor.text()
        if len(text) < 80:
            raise RuntimeError(f"Page contained too little extractable text: {final_url}")
        title = extractor.title or result.title
        digest = hashlib.sha256(text.encode()).hexdigest()
        source_type = _classify_source(final_url)
        return Page(
            url=_canonical_url(final_url),
            title=title,
            text=text[:100_000],
            source_type=source_type,
            content_hash=digest,
            content_hash_scope="full_extracted_text",
            fetched_at=datetime.now(UTC).isoformat(),
            http_status=http_status,
            content_type=content_type,
            parser_version="stdlib-htmlparser-v1",
            bytes_read=len(raw),
            canonical_url=provenance["canonical_url"],
            publisher_name=provenance["publisher_name"],
            publisher_url=provenance["publisher_url"],
            author_names=provenance["author_names"],
            site_name=provenance["site_name"],
            upstream_urls=provenance["upstream_urls"],
            provenance_signals=_unique(
                [
                    *provenance["provenance_signals"],
                    fallback_signal,
                    _source_classification_signal(final_url, source_type),
                ]
            ),
        )

    def _read_url_sync(
        self,
        url: str,
        *,
        default_byte_limit: int,
        pdf_byte_limit: int | None = None,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[bytes, str, int, str]:
        last_error: Exception | None = None
        for attempt in range(1, NETWORK_MAX_ATTEMPTS + 1):
            request_headers = {"User-Agent": USER_AGENT}
            if headers:
                request_headers.update(headers)
            request = self._pin(urllib.request.Request(url, headers=request_headers))
            try:
                with self._network_slots:
                    with self._opener(allow_redirects=allow_redirects).open(
                        request, timeout=30
                    ) as response:
                        content_type = response.headers.get_content_type()
                        final_url = response.geturl()
                        status = response.status
                        byte_limit = default_byte_limit
                        if pdf_byte_limit is not None and (
                            content_type == "application/pdf"
                            or final_url.lower().endswith(".pdf")
                        ):
                            byte_limit = pdf_byte_limit
                        raw = response.read(byte_limit + 1)
                return raw, final_url, status, content_type
            except Exception as error:
                last_error = error
                if attempt >= NETWORK_MAX_ATTEMPTS or not _is_transient_network_error(
                    error
                ):
                    raise
                time.sleep(0.25 * (2 ** (attempt - 1)))
        assert last_error is not None
        raise last_error


class BraveSearchProvider:
    """Brave Web Search discovery with the existing hardened page-fetch path."""

    supports_ssrf_guard = True

    def __init__(
        self,
        cache: FileCache,
        *,
        api_key: str,
        allow_rfc2544_proxy_fake_ip: bool = False,
    ) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError(
                "DR_BRAVE_API_KEY is empty. Fill it in the project .env file."
            )
        self.cache = cache
        self.api_key = key
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)
        self._fetch_provider = DuckDuckGoSearchProvider(
            cache,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )

    def validate_public_url(self, url: str) -> None:
        self._fetch_provider.validate_public_url(url)

    async def search(self, query: Query, limit: int = 5) -> list[SearchResult]:
        bounded_limit = max(1, min(BRAVE_MAX_RESULTS, int(limit)))
        key = f"brave-web-v1|{query.text}|{bounded_limit}"
        cached = self.cache.get_json("search", key)
        if cached is not None:
            return [SearchResult(**item) for item in cached]
        results = await asyncio.to_thread(
            self._search_sync,
            query.text,
            bounded_limit,
        )
        if results:
            self.cache.put_json(
                "search",
                key,
                [
                    {
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "source_type": item.source_type,
                    }
                    for item in results
                ],
            )
        return results

    async def fetch(self, result: SearchResult) -> Page:
        return await self._fetch_provider.fetch(result)

    def _read_url_sync(
        self,
        url: str,
        *,
        default_byte_limit: int,
        headers: Mapping[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> tuple[bytes, str, int, str]:
        return self._fetch_provider._read_url_sync(
            url,
            default_byte_limit=default_byte_limit,
            headers=headers,
            allow_redirects=allow_redirects,
        )

    def _search_sync(self, query: str, limit: int) -> list[SearchResult]:
        bounded_limit = max(1, min(BRAVE_MAX_RESULTS, int(limit)))
        # Ask for a small ranked buffer, then apply the same local source
        # preference used by the DuckDuckGo provider before returning results.
        request_count = min(BRAVE_MAX_RESULTS, max(bounded_limit, bounded_limit * 2))
        url = BRAVE_SEARCH_ENDPOINT + "?" + urllib.parse.urlencode(
            {
                "q": query,
                "count": request_count,
                "safesearch": "moderate",
            }
        )
        try:
            raw, final_url, _status, _content_type = self._read_url_sync(
                url,
                default_byte_limit=MAX_SEARCH_BYTES,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self.api_key,
                },
                # The credential must never follow a redirect to another host.
                allow_redirects=False,
            )
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise RuntimeError(
                    "Brave Search rejected DR_BRAVE_API_KEY; check the key and plan."
                ) from error
            raise
        if len(raw) > MAX_SEARCH_BYTES:
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: Brave search response exceeded the 2 MB limit"
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Brave Search returned invalid JSON: {final_url}"
            ) from error
        if isinstance(payload, dict) and payload.get("type") == "ErrorResponse":
            raise RuntimeError(
                "Brave Search returned an API error; check DR_BRAVE_API_KEY and plan."
            )
        return _parse_brave_results(payload, bounded_limit)


class OpenAlexSearchProvider:
    """No-key scholarly discovery backed by OpenAlex and safe source fetching."""

    supports_ssrf_guard = True

    def __init__(
        self,
        cache: FileCache,
        *,
        allow_rfc2544_proxy_fake_ip: bool = False,
    ) -> None:
        self.cache = cache
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)
        self._fetch_provider = DuckDuckGoSearchProvider(
            cache,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0

    def validate_public_url(self, url: str) -> None:
        self._fetch_provider.validate_public_url(url)

    async def search(self, query: Query, limit: int = 5) -> list[SearchResult]:
        bounded_limit = max(1, min(OPENALEX_MAX_RESULTS, int(limit)))
        # v5 applies the same topic guard to the arXiv outage fallback. Do not
        # reuse responses gathered before fallback relevance filtering.
        key = f"openalex-works-v5|{query.text}|{bounded_limit}"
        cached = self.cache.get_json("search", key)
        if cached is not None:
            return [SearchResult(**item) for item in cached]
        results = await asyncio.to_thread(
            self._search_sync,
            query.text,
            bounded_limit,
        )
        if results:
            self.cache.put_json(
                "search",
                key,
                [
                    {
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "source_type": item.source_type,
                    }
                    for item in results
                ],
            )
        return results

    async def fetch(self, result: SearchResult) -> Page:
        return await self._fetch_provider.fetch(result)

    def _read_url_sync(
        self,
        url: str,
        *,
        default_byte_limit: int,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[bytes, str, int, str]:
        return self._fetch_provider._read_url_sync(
            url,
            default_byte_limit=default_byte_limit,
            headers=headers,
        )

    def _search_sync(self, query: str, limit: int) -> list[SearchResult]:
        bounded_limit = max(1, min(OPENALEX_MAX_RESULTS, int(limit)))
        terms = _openalex_search_terms(query)
        if not terms:
            return []
        # Relevance filtering needs a larger candidate pool than the number of
        # pages we will actually fetch. This request is metadata-only and does
        # not increase the downstream page-fetch budget.
        request_count = min(
            OPENALEX_MAX_RESULTS,
            max(10, bounded_limit, bounded_limit * 4),
        )
        parameters: dict[str, str | int] = {
            "search": terms,
            "per-page": request_count,
            "select": OPENALEX_SELECT_FIELDS,
        }
        recent_floor = _openalex_recent_publication_floor(query)
        if recent_floor is not None:
            parameters["filter"] = f"from_publication_date:{recent_floor}-01-01"
            parameters["sort"] = "publication_date:desc"
        url = OPENALEX_WORKS_ENDPOINT + "?" + urllib.parse.urlencode(parameters)
        openalex_error: Exception | None = None
        try:
            with self._rate_lock:
                delay = (
                    self._last_request_at
                    + OPENALEX_MIN_INTERVAL_SECONDS
                    - time.monotonic()
                )
                if delay > 0:
                    time.sleep(delay)
                self._last_request_at = time.monotonic()
                raw, final_url, _status, _content_type = self._read_url_sync(
                    url,
                    default_byte_limit=MAX_SEARCH_BYTES,
                    headers={"Accept": "application/json"},
                )
            if len(raw) > MAX_SEARCH_BYTES:
                raise ResourceLimitExceededError(
                    "resource_limit_exceeded: OpenAlex response exceeded the 2 MB limit"
                )
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"OpenAlex returned invalid JSON: {final_url}"
                ) from error
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError("OpenAlex returned an API error")
            results = _parse_openalex_results(
                payload,
                bounded_limit,
                query=query,
            )
            if results:
                return results
        except ResourceLimitExceededError:
            raise
        except Exception as error:
            openalex_error = error

        # Keep a no-key, official scholarly fallback for an outage or a query
        # that OpenAlex cannot map to a work. This never invokes DuckDuckGo HTML.
        try:
            fallback_results = self._fetch_provider._search_arxiv_sync(
                terms,
                request_count,
            )
            return _filter_openalex_fallback_results(
                fallback_results,
                query,
                bounded_limit,
            )
        except Exception as arxiv_error:
            if openalex_error is not None:
                raise RuntimeError(
                    "public scholarly search providers failed; "
                    f"OpenAlex: {openalex_error}; arXiv: {arxiv_error}"
                ) from arxiv_error
            raise


class _DuckDuckGoParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[str, str, str]] = []
        self._active_url = ""
        self._title_parts: list[str] = []
        self._snippet_parts: list[str] = []
        self._in_title = False
        self._in_snippet = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = values.get("class", "") or ""
        if tag == "a" and "result__a" in classes:
            self._flush()
            self._active_url = values.get("href", "") or ""
            self._in_title = True
        elif "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._in_title:
            self._in_title = False
        if tag in {"a", "div"} and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_snippet:
            self._snippet_parts.append(data)

    def close(self) -> None:
        super().close()
        self._flush()

    def _flush(self) -> None:
        if self._active_url and self._title_parts:
            self.results.append(
                (
                    _clean(" ".join(self._title_parts)),
                    self._active_url,
                    _clean(" ".join(self._snippet_parts)),
                )
            )
        self._active_url = ""
        self._title_parts = []
        self._snippet_parts = []


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._in_title = False
        self.title = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in {"p", "br", "li", "h1", "h2", "h3", "article", "section"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer"} and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
        self._parts.append(data)

    def text(self) -> str:
        lines = [_clean(line) for line in "".join(self._parts).splitlines()]
        return "\n".join(line for line in lines if len(line) > 1)


class _ProvenanceExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.canonical = ""
        self.publisher_name = ""
        self.publisher_url = ""
        self.site_name = ""
        self.authors: list[str] = []
        self.upstream: list[str] = []
        self.signals: list[str] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []
        self.jsonld_blocks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "link":
            rels = {item.casefold() for item in values.get("rel", "").split()}
            href = values.get("href", "").strip()
            if "canonical" in rels and href and not self.canonical:
                self.canonical = href
                self.signals.append("html:rel-canonical")
            if rels & {"syndication-source", "original-source"} and href:
                self.upstream.append(href)
                self.signals.append("html:upstream-link")
        elif tag == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            content = values.get("content", "").strip()
            if not content:
                return
            if key == "og:site_name" and not self.site_name:
                self.site_name = content
                self.signals.append("meta:og-site-name")
            elif key in {"author", "citation_author", "byl"}:
                self.authors.append(content)
                self.signals.append("meta:author")
            elif key in {"publisher", "citation_publisher"} and not self.publisher_name:
                self.publisher_name = content
                self.signals.append("meta:publisher")
            elif key == "article:publisher" and not self.publisher_url:
                self.publisher_url = content
                self.signals.append("meta:article-publisher")
            elif key in {"original-source", "syndication-source"}:
                self.upstream.append(content)
                self.signals.append("meta:upstream")
        elif tag == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            block = "".join(self._jsonld_parts).strip()
            if block:
                self.jsonld_blocks.append(block)
            self._jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_parts.append(data)


def _extract_provenance_metadata(document: str, final_url: str) -> dict[str, object]:
    """Extract self-declared provenance without treating it as verified ownership."""
    parser = _ProvenanceExtractor()
    parser.feed(document)
    parser.close()
    for block in parser.jsonld_blocks:
        try:
            value = json.loads(block)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        for node in _jsonld_nodes(value):
            publisher = node.get("publisher")
            if isinstance(publisher, dict):
                if not parser.publisher_name and isinstance(publisher.get("name"), str):
                    parser.publisher_name = publisher["name"].strip()
                if not parser.publisher_url:
                    candidate = publisher.get("url") or publisher.get("@id")
                    if isinstance(candidate, str):
                        parser.publisher_url = candidate.strip()
                parser.signals.append("jsonld:publisher")
            elif isinstance(publisher, str) and not parser.publisher_name:
                parser.publisher_name = publisher.strip()
                parser.signals.append("jsonld:publisher")
            authors = node.get("author")
            if not isinstance(authors, list):
                authors = [authors] if authors else []
            for author in authors:
                name = author.get("name") if isinstance(author, dict) else author
                if isinstance(name, str) and name.strip():
                    parser.authors.append(name.strip())
                    parser.signals.append("jsonld:author")
            for key in ("isBasedOn", "isBasedOnUrl", "citation"):
                _collect_jsonld_urls(node.get(key), parser.upstream)
                if node.get(key):
                    parser.signals.append(f"jsonld:{key}")

    canonical = _metadata_url(parser.canonical, final_url)
    publisher_url = _metadata_url(parser.publisher_url, final_url)
    upstream_urls = _unique(
        value
        for raw in parser.upstream
        if (value := _metadata_url(raw, final_url))
    )
    return {
        "canonical_url": canonical,
        "publisher_name": _clean(parser.publisher_name),
        "publisher_url": publisher_url,
        "author_names": _unique(_clean(value) for value in parser.authors if _clean(value)),
        "site_name": _clean(parser.site_name),
        "upstream_urls": upstream_urls,
        "provenance_signals": _unique(parser.signals),
    }


def _jsonld_nodes(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _jsonld_nodes(item)
    elif isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if graph is not None:
            yield from _jsonld_nodes(graph)


def _collect_jsonld_urls(value: object, output: list[str]) -> None:
    if isinstance(value, str):
        output.append(value)
    elif isinstance(value, list):
        for item in value:
            _collect_jsonld_urls(item, output)
    elif isinstance(value, dict):
        candidate = value.get("url") or value.get("@id")
        if isinstance(candidate, str):
            output.append(candidate)


def _metadata_url(value: str, base_url: str) -> str:
    if not value:
        return ""
    joined = urllib.parse.urljoin(base_url, html.unescape(value.strip()))
    return _canonical_url(joined)


def _unique(values) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _is_duckduckgo_challenge(document: str) -> bool:
    folded = document.casefold()
    return any(
        marker in folded
        for marker in (
            "anomaly-modal",
            "bots use duckduckgo",
            "challenge-form",
            "/anomaly.js",
        )
    )


def _parse_duckduckgo_results(document: str, limit: int) -> list[SearchResult]:
    parser = _DuckDuckGoParser()
    parser.feed(document)
    parser.close()
    ranked_results: list[tuple[int, int, SearchResult]] = []
    seen: set[str] = set()
    for discovery_rank, (title, raw_url, snippet) in enumerate(parser.results):
        resolved = _resolve_ddg_url(raw_url)
        canonical = _canonical_url(resolved)
        if not canonical or canonical in seen:
            continue
        seen.add(canonical)
        result = SearchResult(
            title=title,
            url=canonical,
            snippet=snippet,
            source_type=_classify_source(canonical),
        )
        ranked_results.append(
            (_search_result_fetch_priority(result), discovery_rank, result)
        )
    ranked_results.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked_results[:limit]]


def _parse_brave_results(payload: object, limit: int) -> list[SearchResult]:
    if not isinstance(payload, dict):
        raise RuntimeError("Brave Search response must be a JSON object")
    web = payload.get("web")
    if web is None:
        return []
    if not isinstance(web, dict):
        raise RuntimeError("Brave Search response contains an invalid web result set")
    records = web.get("results", [])
    if not isinstance(records, list):
        raise RuntimeError("Brave Search response contains an invalid results list")

    ranked_results: list[tuple[int, int, SearchResult]] = []
    seen: set[str] = set()
    for discovery_rank, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        raw_url = record.get("url")
        if not isinstance(raw_url, str):
            continue
        canonical = _canonical_url(raw_url)
        if not canonical or canonical in seen:
            continue
        title = _clean(str(record.get("title") or "")) or canonical
        description = _clean(str(record.get("description") or ""))
        extra_snippets = record.get("extra_snippets", [])
        if isinstance(extra_snippets, list):
            extras = [
                _clean(item)
                for item in extra_snippets
                if isinstance(item, str) and _clean(item)
            ]
        else:
            extras = []
        seen.add(canonical)
        result = SearchResult(
            title=title,
            url=canonical,
            snippet=_clean(" ".join([description, *extras]))[:1200],
            source_type=_classify_source(canonical),
        )
        ranked_results.append(
            (_search_result_fetch_priority(result), discovery_rank, result)
        )
    ranked_results.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked_results[: max(1, int(limit))]]


def _openalex_search_terms(query: str) -> str:
    text = _normalize_openalex_query(query)
    if not text:
        return ""
    tokens = _openalex_query_tokens(text)
    if not tokens:
        # Preserve non-Latin technical queries rather than discarding them
        # because the compact English-token heuristic has no useful signal.
        return text[:500]

    folded_tokens = {token.casefold() for token in tokens}
    has_reid = _has_openalex_reid_marker(text)
    anchors: list[str] = []
    if has_reid:
        # Put the subject before broad method terms. OpenAlex otherwise tends
        # to return generic NLP or vision-language surveys for ReID queries.
        if "person" in folded_tokens:
            anchors.append("person re-identification")
        elif "vehicle" in folded_tokens:
            anchors.append("vehicle re-identification")
        else:
            anchors.append("re-identification")

    terms: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        folded = token.casefold()
        if (
            folded in _ARXIV_STOP_WORDS
            or folded in _OPENALEX_QUERY_NOISE_TERMS
            or folded in seen
            or (has_reid and folded in {"person", "vehicle", "re-identification"})
        ):
            continue
        seen.add(folded)
        terms.append(token)
    selected = [*anchors, *terms]
    if selected:
        return " ".join(selected[:12])
    return text[:500]


def _normalize_openalex_query(query: str) -> str:
    text = _clean(query)
    # ``\b`` is not reliable next to CJK text because both sides may be
    # Unicode word characters. The explicit ASCII boundary keeps `ReID领域`
    # searchable without rewriting unrelated identifiers.
    return _OPENALEX_REID_MARKER.sub("re-identification", text)


def _openalex_query_tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+(?:[-+][A-Za-z0-9]+)*", text)


def _has_openalex_reid_marker(text: str) -> bool:
    return bool(_OPENALEX_REID_MARKER.search(text))


def _openalex_recent_publication_floor(query: str) -> int | None:
    """Return a bounded recent-work window only when the question asks for it."""
    text = _normalize_openalex_query(query).casefold()
    if any(
        marker in text
        for marker in (
            "latest",
            "recent",
            "current",
            "present",
            "最新",
            "近期",
            "当前",
            "进展",
        )
    ):
        return max(2018, datetime.now(UTC).year - 4)
    years = [
        int(value)
        for value in re.findall(r"(?<!pre-)\b(20\d{2})\b", text)
    ]
    if years:
        # Keep one preceding year for papers published near a requested
        # boundary, while ruling out unrelated historical high-citation hits.
        return max(2018, min(years) - 1)
    return None


def _openalex_result_relevance(
    record: dict[str, object],
    query: str,
) -> int | None:
    """Score a work against the user topic before spending a fetch slot.

    OpenAlex's broad full-text search is useful for recall, but a multi-agent
    research query also contains workflow language. A ReID marker is strong
    enough to require the returned title or abstract to mention the same
    subject; doing this locally prevents unrelated but highly cited surveys
    from consuming the evidence budget.
    """
    title = _clean(str(record.get("display_name") or ""))
    abstract = _openalex_abstract(record.get("abstract_inverted_index"))
    year = record.get("publication_year")
    return _openalex_text_relevance(
        title,
        abstract,
        query,
        publication_year=(
            year if isinstance(year, int) and not isinstance(year, bool) else None
        ),
    )


def _openalex_text_relevance(
    title: str,
    abstract: str,
    query: str,
    *,
    publication_year: int | None = None,
) -> int | None:
    """Score any scholarly title/abstract pair against the research topic."""
    normalized_query = _normalize_openalex_query(query)
    title = _clean(title)
    abstract = _clean(abstract)
    title_folded = title.casefold()
    abstract_folded = abstract.casefold()
    searchable = f"{title_folded} {abstract_folded}"
    requires_reid = _has_openalex_reid_marker(normalized_query)
    if requires_reid and not _has_openalex_reid_marker(searchable):
        return None

    score = 0
    if requires_reid:
        score += 24
    query_tokens = _openalex_query_tokens(normalized_query)
    query_folds = {
        token.casefold()
        for token in query_tokens
        if (
            not token.isdigit()
            and token.casefold() not in _ARXIV_STOP_WORDS
            and token.casefold() not in _OPENALEX_QUERY_NOISE_TERMS
            and token.casefold()
            not in {
                "person",
                "vehicle",
                "re-identification",
                "reid",
            }
        )
    }
    for token in query_folds:
        if token in title_folded:
            score += 5
        elif token in abstract_folded:
            score += 2

    # A specific subject in the query is a useful ranking signal but should
    # not be a hard filter: some person-ReID papers omit "person" in their
    # title while their abstract uses it.
    for subject in ("person", "vehicle"):
        if subject in {token.casefold() for token in query_tokens}:
            if subject in title_folded:
                score += 4
            elif subject in abstract_folded:
                score += 2

    query_folded = normalized_query.casefold()
    if publication_year is not None and any(
        term in query_folded
        for term in ("latest", "recent", "2023", "2024", "2025", "2026")
    ):
        score += max(0, min(6, publication_year - 2020))
    return score


def _filter_openalex_fallback_results(
    results: list[SearchResult],
    query: str,
    limit: int,
) -> list[SearchResult]:
    """Apply the OpenAlex domain guard when arXiv is used as a fallback."""
    ranked: list[tuple[int, int, int, SearchResult]] = []
    for discovery_rank, result in enumerate(results):
        relevance = _openalex_text_relevance(result.title, result.snippet, query)
        if relevance is None:
            continue
        ranked.append(
            (
                -relevance,
                _search_result_fetch_priority(result),
                discovery_rank,
                result,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked[: max(1, int(limit))]]


def _openalex_result_url(record: dict[str, object]) -> str:
    candidates: list[object] = []
    for field in ("best_oa_location", "primary_location"):
        location = record.get(field)
        if isinstance(location, dict):
            candidates.extend(
                [location.get("landing_page_url"), location.get("pdf_url")]
            )
    open_access = record.get("open_access")
    if isinstance(open_access, dict):
        candidates.append(open_access.get("oa_url"))
    candidates.append(record.get("doi"))
    for candidate in candidates:
        if isinstance(candidate, str):
            if candidate.startswith("http://arxiv.org/"):
                candidate = "https://arxiv.org/" + candidate.removeprefix(
                    "http://arxiv.org/"
                )
            canonical = _canonical_url(candidate)
            if canonical:
                return canonical
    return ""


def _openalex_abstract(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    positioned: dict[int, str] = {}
    for token, positions in value.items():
        if not isinstance(token, str) or not isinstance(positions, list):
            continue
        for position in positions:
            if (
                isinstance(position, int)
                and not isinstance(position, bool)
                and 0 <= position < 4_000
                and position not in positioned
            ):
                positioned[position] = token
    return _clean(" ".join(positioned[index] for index in sorted(positioned)))[:1200]


def _parse_openalex_results(
    payload: object,
    limit: int,
    *,
    query: str = "",
) -> list[SearchResult]:
    if not isinstance(payload, dict):
        raise RuntimeError("OpenAlex response must be a JSON object")
    records = payload.get("results", [])
    if not isinstance(records, list):
        raise RuntimeError("OpenAlex response contains an invalid results list")

    ranked_results: list[tuple[int, int, int, SearchResult]] = []
    seen: set[str] = set()
    for discovery_rank, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        canonical = _openalex_result_url(record)
        if not canonical or canonical in seen:
            continue
        relevance = _openalex_result_relevance(record, query)
        if relevance is None:
            continue
        title = _clean(str(record.get("display_name") or "")) or canonical
        abstract = _openalex_abstract(record.get("abstract_inverted_index"))
        details: list[str] = []
        year = record.get("publication_year")
        if isinstance(year, int) and not isinstance(year, bool):
            details.append(str(year))
        cited_by = record.get("cited_by_count")
        if isinstance(cited_by, int) and not isinstance(cited_by, bool):
            details.append(f"cited by {cited_by}")
        prefix = "OpenAlex metadata"
        if details:
            prefix += f" ({'; '.join(details)})"
        snippet = f"{prefix}: {abstract}" if abstract else prefix
        seen.add(canonical)
        result = SearchResult(
            title=title,
            url=canonical,
            snippet=snippet[:1200],
            # OpenAlex records represent scholarly works even when their landing
            # URL is a DOI resolver or a repository rather than a paper host.
            source_type="paper",
        )
        ranked_results.append(
            (
                -relevance,
                _search_result_fetch_priority(result),
                discovery_rank,
                result,
            )
        )
    ranked_results.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in ranked_results[: max(1, int(limit))]]


def _arxiv_search_expression(query: str) -> str:
    terms: list[str] = []
    seen: set[str] = set()
    for token in re.findall(r"[A-Za-z0-9]+(?:[-+][A-Za-z0-9]+)*", query):
        folded = token.casefold()
        if folded in _ARXIV_STOP_WORDS or folded in seen:
            continue
        seen.add(folded)
        terms.append(token)
    if not terms:
        return ""
    anchor = next((term for term in terms if any(char.isdigit() for char in term)), terms[0])
    optional = [term for term in terms if term != anchor][:7]
    anchor_query = f'all:"{anchor}"'
    if not optional:
        return anchor_query
    optional_query = " OR ".join(f'all:"{term}"' for term in optional)
    return f"{anchor_query} AND ({optional_query})"


def _parse_arxiv_results(raw: bytes, limit: int) -> list[SearchResult]:
    root = ET.fromstring(raw)
    namespace = {"atom": _ARXIV_ATOM_NAMESPACE}
    results: list[SearchResult] = []
    seen: set[str] = set()
    for entry in root.findall("atom:entry", namespace):
        title = _clean(entry.findtext("atom:title", default="", namespaces=namespace))
        summary = _clean(
            entry.findtext("atom:summary", default="", namespaces=namespace)
        )
        pdf_url = ""
        alternate_url = ""
        for link in entry.findall("atom:link", namespace):
            href = str(link.attrib.get("href", "")).strip()
            if not href:
                continue
            if link.attrib.get("type") == "application/pdf":
                pdf_url = href
            elif link.attrib.get("rel") == "alternate":
                alternate_url = href
        entry_url = entry.findtext("atom:id", default="", namespaces=namespace)
        candidate = pdf_url or alternate_url or entry_url
        if candidate.startswith("http://arxiv.org/"):
            candidate = "https://arxiv.org/" + candidate.removeprefix(
                "http://arxiv.org/"
            )
        canonical = _canonical_url(candidate)
        if not title or not canonical or canonical in seen:
            continue
        seen.add(canonical)
        results.append(
            SearchResult(
                title=title,
                url=canonical,
                snippet=f"arXiv metadata: {summary[:1200]}",
                source_type="paper",
            )
        )
        if len(results) >= limit:
            break
    return results


def _arxiv_abstract_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not _host_matches(host, "arxiv.org"):
        return ""
    match = re.fullmatch(r"/pdf/([^/]+?)(?:\.pdf)?", parsed.path)
    if not match:
        return ""
    return f"https://arxiv.org/abs/{match.group(1)}"


def _resolve_ddg_url(url: str) -> str:
    parsed = urllib.parse.urlparse(html.unescape(url))
    query = urllib.parse.parse_qs(parsed.query)
    return query.get("uddg", [url])[0]


def _canonical_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    filtered = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query)
        if not key.lower().startswith("utm_") and key.lower() not in {"ref", "source"}
    ]
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc.lower(), parsed.path or "/", urllib.parse.urlencode(filtered), "")
    )


def _classify_source(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")
    if any(_host_matches(host, candidate) for candidate in _PAPER_HOSTS):
        return "paper"
    if _is_government_host(host):
        return "official"
    if (
        any(_host_matches(host, candidate) for candidate in _REFERENCE_HOSTS)
        or host.endswith(".edu")
        or host.startswith("docs.")
    ):
        return "reference"
    return "web"


def _search_result_fetch_priority(result: SearchResult) -> int:
    host = (urllib.parse.urlsplit(result.url).hostname or "").casefold().rstrip(".")
    if any(_host_matches(host, candidate) for candidate in _OPEN_RESEARCH_HOSTS):
        return 0
    if result.source_type in {"official", "reference"}:
        return 1
    if result.source_type == "paper":
        return 2
    if any(_host_matches(host, candidate) for candidate in _RESTRICTED_RESEARCH_HOSTS):
        return 4
    return 3


def _source_classification_signal(url: str, source_type: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "unknown").casefold().rstrip(".")
    if source_type == "paper":
        rule = "curated academic repository host"
    elif source_type == "official":
        rule = "government-domain suffix"
    elif host.endswith(".edu"):
        rule = "education-domain host"
    elif host.startswith("docs."):
        rule = "docs subdomain"
    elif any(_host_matches(host, candidate) for candidate in _REFERENCE_HOSTS):
        rule = "curated reference host"
    else:
        rule = "general-web fallback"
    return (
        f"source-classification-v2: type={source_type}; rule={rule}; "
        "publisher identity and claim authority are not verified"
    )


def _host_matches(host: str, candidate: str) -> bool:
    return host == candidate or host.endswith(f".{candidate}")


def _is_government_host(host: str) -> bool:
    return host == "europa.eu" or host.endswith(".europa.eu") or any(
        host.endswith(suffix) for suffix in _GOVERNMENT_SUFFIXES
    )


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _is_transient_network_error(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 425, 429, 500, 502, 503, 504}
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        return reason is not error and isinstance(reason, BaseException) and (
            _is_transient_network_error(reason)
        )
    if isinstance(error, ssl.SSLCertVerificationError):
        return False
    if isinstance(error, ssl.SSLError):
        return "CERTIFICATE_VERIFY_FAILED" not in str(error).upper()
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(
        error,
        (
            TimeoutError,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            http.client.IncompleteRead,
            http.client.RemoteDisconnected,
        ),
    ):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ETIMEDOUT,
    }


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, *, allow_rfc2544_proxy_fake_ip: bool = False) -> None:
        super().__init__()
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        return _pin_request(
            redirected,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject redirects for requests carrying an API credential."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Redirects are not allowed for authenticated search requests")


def _validate_public_url(
    url: str,
    *,
    allow_rfc2544_proxy_fake_ip: bool = False,
) -> None:
    _resolve_public_addresses(
        url,
        allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip,
    )


def _proxy_fake_ip_destination_allowed(
    parsed: urllib.parse.SplitResult,
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    enabled: bool,
) -> bool:
    if not enabled or parsed.scheme != "https" or ip not in _RFC2544_PROXY_FAKE_IP_NETWORK:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not host or "." not in host:
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    return not any(
        host == suffix.removeprefix(".") or host.endswith(suffix)
        for suffix in _BLOCKED_PROXY_HOST_SUFFIXES
    )


def _resolve_public_addresses(
    url: str,
    *,
    allow_rfc2544_proxy_fake_ip: bool = False,
) -> tuple[tuple[int, int, int, tuple], ...]:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise RuntimeError("Only HTTP(S) URLs may be fetched")
    if parsed.username or parsed.password:
        raise RuntimeError("URLs containing credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as error:
        raise RuntimeError("Invalid URL port") from error
    if port not in {None, 80, 443}:
        raise RuntimeError(f"Port {port} is not allowed")
    host = parsed.hostname
    if not host:
        raise RuntimeError("URL hostname is missing")
    try:
        address_info = socket.getaddrinfo(
            host,
            port or (443 if parsed.scheme == "https" else 80),
            0,
            socket.SOCK_STREAM,
        )
    except socket.gaierror as error:
        raise RuntimeError(f"Unable to resolve URL hostname: {host}") from error
    if not address_info:
        raise RuntimeError(f"URL hostname resolved to no addresses: {host}")
    destinations: list[tuple[int, int, int, tuple]] = []
    seen: set[tuple[int, tuple]] = set()
    for family, socktype, protocol, _canonical_name, sockaddr in address_info:
        address = sockaddr[0]
        ip = ipaddress.ip_address(address)
        if not ip.is_global and not _proxy_fake_ip_destination_allowed(
            parsed,
            ip,
            enabled=allow_rfc2544_proxy_fake_ip,
        ):
            raise RuntimeError(f"Non-public destination is blocked: {address}")
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        destinations.append((family, socktype, protocol, sockaddr))
    return tuple(destinations)


def _pin_request(
    request: urllib.request.Request,
    *,
    allow_rfc2544_proxy_fake_ip: bool = False,
) -> urllib.request.Request:
    setattr(
        request,
        _PINNED_ADDRESSES_ATTR,
        _resolve_public_addresses(
            request.full_url,
            allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip,
        ),
    )
    return request


def _request_pinned_addresses(
    request: urllib.request.Request,
    *,
    allow_rfc2544_proxy_fake_ip: bool = False,
) -> tuple[tuple[int, int, int, tuple], ...]:
    destinations = getattr(request, _PINNED_ADDRESSES_ATTR, None)
    if destinations is None:
        destinations = _resolve_public_addresses(
            request.full_url,
            allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip,
        )
        setattr(request, _PINNED_ADDRESSES_ATTR, destinations)
    return destinations


def _connect_pinned(
    destinations: tuple[tuple[int, int, int, tuple], ...],
    timeout: object,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    last_error: OSError | None = None
    for family, socktype, protocol, sockaddr in destinations:
        sock = socket.socket(family, socktype, protocol)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as error:
                if error.errno != errno.ENOPROTOOPT:
                    raise
            return sock
        except OSError as error:
            last_error = error
            sock.close()
    if last_error is not None:
        raise last_error
    raise OSError("URL hostname resolved to no usable addresses")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, destinations, **kwargs) -> None:
        self._pinned_destinations = destinations
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        if self._tunnel_host:
            raise RuntimeError("Proxy tunneling is disabled for public fetches")
        self.sock = _connect_pinned(
            self._pinned_destinations,
            self.timeout,
            self.source_address,
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, destinations, **kwargs) -> None:
        self._pinned_destinations = destinations
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        sys.audit("http.client.connect", self, self.host, self.port)
        if self._tunnel_host:
            raise RuntimeError("Proxy tunneling is disabled for public fetches")
        raw_socket = _connect_pinned(
            self._pinned_destinations,
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(
            raw_socket,
            server_hostname=self.host,
        )


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, *, allow_rfc2544_proxy_fake_ip: bool = False) -> None:
        super().__init__()
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)

    def http_open(self, request):
        destinations = _request_pinned_addresses(
            request,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPConnection(
                host,
                destinations,
                **kwargs,
            ),
            request,
        )


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(
        self,
        *,
        allow_rfc2544_proxy_fake_ip: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.allow_rfc2544_proxy_fake_ip = bool(allow_rfc2544_proxy_fake_ip)

    def https_open(self, request):
        destinations = _request_pinned_addresses(
            request,
            allow_rfc2544_proxy_fake_ip=self.allow_rfc2544_proxy_fake_ip,
        )
        return self.do_open(
            lambda host, **kwargs: _PinnedHTTPSConnection(
                host,
                destinations,
                **kwargs,
            ),
            request,
            context=self._context,
        )


def _public_opener(
    *,
    allow_rfc2544_proxy_fake_ip: bool = False,
    allow_redirects: bool = True,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _PinnedHTTPHandler(
            allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip
        ),
        _PinnedHTTPSHandler(
            context=ssl.create_default_context(),
            allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip,
        ),
        _SafeRedirectHandler(
            allow_rfc2544_proxy_fake_ip=allow_rfc2544_proxy_fake_ip
        )
        if allow_redirects
        else _NoRedirectHandler(),
    )


def _extract_pdf(raw: bytes) -> str:
    executable = shutil.which("pdftotext")
    if not executable:
        raise RuntimeError("pdftotext is required to parse PDF search results")
    with tempfile.TemporaryDirectory() as directory:
        pdf_path = f"{directory}/source.pdf"
        text_path = f"{directory}/source.txt"
        with open(pdf_path, "wb") as handle:
            handle.write(raw)
        process = subprocess.Popen(
            [executable, "-layout", pdf_path, text_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
        deadline = time.monotonic() + 30
        try:
            while process.poll() is None:
                try:
                    output_size = os.path.getsize(text_path)
                except FileNotFoundError:
                    output_size = 0
                if output_size > MAX_PDF_OUTPUT_BYTES:
                    _terminate_pdf_process(process)
                    raise ResourceLimitExceededError(
                        "resource_limit_exceeded: PDF extracted text exceeded the 400 KB limit"
                    )
                if time.monotonic() >= deadline:
                    _terminate_pdf_process(process)
                    raise ResourceLimitExceededError(
                        "resource_limit_exceeded: PDF parser exceeded the 30 second limit"
                    )
                time.sleep(0.01)
            return_code = process.wait()
        except ResourceLimitExceededError:
            raise
        except BaseException:
            _terminate_pdf_process(process)
            raise
        if return_code != 0:
            raise RuntimeError("PDF parsing failed")
        try:
            output_size = os.path.getsize(text_path)
        except FileNotFoundError as error:
            raise RuntimeError("PDF parser did not produce a text file") from error
        if output_size > MAX_PDF_OUTPUT_BYTES:
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: PDF extracted text exceeded the 400 KB limit"
            )
        with open(text_path, "rb") as handle:
            encoded = handle.read(MAX_PDF_OUTPUT_BYTES + 1)
        if len(encoded) > MAX_PDF_OUTPUT_BYTES:
            raise ResourceLimitExceededError(
                "resource_limit_exceeded: PDF extracted text exceeded the 400 KB limit"
            )
        text = encoded.decode("utf-8", errors="replace").strip()
    if len(text) < 80:
        raise RuntimeError("PDF contained too little extractable text")
    return text


def _terminate_pdf_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "posix" and process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            if os.name == "posix" and process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
    finally:
        # Reap even when the process ignored SIGTERM; this also prevents a
        # completed-but-uncollected helper from surviving the TemporaryDirectory.
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            process.wait(timeout=1)
