import unittest

from deep_research.providers.web import (
    _classify_source,
    _extract_provenance_metadata,
    _source_classification_signal,
)
from deep_research.schemas import (
    AnswerSlot,
    Evidence,
    Page,
    Query,
    ResearchPlan,
    SourceRecord,
    Subgoal,
)
from deep_research.engine import ResearchEngine
from deep_research.state import ResearchState


class ProvenanceMetadataTest(unittest.TestCase):
    def test_source_classification_is_host_bound_and_conservative(self) -> None:
        self.assertEqual(_classify_source("https://arxiv.org/abs/1"), "paper")
        self.assertEqual(
            _classify_source("https://arxiv.org.attacker.example/abs/1"),
            "web",
        )
        self.assertEqual(_classify_source("https://agency.gov/report"), "official")
        self.assertEqual(_classify_source("https://docs.example.com/guide"), "reference")
        self.assertEqual(_classify_source("https://university.edu/news"), "reference")

    def test_source_classification_signal_disclaims_identity_verification(self) -> None:
        signal = _source_classification_signal(
            "https://agency.gov/report", "official"
        )

        self.assertIn("government-domain suffix", signal)
        self.assertIn("publisher identity", signal)
        self.assertIn("not verified", signal)

    def test_resolves_relative_canonical_url_and_extracts_site_name(self) -> None:
        metadata = _extract_provenance_metadata(
            """
            <html><head>
              <link rel="canonical" href="../canonical/story">
              <meta property="og:site_name" content="Example News">
            </head></html>
            """,
            "https://news.example.test/articles/2026/story.html",
        )

        self.assertIsInstance(metadata, dict)
        self.assertEqual(
            metadata["canonical_url"],
            "https://news.example.test/articles/canonical/story",
        )
        self.assertEqual(metadata["site_name"], "Example News")

    def test_collects_meta_authors_and_deduplicates_them(self) -> None:
        metadata = _extract_provenance_metadata(
            """
            <html><head>
              <meta name="author" content=" Alice Smith ">
              <meta name="citation_author" content="Alice Smith">
              <meta name="citation_author" content="Bob Jones">
              <meta name="author" content="Bob Jones">
            </head></html>
            """,
            "https://example.test/story",
        )

        self.assertEqual(metadata["author_names"], ["Alice Smith", "Bob Jones"])

    def test_extracts_json_ld_publisher_and_upstream_urls(self) -> None:
        metadata = _extract_provenance_metadata(
            """
            <html><head>
              <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "NewsArticle",
                  "publisher": {
                    "@type": "Organization",
                    "name": "Example Wire",
                    "url": "https://wire.example.test/about"
                  },
                  "isBasedOn": "https://origin.example.test/report",
                  "citation": ["https://journal.example.test/paper"]
                }
              </script>
            </head></html>
            """,
            "https://news.example.test/story",
        )

        self.assertEqual(metadata["publisher_name"], "Example Wire")
        self.assertEqual(metadata["publisher_url"], "https://wire.example.test/about")
        self.assertEqual(
            metadata["upstream_urls"],
            [
                "https://origin.example.test/report",
                "https://journal.example.test/paper",
            ],
        )

    def test_ignores_malicious_and_non_http_metadata_urls(self) -> None:
        metadata = _extract_provenance_metadata(
            """
            <html><head>
              <link rel="canonical" href="javascript:alert('canonical')">
              <script type="application/ld+json">
                {
                  "@context": "https://schema.org",
                  "@type": "Article",
                  "publisher": {
                    "name": "Named Publisher",
                    "url": "file:///etc/passwd"
                  },
                  "isBasedOn": "javascript:alert('upstream')",
                  "citation": [
                    "mailto:editor@example.test",
                    "https://safe.example.test/source"
                  ]
                }
              </script>
            </head></html>
            """,
            "https://news.example.test/story",
        )

        self.assertEqual(metadata["canonical_url"], "")
        self.assertEqual(metadata["publisher_url"], "")
        self.assertEqual(metadata["publisher_name"], "Named Publisher")
        self.assertEqual(
            metadata["upstream_urls"], ["https://safe.example.test/source"]
        )

    def test_no_metadata_returns_empty_dict_without_guessing_publisher(self) -> None:
        metadata = _extract_provenance_metadata(
            "<html><head><title>Example Publisher</title></head><body>Story</body></html>",
            "https://example-publisher.test/story",
        )

        self.assertEqual(
            metadata,
            {
                "canonical_url": "",
                "publisher_name": "",
                "publisher_url": "",
                "author_names": [],
                "site_name": "",
                "upstream_urls": [],
                "provenance_signals": [],
            },
        )

    def test_unnamed_meta_elements_do_not_abort_page_extraction(self) -> None:
        metadata = _extract_provenance_metadata(
            """
            <html><head>
              <meta charset="utf-8">
              <meta content="width=device-width, initial-scale=1">
              <meta property="og:site_name" content="Tracking Lab">
            </head></html>
            """,
            "https://tracking.example.test/article",
        )

        self.assertEqual(metadata["site_name"], "Tracking Lab")

    def test_page_and_source_record_new_fields_have_backward_compatible_defaults(self) -> None:
        page = Page(url="https://example.test", title="Example", text="Body")
        source = SourceRecord(
            id="S1",
            url="https://example.test",
            title="Example",
            source_type="web",
            snippet="Snippet",
        )

        self.assertEqual(
            (
                page.canonical_url,
                page.publisher_name,
                page.publisher_url,
                page.author_names,
                page.site_name,
                page.upstream_urls,
                page.provenance_signals,
            ),
            ("", "", "", [], "", [], []),
        )
        self.assertEqual(
            (
                source.publisher_name,
                source.publisher_url,
                source.publisher_id,
                source.author_names,
                source.site_name,
                source.upstream_urls,
                source.provenance_signals,
            ),
            ("", "", "", [], "", [], []),
        )

    def test_cross_domain_canonical_is_grouped_but_remains_self_declared(self) -> None:
        source = SourceRecord(
            id="S1",
            url="https://repost.example/story",
            final_url="https://repost.example/story",
            canonical_url="https://wire.example/original",
            title="Story",
            source_type="web",
            snippet="",
            status="fetched",
        )
        state = ResearchState(run_id="provenance-run", question="q", sources=[source])
        ResearchEngine._resolve_source_provenance(state, source)
        self.assertEqual(source.origin_cluster_id, "declared-upstream:wire.example")
        self.assertEqual(source.independence_status, "declared_upstream")
        self.assertIn("not treated as verified", source.independence_reason)

    def test_matching_declared_publishers_merge_cross_domain_sources(self) -> None:
        first = SourceRecord(
            id="S1",
            url="https://site-one.example/story",
            final_url="https://site-one.example/story",
            title="One",
            source_type="web",
            snippet="",
            status="fetched",
            publisher_id="declared-publisher-name:abc",
            origin_cluster_id="declared-publisher-name:abc",
        )
        second = SourceRecord(
            id="S2",
            url="https://site-two.example/story",
            final_url="https://site-two.example/story",
            title="Two",
            source_type="web",
            snippet="",
            status="fetched",
            publisher_id="declared-publisher-name:abc",
        )
        state = ResearchState(
            run_id="publisher-run", question="q", sources=[first, second]
        )
        ResearchEngine._resolve_source_provenance(state, second)
        self.assertEqual(second.origin_cluster_id, first.origin_cluster_id)
        self.assertEqual(second.independence_status, "same_publisher_group")

    def test_distinct_arxiv_works_are_not_collapsed_into_the_repository_host(self) -> None:
        first = SourceRecord(
            id="S1",
            url="https://arxiv.org/pdf/2301.00001v1",
            final_url="https://arxiv.org/pdf/2301.00001v1",
            title="First paper",
            source_type="paper",
            snippet="",
            status="fetched",
        )
        second = SourceRecord(
            id="S2",
            url="https://arxiv.org/abs/2301.00002",
            final_url="https://arxiv.org/abs/2301.00002",
            title="Second paper",
            source_type="paper",
            snippet="",
            status="fetched",
        )
        state = ResearchState(run_id="arxiv-run", question="q", sources=[first, second])

        ResearchEngine._resolve_source_provenance(state, first)
        ResearchEngine._resolve_source_provenance(state, second)

        self.assertEqual(first.independence_status, "distinct_scholarly_work")
        self.assertEqual(second.independence_status, "distinct_scholarly_work")
        self.assertNotEqual(first.origin_cluster_id, second.origin_cluster_id)
        self.assertIn("not as verified independent", first.independence_reason)

    def test_arxiv_abs_and_pdf_of_the_same_work_are_dependent(self) -> None:
        first = SourceRecord(
            id="S1",
            url="https://arxiv.org/pdf/2301.00001v1",
            final_url="https://arxiv.org/pdf/2301.00001v1",
            title="Paper PDF",
            source_type="paper",
            snippet="",
            status="fetched",
        )
        duplicate = SourceRecord(
            id="S2",
            url="https://arxiv.org/abs/2301.00001",
            final_url="https://arxiv.org/abs/2301.00001",
            title="Paper abstract",
            source_type="paper",
            snippet="",
            status="fetched",
        )
        state = ResearchState(
            run_id="arxiv-duplicate-run",
            question="q",
            sources=[first, duplicate],
        )

        ResearchEngine._resolve_source_provenance(state, first)
        ResearchEngine._resolve_source_provenance(state, duplicate)

        self.assertEqual(duplicate.independence_status, "dependent")
        self.assertEqual(duplicate.near_duplicate_of_source_id, first.id)

    def test_evidence_does_not_inherit_latest_fetch_when_content_hash_differs(self) -> None:
        source = SourceRecord(
            id="S1",
            url="https://example.test/story",
            final_url="https://example.test/story",
            title="Story",
            source_type="web",
            snippet="",
            status="fetched",
            content_hash="new-content",
            content_hash_scope="page_text",
            fetch_record_id="F-latest",
            snapshot_available=True,
            snapshot_sha256="snapshot-latest",
            fetch_binding_status="server_bound",
            fetch_binding_valid=True,
        )
        item = Evidence(
            id="E1",
            subgoal_id="sg1",
            slot_id="slot1",
            claim="old claim",
            quote="old quote",
            source_url=source.url,
            source_title=source.title,
            stance="supports",
            reliability=0.9,
            extraction_confidence=0.9,
            content_hash="old-content",
            source_cluster_id="host:example.test",
        )
        state = ResearchState(run_id="provenance-run", question="q", sources=[source])

        ResearchEngine._attach_provenance(state, [item])

        self.assertEqual(item.source_id, source.id)
        self.assertEqual(item.fetch_record_id, "")
        self.assertEqual(item.snapshot_sha256, "")
        self.assertEqual(item.fetch_binding_status, "unbound")
        self.assertIsNone(item.fetch_binding_valid)

    def test_relevance_accepts_only_source_bound_cross_language_query_targets(self) -> None:
        query = Query(
            text="3D pose estimation improves 2D tracking robustness under occlusion",
            subgoal_id="sg-core",
            strategy="source_targeting",
        )
        plan = ResearchPlan(
            "text",
            [
                AnswerSlot(
                    "core",
                    "解释三维结构为何能在遮挡等困难场景增强二维追踪鲁棒性。",
                )
            ],
            [
                Subgoal(
                    "sg-core",
                    "为什么三维姿态和几何结构能补充二维追踪？",
                    ["core"],
                    "有可定位证据",
                )
            ],
        )
        relevant_source = SourceRecord(
            id="S-relevant",
            url="https://papers.example.test/pose",
            title="Pose tracking study",
            source_type="paper",
            snippet="",
            status="fetched",
            query_texts=[query.text],
        )
        unrelated_source = SourceRecord(
            id="S-unrelated",
            url="https://papers.example.test/chess",
            title="Chess archive",
            source_type="web",
            snippet="",
            status="fetched",
            query_texts=["medieval chess history"],
        )
        relevant = Evidence(
            id="E-relevant",
            subgoal_id="sg-core",
            slot_id="core",
            claim="3D pose estimates retain geometric cues under occlusion and improve tracking robustness.",
            quote="3D pose estimates retain geometric cues under occlusion and improve tracking robustness.",
            source_url=relevant_source.url,
            source_title=relevant_source.title,
            stance="supports",
            reliability=0.9,
            extraction_confidence=1.0,
            content_hash="relevant-content",
            source_cluster_id="host:example.test",
        )
        unrelated = Evidence(
            id="E-unrelated",
            subgoal_id="sg-core",
            slot_id="core",
            claim="Medieval chess games used carved wooden pieces.",
            quote="Medieval chess games used carved wooden pieces.",
            source_url=unrelated_source.url,
            source_title=unrelated_source.title,
            stance="supports",
            reliability=0.65,
            extraction_confidence=1.0,
            content_hash="unrelated-content",
            source_cluster_id="host:example.test",
        )
        state = ResearchState(
            run_id="cross-language-query-target",
            question="为什么三维信息增强二维追踪？",
            plan=plan,
            queries=[query],
            sources=[relevant_source, unrelated_source],
        )

        ResearchEngine._attach_provenance(state, [relevant, unrelated])

        self.assertGreaterEqual(relevant.slot_relevance_score, 0.45)
        self.assertTrue(
            any(query.text in reason for reason in relevant.slot_relevance_reasons)
        )
        self.assertEqual(unrelated.slot_relevance_score, 0.0)


if __name__ == "__main__":
    unittest.main()
