import asyncio
from email.message import Message
from http import HTTPStatus
import json
import os
import socket
import ssl
import tempfile
from types import SimpleNamespace
import unittest
from pathlib import Path
import urllib.request
import urllib.error
from unittest import mock

from deep_research import webapp
from deep_research.cache import FileCache
from deep_research.providers.base import (
    ProviderOutcomeUncertain,
    ResourceLimitExceededError,
)
from deep_research.providers.deepseek import (
    MAX_DEEPSEEK_RESPONSE_BYTES,
    DeepSeekModelProvider,
)
from deep_research.providers.web import (
    MAX_FETCH_BYTES,
    MAX_PDF_FETCH_BYTES,
    MAX_PDF_OUTPUT_BYTES,
    MAX_SEARCH_BYTES,
    _SafeRedirectHandler,
    _arxiv_abstract_url,
    _arxiv_search_expression,
    _connect_pinned,
    _extract_pdf,
    _pin_request,
    _public_opener,
    _parse_arxiv_results,
    _search_result_fetch_priority,
    _validate_public_url,
    BraveSearchProvider,
    DuckDuckGoSearchProvider,
    OpenAlexSearchProvider,
)
from deep_research.schemas import Page, Query, SearchResult
from deep_research.storage import RunStore


def _invoke_web_get(
    runs_dir: Path,
    path: str,
    headers: dict[str, str] | None = None,
):
    previous = os.environ.get("DR_RUNS_DIR")
    os.environ["DR_RUNS_DIR"] = str(runs_dir)
    handler = object.__new__(webapp.ResearchRequestHandler)
    handler.path = path
    handler.server = SimpleNamespace(server_port=8000)
    request_headers = Message()
    for name, value in (
        headers
        or {
            "Host": "127.0.0.1:8000",
        }
    ).items():
        request_headers[name] = value
    handler.headers = request_headers
    responses: list[tuple[int, object]] = []

    def record_json(value, status=HTTPStatus.OK):
        responses.append((int(status), value))

    handler._json = record_json
    try:
        handler.do_GET()
    finally:
        if previous is None:
            os.environ.pop("DR_RUNS_DIR", None)
        else:
            os.environ["DR_RUNS_DIR"] = previous
    if not responses:
        raise AssertionError(f"GET {path} did not produce a JSON response")
    return responses[-1]


class _FakeDeepSeekResponse:
    def __init__(self, payload: bytes, url: str) -> None:
        self.payload = payload
        self.url = url
        self.read_sizes: list[int | None] = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self) -> str:
        return self.url

    def read(self, amount: int | None = None) -> bytes:
        self.read_sizes.append(amount)
        return self.payload if amount is None else self.payload[:amount]


class SecurityTest(unittest.TestCase):
    def test_run_store_rejects_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                RunStore(Path(directory), "../escape")

    def test_fetch_validator_rejects_loopback_and_private_addresses(self) -> None:
        for url in ("http://127.0.0.1/", "http://10.0.0.1/", "http://[::1]/"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                _validate_public_url(url)

    def test_public_fetch_connects_to_the_address_validated_before_io(self) -> None:
        public_address = "93.184.216.34"
        address_info = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (public_address, 443),
            )
        ]
        with mock.patch("socket.getaddrinfo", return_value=address_info) as resolver:
            request = _pin_request(urllib.request.Request("https://example.com/"))
        destinations = getattr(request, "_deep_research_pinned_addresses")
        fake_socket = mock.Mock()
        with mock.patch("socket.socket", return_value=fake_socket):
            connected = _connect_pinned(destinations, 3, None)

        self.assertIs(connected, fake_socket)
        fake_socket.connect.assert_called_once_with((public_address, 443))
        resolver.assert_called_once()

    def test_rfc2544_fake_ip_is_blocked_without_explicit_opt_in(self) -> None:
        fake_ip = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("198.18.1.81", 443),
            )
        ]
        with mock.patch("socket.getaddrinfo", return_value=fake_ip):
            with self.assertRaisesRegex(RuntimeError, "Non-public destination"):
                _pin_request(urllib.request.Request("https://example.com/"))

    def test_rfc2544_opt_in_allows_only_https_domain_names(self) -> None:
        fake_ip = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("198.18.1.81", 443),
            )
        ]
        with mock.patch("socket.getaddrinfo", return_value=fake_ip):
            request = _pin_request(
                urllib.request.Request("https://html.duckduckgo.com/html/"),
                allow_rfc2544_proxy_fake_ip=True,
            )
        self.assertEqual(
            getattr(request, "_deep_research_pinned_addresses"),
            (
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("198.18.1.81", 443),
                ),
            ),
        )

        for url in (
            "http://html.duckduckgo.com/html/",
            "https://198.18.1.81/",
            "https://localhost/",
            "https://service.internal/",
        ):
            with self.subTest(url=url), mock.patch(
                "socket.getaddrinfo",
                return_value=fake_ip,
            ), self.assertRaisesRegex(RuntimeError, "Non-public destination"):
                _pin_request(
                    urllib.request.Request(url),
                    allow_rfc2544_proxy_fake_ip=True,
                )

    def test_rfc2544_opt_in_is_applied_to_each_https_redirect(self) -> None:
        handler = _SafeRedirectHandler(allow_rfc2544_proxy_fake_ip=True)
        request = urllib.request.Request("https://example.com/start")
        fake_ip = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("198.18.1.81", 443),
            )
        ]
        with mock.patch("socket.getaddrinfo", return_value=fake_ip):
            redirected = handler.redirect_request(
                request,
                None,
                302,
                "Found",
                {},
                "https://redirect.example/next",
            )
        self.assertEqual(
            getattr(redirected, "_deep_research_pinned_addresses"),
            (
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    ("198.18.1.81", 443),
                ),
            ),
        )

        with mock.patch("socket.getaddrinfo", return_value=fake_ip):
            with self.assertRaisesRegex(RuntimeError, "Non-public destination"):
                handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "http://redirect.example/next",
                )

    def test_public_opener_propagates_rfc2544_policy_to_all_network_hops(self) -> None:
        opener = _public_opener(allow_rfc2544_proxy_fake_ip=True)
        guarded_handlers = [
            handler
            for handler in opener.handlers
            if hasattr(handler, "allow_rfc2544_proxy_fake_ip")
        ]
        self.assertEqual(len(guarded_handlers), 3)
        self.assertTrue(
            all(handler.allow_rfc2544_proxy_fake_ip for handler in guarded_handlers)
        )

    def test_search_and_fetch_response_limits_are_non_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))

            search_response = mock.MagicMock()
            search_response.__enter__.return_value = search_response
            search_response.__exit__.return_value = False
            search_response.read.return_value = b"x" * (MAX_SEARCH_BYTES + 1)
            search_opener = mock.Mock()
            search_opener.open.return_value = search_response
            with mock.patch(
                "deep_research.providers.web._pin_request",
                side_effect=lambda request: request,
            ), mock.patch(
                "deep_research.providers.web._public_opener",
                return_value=search_opener,
            ):
                with self.assertRaises(ResourceLimitExceededError):
                    provider._search_sync("bounded search", 5)

            fetch_response = mock.MagicMock()
            fetch_response.__enter__.return_value = fetch_response
            fetch_response.__exit__.return_value = False
            fetch_response.headers.get_content_type.return_value = "text/html"
            fetch_response.read.return_value = b"x" * (MAX_FETCH_BYTES + 1)
            fetch_response.geturl.return_value = "https://example.org/large"
            fetch_response.status = 200
            fetch_opener = mock.Mock()
            fetch_opener.open.return_value = fetch_response
            with mock.patch(
                "deep_research.providers.web._pin_request",
                side_effect=lambda request: request,
            ), mock.patch(
                "deep_research.providers.web._public_opener",
                return_value=fetch_opener,
            ):
                with self.assertRaises(ResourceLimitExceededError):
                    provider._fetch_sync(
                        SearchResult(
                            title="Large page",
                            url="https://example.org/large",
                            snippet="",
                        )
                    )

    def test_transient_tls_eof_is_retried_with_a_fresh_pinned_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.headers.get_content_type.return_value = "text/html"
            response.geturl.return_value = "https://example.org/result"
            response.status = 200
            response.read.return_value = b"usable response"
            opener = mock.Mock()
            opener.open.side_effect = [
                urllib.error.URLError(ssl.SSLEOFError(8, "unexpected eof")),
                response,
            ]

            with mock.patch(
                "deep_research.providers.web._pin_request",
                side_effect=lambda request: request,
            ) as pin, mock.patch(
                "deep_research.providers.web._public_opener",
                return_value=opener,
            ), mock.patch("deep_research.providers.web.time.sleep") as sleep:
                raw, final_url, status, content_type = provider._read_url_sync(
                    "https://example.org/result",
                    default_byte_limit=MAX_FETCH_BYTES,
                )

            self.assertEqual(raw, b"usable response")
            self.assertEqual(final_url, "https://example.org/result")
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "text/html")
            self.assertEqual(opener.open.call_count, 2)
            self.assertEqual(pin.call_count, 2)
            sleep.assert_called_once_with(0.25)

    def test_http_403_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            opener = mock.Mock()
            opener.open.side_effect = urllib.error.HTTPError(
                "https://example.org/blocked",
                403,
                "Forbidden",
                None,
                None,
            )
            with mock.patch(
                "deep_research.providers.web._pin_request",
                side_effect=lambda request: request,
            ), mock.patch(
                "deep_research.providers.web._public_opener",
                return_value=opener,
            ), mock.patch("deep_research.providers.web.time.sleep") as sleep:
                with self.assertRaises(urllib.error.HTTPError):
                    provider._read_url_sync(
                        "https://example.org/blocked",
                        default_byte_limit=MAX_FETCH_BYTES,
                    )
            self.assertEqual(opener.open.call_count, 1)
            sleep.assert_not_called()

    def test_pdf_transport_has_a_separate_bounded_input_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            response = mock.MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.headers.get_content_type.return_value = "application/pdf"
            response.geturl.return_value = "https://arxiv.org/pdf/1234.5678"
            response.status = 200
            response.read.return_value = b"%PDF-bounded"
            opener = mock.Mock()
            opener.open.return_value = response
            with mock.patch(
                "deep_research.providers.web._pin_request",
                side_effect=lambda request: request,
            ), mock.patch(
                "deep_research.providers.web._public_opener",
                return_value=opener,
            ):
                provider._read_url_sync(
                    "https://arxiv.org/pdf/1234.5678",
                    default_byte_limit=MAX_FETCH_BYTES,
                    pdf_byte_limit=MAX_PDF_FETCH_BYTES,
                )
            self.assertGreater(MAX_PDF_FETCH_BYTES, MAX_FETCH_BYTES)
            response.read.assert_called_once_with(MAX_PDF_FETCH_BYTES + 1)

    def test_oversized_arxiv_pdf_falls_back_to_audited_abstract_page(self) -> None:
        class OversizedPdf:
            def __len__(self) -> int:
                return MAX_PDF_FETCH_BYTES + 1

        html = (
            b"<html><head><title>Tracking paper</title></head><body><p>"
            + b"The official abstract explains how 3D structure improves tracking "
            + b"under heavy occlusion and appearance change."
            + b"</p></body></html>"
        )
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            with mock.patch.object(
                provider,
                "_read_url_sync",
                side_effect=[
                    (
                        OversizedPdf(),
                        "https://arxiv.org/pdf/1811.10863v1",
                        200,
                        "application/pdf",
                    ),
                    (
                        html,
                        "https://arxiv.org/abs/1811.10863v1",
                        200,
                        "text/html",
                    ),
                ],
            ) as read:
                page = provider._fetch_sync(
                    SearchResult(
                        title="Tracking paper",
                        url="https://arxiv.org/pdf/1811.10863v1",
                        snippet="",
                        source_type="paper",
                    )
                )

        self.assertEqual(page.url, "https://arxiv.org/abs/1811.10863v1")
        self.assertIn("heavy occlusion", page.text)
        self.assertTrue(
            any("official arXiv abstract" in item for item in page.provenance_signals)
        )
        self.assertEqual(
            read.call_args_list[1].args[0],
            "https://arxiv.org/abs/1811.10863v1",
        )

    def test_arxiv_abstract_fallback_only_rewrites_arxiv_pdf_urls(self) -> None:
        self.assertEqual(
            _arxiv_abstract_url("https://arxiv.org/pdf/1811.10863v1.pdf"),
            "https://arxiv.org/abs/1811.10863v1",
        )
        self.assertEqual(
            _arxiv_abstract_url("https://example.org/pdf/1811.10863v1.pdf"),
            "",
        )

    def test_open_research_results_rank_ahead_of_blocked_aggregators(self) -> None:
        open_paper = SearchResult(
            title="Open paper",
            url="https://openaccess.thecvf.com/content/paper.pdf",
            snippet="",
            source_type="web",
        )
        general = SearchResult(
            title="General page",
            url="https://tracking.example/article",
            snippet="",
            source_type="web",
        )
        restricted = SearchResult(
            title="Restricted copy",
            url="https://www.researchgate.net/publication/123",
            snippet="",
            source_type="web",
        )
        self.assertLess(
            _search_result_fetch_priority(open_paper),
            _search_result_fetch_priority(general),
        )
        self.assertLess(
            _search_result_fetch_priority(general),
            _search_result_fetch_priority(restricted),
        )

    def test_arxiv_expression_anchors_numeric_modality_and_keeps_context(self) -> None:
        expression = _arxiv_search_expression(
            "why 3D pose improves 2D tracking under occlusion"
        )
        self.assertIn('all:"3D"', expression)
        self.assertIn('all:"tracking"', expression)
        self.assertIn('all:"occlusion"', expression)
        self.assertNotIn('all:"why"', expression)

    def test_duckduckgo_challenge_falls_back_to_structured_arxiv_results(self) -> None:
        atom = b"""<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>http://arxiv.org/abs/1811.10863v1</id>
            <title>Object Tracking by Reconstruction</title>
            <summary>3D reconstruction improves tracking through heavy occlusion.</summary>
            <link href="https://arxiv.org/abs/1811.10863v1" rel="alternate" type="text/html" />
            <link href="https://arxiv.org/pdf/1811.10863v1" rel="related" type="application/pdf" />
          </entry>
        </feed>"""
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            challenge = b"<html><div class='anomaly-modal'>Bots use DuckDuckGo</div></html>"
            with mock.patch.object(
                provider,
                "_read_url_sync",
                side_effect=[
                    (challenge, "https://html.duckduckgo.com/html/", 202, "text/html"),
                    (
                        atom,
                        "https://export.arxiv.org/api/query",
                        200,
                        "application/atom+xml",
                    ),
                ],
            ) as read:
                results = provider._search_sync("3D tracking occlusion", 3)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_type, "paper")
        self.assertEqual(
            results[0].url,
            "https://arxiv.org/pdf/1811.10863v1",
        )
        self.assertIn("heavy occlusion", results[0].snippet)
        self.assertEqual(read.call_count, 2)
        self.assertIn("export.arxiv.org/api/query", read.call_args_list[1].args[0])

    def test_brave_search_uses_header_auth_without_caching_the_secret(self) -> None:
        payload = json.dumps(
            {
                "web": {
                    "results": [
                        {
                            "title": "General source",
                            "url": "https://example.org/article?utm_source=brave",
                            "description": "General material.",
                        },
                        {
                            "title": "Open paper",
                            "url": "https://arxiv.org/abs/2401.00001",
                            "description": "Research material.",
                            "extra_snippets": ["Additional context."],
                        },
                    ]
                }
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider = BraveSearchProvider(
                FileCache(root),
                api_key="brave-test-secret",
            )
            with mock.patch.object(
                provider,
                "_read_url_sync",
                return_value=(
                    payload,
                    "https://api.search.brave.com/res/v1/web/search",
                    200,
                    "application/json",
                ),
            ) as read:
                results = asyncio.run(
                    provider.search(Query("3D improves 2D retrieval", "sg", "broad"), 2)
                )

            request_url = read.call_args.args[0]
            request_headers = read.call_args.kwargs["headers"]
            self.assertIn("api.search.brave.com/res/v1/web/search", request_url)
            self.assertIn("q=3D+improves+2D+retrieval", request_url)
            self.assertNotIn("brave-test-secret", request_url)
            self.assertEqual(request_headers["X-Subscription-Token"], "brave-test-secret")
            self.assertFalse(read.call_args.kwargs["allow_redirects"])
            self.assertEqual(results[0].url, "https://arxiv.org/abs/2401.00001")
            self.assertEqual(results[1].url, "https://example.org/article")
            cached = "\n".join(
                path.read_text(encoding="utf-8")
                for path in root.rglob("*.json")
            )
            self.assertNotIn("brave-test-secret", cached)

    def test_brave_search_requires_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "DR_BRAVE_API_KEY is empty"):
                BraveSearchProvider(FileCache(Path(directory)), api_key="")

    def test_openalex_search_uses_public_work_metadata_then_returns_source_url(self) -> None:
        payload = json.dumps(
            {
                "results": [
                    {
                        "display_name": "Learning 3D Shape Feature for Texture-insensitive Person Re-identification",
                        "publication_year": 2021,
                        "cited_by_count": 149,
                        "doi": "https://doi.org/10.1109/cvpr46437.2021.00805",
                        "open_access": {"oa_url": None},
                        "primary_location": {
                            "landing_page_url": "https://doi.org/10.1109/cvpr46437.2021.00805",
                            "pdf_url": None,
                        },
                        "best_oa_location": {
                            "landing_page_url": "http://arxiv.org/abs/2103.11111",
                            "pdf_url": "https://arxiv.org/pdf/2103.11111",
                        },
                        "abstract_inverted_index": {
                            "2D": [0],
                            "appearance": [1],
                            "and": [2],
                            "3D": [3],
                            "shape": [4],
                            "are": [5],
                            "complementary.": [6],
                        },
                    },
                    {"display_name": "Unfetchable metadata only"},
                ]
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            provider = OpenAlexSearchProvider(FileCache(Path(directory)))
            with mock.patch.object(
                provider,
                "_read_url_sync",
                return_value=(
                    payload,
                    "https://api.openalex.org/works",
                    200,
                    "application/json",
                ),
            ) as read:
                results = asyncio.run(
                    provider.search(
                        Query("why 3D can enhance 2D retrieval", "sg", "broad"),
                        2,
                    )
                )

        request_url = read.call_args.args[0]
        self.assertIn("api.openalex.org/works", request_url)
        self.assertIn("search=3D+enhance+2D+retrieval", request_url)
        self.assertIn("per-page=10", request_url)
        self.assertEqual(read.call_args.kwargs["headers"], {"Accept": "application/json"})
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "https://arxiv.org/abs/2103.11111")
        self.assertEqual(results[0].source_type, "paper")
        self.assertIn("2D appearance and 3D shape are complementary", results[0].snippet)

    def test_openalex_reid_search_filters_unrelated_metadata(self) -> None:
        def work(title: str, abstract: dict[str, list[int]], identifier: str) -> dict[str, object]:
            return {
                "display_name": title,
                "publication_year": 2024,
                "cited_by_count": 12,
                "doi": None,
                "open_access": {"oa_url": None},
                "primary_location": {
                    "landing_page_url": f"https://arxiv.org/abs/{identifier}",
                    "pdf_url": None,
                },
                "best_oa_location": None,
                "abstract_inverted_index": abstract,
            }

        payload = json.dumps(
            {
                "results": [
                    work(
                        "Recent Advances in Natural Language Processing",
                        {"Transformer": [0], "models": [1], "for": [2], "language": [3]},
                        "2401.00001",
                    ),
                    work(
                        "Vision-Language Transformer for Person Re-Identification",
                        {
                            "Person": [0],
                            "re-identification": [1],
                            "uses": [2],
                            "vision-language": [3],
                            "features.": [4],
                        },
                        "2401.00002",
                    ),
                ]
            }
        ).encode()
        with tempfile.TemporaryDirectory() as directory:
            provider = OpenAlexSearchProvider(FileCache(Path(directory)))
            with mock.patch.object(
                provider,
                "_read_url_sync",
                return_value=(
                    payload,
                    "https://api.openalex.org/works",
                    200,
                    "application/json",
                ),
            ) as read:
                results = asyncio.run(
                    provider.search(
                        Query(
                            "person ReID latest advances transformer vision-language method paper evidence",
                            "sg",
                            "broad",
                        ),
                        3,
                    )
                )

        request_url = read.call_args.args[0]
        self.assertIn(
            "search=person+re-identification+transformer+vision-language",
            request_url,
        )
        self.assertEqual(
            [item.title for item in results],
            ["Vision-Language Transformer for Person Re-Identification"],
        )

    def test_openalex_reid_guard_also_filters_arxiv_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = OpenAlexSearchProvider(FileCache(Path(directory)))
            fallback = [
                SearchResult(
                    title="Transformer for Person Re-Identification",
                    url="https://arxiv.org/abs/2401.00002",
                    snippet="Person re-identification with transformer features.",
                    source_type="paper",
                ),
                SearchResult(
                    title="Multilingual Competition Report",
                    url="https://arxiv.org/abs/2401.00003",
                    snippet="A shared task for multilingual text classification.",
                    source_type="paper",
                ),
            ]
            with mock.patch.object(
                provider,
                "_read_url_sync",
                side_effect=RuntimeError("OpenAlex unavailable"),
            ), mock.patch.object(
                provider._fetch_provider,
                "_search_arxiv_sync",
                return_value=fallback,
            ) as arxiv_search:
                results = asyncio.run(
                    provider.search(
                        Query("person ReID latest transformer advances", "sg", "broad"),
                        3,
                    )
                )

        self.assertEqual(
            [item.title for item in results],
            ["Transformer for Person Re-Identification"],
        )
        self.assertEqual(arxiv_search.call_args.args[0], "person re-identification transformer")

    def test_arxiv_parser_rejects_entries_without_fetchable_identity(self) -> None:
        results = _parse_arxiv_results(
            b"""<feed xmlns="http://www.w3.org/2005/Atom">
              <entry><title>No URL</title><summary>Missing identity.</summary></entry>
            </feed>""",
            3,
        )
        self.assertEqual(results, [])

    def test_empty_search_results_are_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DuckDuckGoSearchProvider(FileCache(Path(directory)))
            query = Query("no durable empty result", "sg", "broad")
            with mock.patch.object(provider, "_search_sync", return_value=[]) as search:
                asyncio.run(provider.search(query, limit=3))
                asyncio.run(provider.search(query, limit=3))
            self.assertEqual(search.call_count, 2)

    def test_pdf_output_limit_terminates_parser_without_retryable_error(self) -> None:
        process = mock.Mock()
        process.pid = 123
        process.poll.return_value = None
        process.wait.return_value = 0
        with mock.patch(
            "deep_research.providers.web.shutil.which",
            return_value="/usr/bin/pdftotext",
        ), mock.patch(
            "deep_research.providers.web.subprocess.Popen",
            return_value=process,
        ), mock.patch(
            "deep_research.providers.web.os.path.getsize",
            return_value=MAX_PDF_OUTPUT_BYTES + 1,
        ), mock.patch("deep_research.providers.web.os.killpg"):
            with self.assertRaises(ResourceLimitExceededError):
                _extract_pdf(b"%PDF-oversized")

    def test_each_redirect_is_resolved_and_private_rebinding_is_rejected(self) -> None:
        handler = _SafeRedirectHandler()
        request = urllib.request.Request("https://example.com/start")
        private_address = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("127.0.0.1", 443),
            )
        ]
        with mock.patch("socket.getaddrinfo", return_value=private_address):
            with self.assertRaisesRegex(RuntimeError, "Non-public destination"):
                handler.redirect_request(
                    request,
                    None,
                    302,
                    "Found",
                    {},
                    "https://redirect.example/next",
                )

    def test_all_api_gets_apply_origin_boundary_before_dispatch(self) -> None:
        paths = (
            "/api/config",
            "/api/methodology",
            "/api/system-contract",
            "/api/protocol-verification",
            "/api/runs",
            "/api/runs/missing-run",
            "/api/runs/missing-run/stream",
            "/api/runs/missing-run/protocol-audit",
            "/api/runs/missing-run/artifacts/A1",
            "/api/runs/missing-run/sources/S1/snapshot",
        )
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory) / "runs"
            for path in paths:
                with self.subTest(path=path):
                    status, _body = _invoke_web_get(
                        runs_dir,
                        path,
                        {
                            "Host": "attacker.example",
                            "Origin": "http://attacker.example",
                            "Sec-Fetch-Site": "cross-site",
                        },
                    )
                    self.assertEqual(status, 403)
            self.assertFalse(runs_dir.exists())

    def test_unknown_run_gets_do_not_create_a_run_or_sqlite(self) -> None:
        paths = (
            "/api/runs/missing-run",
            "/api/runs/missing-run/stream",
            "/api/runs/missing-run/protocol-audit",
            "/api/runs/missing-run/artifacts/A1",
            "/api/runs/missing-run/sources/S1/snapshot",
        )
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory) / "runs"
            for path in paths:
                with self.subTest(path=path):
                    status, _body = _invoke_web_get(runs_dir, path)
                    self.assertEqual(status, 404)
                    self.assertFalse((runs_dir / "missing-run").exists())
            self.assertFalse(runs_dir.exists())

    def test_run_response_does_not_expose_execution_owner_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runs_dir = Path(directory) / "runs"
            RunStore(runs_dir, "public-run")
            with webapp._jobs_lock:
                webapp._jobs["public-run"] = {
                    "status": "running",
                    "error": "",
                    "owner_token": "top-secret-owner",
                    "fence": 7,
            }
            try:
                status, payload = _invoke_web_get(
                    runs_dir,
                    "/api/runs/public-run",
                )
                self.assertEqual(status, 200)
                self.assertNotIn("owner_token", payload["job"])
                self.assertNotIn("top-secret-owner", json.dumps(payload))
            finally:
                with webapp._jobs_lock:
                    webapp._jobs.pop("public-run", None)

    def test_request_metadata_rejects_malformed_authority_and_unknown_site(self) -> None:
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000/path",
                None,
                "same-origin",
                8000,
            )
        )
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000",
                None,
                "cross-origin",
                8000,
            )
        )

    def test_corrupt_cache_is_a_miss_and_replacement_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = FileCache(Path(directory))
            path = cache._path("test", "key")
            path.parent.mkdir(parents=True)
            path.write_text("{not-json", encoding="utf-8")
            self.assertIsNone(cache.get_json("test", "key"))
            cache.put_json("test", "key", {"ok": True})
            self.assertEqual(cache.get_json("test", "key"), {"ok": True})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_run_store_lease_rejects_a_second_executor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "leased-run")
            lease = store.acquire_lease()
            try:
                with self.assertRaises(RuntimeError):
                    store.acquire_lease()
            finally:
                lease.release()
            replacement = store.acquire_lease()
            replacement.release()

    def test_source_snapshot_is_private_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "snapshot-run")
            metadata = store.write_source_snapshot(
                "S1234abcd",
                Page(url="https://example.org", title="Example", text="verbatim text"),
            )
            snapshot = store.read_source_snapshot("S1234abcd")
            self.assertEqual(snapshot["text"], "verbatim text")
            self.assertEqual(snapshot["sha256"], metadata["sha256"])
            path = Path(directory) / "snapshot-run/sources/S1234abcd.txt"
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(ValueError):
                store.read_source_snapshot("../escape")

    def test_event_reader_only_uses_a_bounded_complete_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_bytes(
                b"x" * (webapp.MAX_EVENT_TAIL_BYTES + 100)
                + b"\n"
                + b'{"event_id":"tail","payload":{}}\n'
                + b'{"event_id":"partial"'
            )
            self.assertEqual(
                [event["event_id"] for event in webapp._read_events(path)],
                ["tail"],
            )

    def test_deepseek_requires_https_origin_and_does_not_redirect_auth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                DeepSeekModelProvider(
                    api_key="secret",
                    base_url="http://api.deepseek.example",
                    model="test",
                    cache=FileCache(Path(directory)),
                )
            provider = DeepSeekModelProvider(
                api_key="secret",
                base_url="https://api.deepseek.example",
                model="test",
                cache=FileCache(Path(directory)),
            )
            response = _FakeDeepSeekResponse(
                b'{"choices":[{"message":{"content":"{}"}}],"usage":{}}',
                "http://attacker.example/stolen",
            )
            captured: list[urllib.request.Request] = []

            def open_request(request):
                captured.append(request)
                return response

            with mock.patch.object(provider, "_open_request", side_effect=open_request):
                with self.assertRaisesRegex(RuntimeError, "trusted HTTPS origin"):
                    provider._post({"messages": []})

            self.assertNotIn("Authorization", captured[0].headers)
            self.assertEqual(
                captured[0].unredirected_hdrs["Authorization"],
                "Bearer secret",
            )

    def test_deepseek_response_and_usage_are_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DeepSeekModelProvider(
                api_key="secret",
                base_url="https://api.deepseek.example",
                model="test",
                cache=FileCache(Path(directory)),
            )
            response = _FakeDeepSeekResponse(
                b"x" * (MAX_DEEPSEEK_RESPONSE_BYTES + 1),
                "https://api.deepseek.example/chat/completions",
            )
            with mock.patch.object(provider, "_open_request", return_value=response):
                with self.assertRaisesRegex(ProviderOutcomeUncertain, "larger"):
                    provider._post({"messages": []})
            self.assertEqual(
                response.read_sizes,
                [MAX_DEEPSEEK_RESPONSE_BYTES + 1],
            )

            with mock.patch.object(
                provider,
                "_post",
                return_value=(
                    '{"answer":"ok"}',
                    {"prompt_tokens": -1, "completion_tokens": float("nan")},
                ),
            ):
                with self.assertRaisesRegex(ProviderOutcomeUncertain, "invalid"):
                    asyncio.run(provider._json_call("system", {"x": 1}))

    def test_openai_compatible_base_url_keeps_v1_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = DeepSeekModelProvider(
                api_key="secret",
                base_url="https://gateway.example/v1/",
                model="test",
                cache=FileCache(Path(directory)),
                timeout_seconds=240,
            )
            response = _FakeDeepSeekResponse(
                b'{"choices":[{"message":{"content":"{}"}}],"usage":{}}',
                "https://gateway.example/v1/chat/completions",
            )
            captured: list[urllib.request.Request] = []
            captured_timeouts: list[float] = []

            def open_request(request, timeout):
                captured.append(request)
                captured_timeouts.append(timeout)
                return response

            with mock.patch.object(provider._opener, "open", side_effect=open_request):
                provider._post({"messages": []})

            self.assertEqual(
                captured[0].full_url,
                "https://gateway.example/v1/chat/completions",
            )
            self.assertEqual(captured_timeouts, [240.0])

    def test_model_timeout_rejects_unbounded_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for value in (True, 0, 9.9, 601, float("inf")):
                with self.subTest(value=value), self.assertRaisesRegex(
                    ValueError,
                    "between 10 and 600",
                ):
                    DeepSeekModelProvider(
                        api_key="secret",
                        base_url="https://gateway.example/v1",
                        model="test",
                        cache=FileCache(Path(directory)),
                        timeout_seconds=value,
                    )


if __name__ == "__main__":
    unittest.main()
