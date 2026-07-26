import http.client
import io
import ssl
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from deep_research.cache import FileCache
from deep_research.providers.base import (
    ProviderOutcomeUncertain,
    ProviderRequestNotSent,
)
from deep_research.providers.deepseek import (
    DeepSeekModelProvider,
    OpenAICompatibleModelProvider,
    _PhaseTrackingHTTPSConnection,
    _answer_claims,
    _is_pre_request_tls_eof,
    _mark_pre_request_tls_eof,
    _normalize_usage,
    _parse_json_object,
    _supports_temperature_override,
    _valid_plan_payload,
)
from deep_research.schemas import (
    AnswerSlot,
    Evidence,
    Page,
    ResearchPlan,
    Subgoal,
)


class DeepSeekTest(unittest.TestCase):
    def test_parses_json_object(self) -> None:
        self.assertEqual(_parse_json_object('{"answer": "ok"}')["answer"], "ok")

    def test_extracts_json_from_fenced_text(self) -> None:
        value = _parse_json_object('```json\n{"answer": "ok"}\n```')
        self.assertEqual(value["answer"], "ok")

    def test_rejects_incomplete_plan_payload(self) -> None:
        self.assertFalse(_valid_plan_payload({"answer_type": "short_text"}))

    def test_accepts_well_formed_plan_payload(self) -> None:
        self.assertTrue(
            _valid_plan_payload(
                {
                    "slots": [{"id": "answer", "description": "Answer"}],
                    "subgoals": [
                        {"id": "sg1", "question": "Find answer", "slot_ids": ["answer"]}
                    ],
                }
            )
        )

    def test_splits_every_answer_sentence_into_expected_claim(self) -> None:
        claims = _answer_claims("First fact [E1234abcd]. Second fact [E5678abcd].")
        self.assertEqual([item["claim_id"] for item in claims], ["C1", "C2"])
        self.assertEqual(claims[1]["evidence_ids"], ["E5678abcd"])

    def test_splits_chinese_factual_sentences(self) -> None:
        claims = _answer_claims("第一条事实[E1234abcd]。第二条事实[E5678abcd]！")
        self.assertEqual(len(claims), 2)
        self.assertEqual(claims[1]["claim_id"], "C2")

    def test_attaches_post_sentence_citations_to_the_preceding_claim(self) -> None:
        claims = _answer_claims(
            "Python was created by Guido van Rossum. [E1234abcd][E5678abcd]"
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(
            claims[0]["evidence_ids"],
            ["E1234abcd", "E5678abcd"],
        )

    def test_usage_requires_both_provider_token_counters(self) -> None:
        for usage in (
            {"completion_tokens": 2},
            {"prompt_tokens": 3},
            {"total_tokens": 5},
        ):
            with self.subTest(usage=usage):
                with self.assertRaisesRegex(ValueError, "missing required token counters"):
                    _normalize_usage(usage)

        self.assertEqual(
            _normalize_usage({"prompt_tokens": 3, "completion_tokens": 2}),
            {"prompt_tokens": 3, "completion_tokens": 2},
        )


class FakeDeepSeekProvider(DeepSeekModelProvider):
    def _post(self, body):
        return {"answer": "ok"}, {"prompt_tokens": 100, "completion_tokens": 25}


class SequencedDeepSeekProvider(DeepSeekModelProvider):
    def __init__(self, responses, cache_dir: Path):
        super().__init__(
            api_key="test",
            base_url="https://example.invalid",
            model="test-model",
            cache=FileCache(cache_dir),
        )
        self.responses = list(responses)
        self.post_calls = 0

    def _post(self, body):
        self.post_calls += 1
        return self.responses.pop(0), {"prompt_tokens": 10, "completion_tokens": 5}


class FailingWriteCache:
    def get_json(self, namespace, key):
        return None

    def put_json(self, namespace, key, value):
        raise OSError("injected cache write failure")


class FakeHttpResponse:
    def __init__(self, payload=None, read_error=None):
        self.payload = payload
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        if self.read_error is not None:
            raise self.read_error
        return self.payload


class StaticJsonDeepSeekProvider(DeepSeekModelProvider):
    def __init__(self, response, cache_dir: Path):
        super().__init__(
            api_key="test",
            base_url="https://example.invalid",
            model="test-model",
            cache=FileCache(cache_dir),
        )
        self.response = response

    async def _json_call(self, system, content):
        return self.response


class CapturingRequestProvider(OpenAICompatibleModelProvider):
    def __init__(self, model: str, cache_dir: Path):
        super().__init__(
            api_key="test",
            base_url="https://example.invalid/v1",
            model=model,
            cache=FileCache(cache_dir),
        )
        self.request_body = None

    def _post(self, body):
        self.request_body = body
        return {"ok": True}, {"prompt_tokens": 1, "completion_tokens": 1}


class DeepSeekUsageTest(unittest.IsolatedAsyncioTestCase):
    def make_provider(self, cache=None):
        return DeepSeekModelProvider(
            api_key="test",
            base_url="https://example.invalid",
            model="test-model",
            cache=cache or FileCache(Path(self.directory)),
        )

    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = self.temporary_directory.name

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_gpt5_omits_unsupported_temperature_but_other_models_keep_it(self) -> None:
        gpt = CapturingRequestProvider("azure/gpt-5.5", Path(self.directory) / "gpt")
        deepseek = CapturingRequestProvider(
            "deepseek-v4-flash", Path(self.directory) / "deepseek"
        )

        await gpt._json_call("system", {"probe": True})
        await deepseek._json_call("system", {"probe": True})

        self.assertNotIn("temperature", gpt.request_body)
        self.assertEqual(deepseek.request_body["temperature"], 0.1)
        self.assertFalse(_supports_temperature_override("gpt-5.4-nano"))
        self.assertTrue(_supports_temperature_override("qwen3.6-35b-a3b"))

    async def test_response_read_interruption_is_outcome_uncertain(self) -> None:
        provider = self.make_provider()
        response = FakeHttpResponse(
            read_error=http.client.IncompleteRead(b"partial response")
        )
        with mock.patch.object(provider, "_open_request", return_value=response) as open_request:
            with self.assertRaisesRegex(ProviderOutcomeUncertain, "reading it was interrupted"):
                await provider._json_call("system", {"x": 1})
        self.assertEqual(open_request.call_count, 1)

    async def test_http_200_invalid_json_is_outcome_uncertain(self) -> None:
        provider = self.make_provider()
        with mock.patch.object(
            provider, "_open_request", return_value=FakeHttpResponse(b"not-json")
        ):
            with self.assertRaisesRegex(ProviderOutcomeUncertain, "not valid JSON"):
                await provider._json_call("system", {"x": 1})

    async def test_paid_response_cache_write_failure_is_outcome_uncertain(self) -> None:
        provider = FakeDeepSeekProvider(
            api_key="test",
            base_url="https://example.invalid",
            model="test-model",
            cache=FailingWriteCache(),
        )
        with self.assertRaisesRegex(ProviderOutcomeUncertain, "could not be persisted"):
            await provider._json_call("system", {"x": 1})

    async def test_http_errors_are_definite_failures_without_successful_result(self) -> None:
        provider = self.make_provider()
        for status in (400, 429, 500, 503):
            error = urllib.error.HTTPError(
                "https://example.invalid/chat/completions",
                status,
                "injected HTTP failure",
                {},
                io.BytesIO(b'{"error":"injected"}'),
            )
            with self.subTest(status=status):
                with mock.patch.object(provider, "_open_request", side_effect=error):
                    with self.assertRaisesRegex(
                        RuntimeError, "no successful result was returned"
                    ) as raised:
                        await provider._json_call("system", {"status": status})
                self.assertNotIsInstance(raised.exception, ProviderOutcomeUncertain)

    async def test_retries_only_marked_pre_request_tls_eof_with_stable_idempotency_key(self) -> None:
        provider = self.make_provider()
        tls_eof = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "injected handshake EOF")
        _mark_pre_request_tls_eof(tls_eof)
        response = FakeHttpResponse(
            b'{"choices":[{"message":{"content":"{\\"ok\\":true}"}}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1}}'
        )
        with mock.patch.object(
            provider,
            "_open_request",
            side_effect=[urllib.error.URLError(tls_eof), response],
        ) as open_request:
            self.assertEqual(await provider._json_call("system", {"x": 1}), {"ok": True})

        self.assertEqual(open_request.call_count, 2)
        first, second = [call.args[0] for call in open_request.call_args_list]
        first_key = first.get_header("Idempotency-key")
        second_key = second.get_header("Idempotency-key")
        self.assertEqual(first_key, second_key)
        self.assertRegex(str(first_key), r"^deep-research-v1-[0-9a-f]{64}$")

    async def test_unmarked_tls_eof_remains_outcome_uncertain_without_retry(self) -> None:
        provider = self.make_provider()
        tls_eof = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "response EOF")
        with mock.patch.object(
            provider,
            "_open_request",
            side_effect=urllib.error.URLError(tls_eof),
        ) as open_request:
            with self.assertRaisesRegex(ProviderOutcomeUncertain, "automatic retry is disabled"):
                await provider._json_call("system", {"x": 1})
        self.assertEqual(open_request.call_count, 1)

    async def test_exhausted_marked_handshake_eof_is_proven_not_sent(self) -> None:
        provider = self.make_provider()
        errors = []
        for _ in range(2):
            tls_eof = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "injected handshake EOF")
            _mark_pre_request_tls_eof(tls_eof)
            errors.append(urllib.error.URLError(tls_eof))
        with mock.patch.object(
            provider,
            "_open_request",
            side_effect=errors,
        ) as open_request:
            with self.assertRaisesRegex(ProviderRequestNotSent, "retry is safe"):
                await provider._json_call("system", {"x": 1})
        self.assertEqual(open_request.call_count, 2)

    async def test_tracking_connection_marks_only_connect_phase_tls_eof(self) -> None:
        tls_eof = ssl.SSLEOFError(ssl.SSL_ERROR_EOF, "injected handshake EOF")
        connection = _PhaseTrackingHTTPSConnection("example.invalid")
        with mock.patch.object(
            http.client.HTTPSConnection,
            "connect",
            side_effect=tls_eof,
        ):
            with self.assertRaises(ssl.SSLEOFError):
                connection.connect()
        self.assertTrue(_is_pre_request_tls_eof(tls_eof))

    async def test_tracks_live_and_cached_usage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeDeepSeekProvider(
                api_key="test",
                base_url="https://example.invalid",
                model="test-model",
                cache=FileCache(Path(directory)),
                input_usd_per_m=1.0,
                output_usd_per_m=2.0,
            )
            await provider._json_call("system", {"x": 1})
            await provider._json_call("system", {"x": 1})
            usage = provider.usage_snapshot()
            self.assertEqual(usage["model_calls"], 1)
            self.assertEqual(usage["model_cache_hits"], 1)
            self.assertEqual(usage["input_tokens"], 100)
            self.assertAlmostEqual(usage["estimated_cost_usd"], 0.00015)

    async def test_emits_one_usage_event_for_each_live_provider_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeDeepSeekProvider(
                api_key="test",
                base_url="https://example.invalid",
                model="test-model",
                cache=FileCache(Path(directory)),
                input_usd_per_m=1.0,
                output_usd_per_m=2.0,
                pricing_configured=True,
            )
            events = []
            listener_id = provider.add_usage_listener(events.append)
            await provider._json_call("system", {"x": 1})
            # Local cache reuse has no new provider charge and therefore does
            # not emit a second billable response event.
            await provider._json_call("system", {"x": 1})
            provider.remove_usage_listener(listener_id)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["model_calls"], 1)
            self.assertEqual(events[0]["input_tokens"], 100)
            self.assertEqual(events[0]["output_tokens"], 25)
            self.assertEqual(events[0]["pricing_status"], "complete")
            self.assertAlmostEqual(events[0]["estimated_cost_usd"], 0.00015)

    async def test_prices_provider_cached_input_and_configured_free_models(self) -> None:
        class CachedUsageProvider(OpenAICompatibleModelProvider):
            def _post(self, _body):
                return {
                    "ok": True
                }, {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "prompt_tokens_details": {"cached_tokens": 40},
                }

        with tempfile.TemporaryDirectory() as directory:
            priced = CachedUsageProvider(
                api_key="test",
                base_url="https://example.invalid",
                model="gpt-5.5",
                cache=FileCache(Path(directory) / "priced"),
                input_usd_per_m=5.0,
                cached_input_usd_per_m=0.5,
                output_usd_per_m=30.0,
                pricing_configured=True,
            )
            await priced._json_call("system", {"x": 1})
            usage = priced.usage_snapshot()
            self.assertAlmostEqual(usage["estimated_cost_usd"], 0.00107)
            self.assertEqual(usage["pricing_status"], "complete")

            free = CachedUsageProvider(
                api_key="test",
                base_url="https://example.invalid",
                model="deepseek-v4-flash",
                cache=FileCache(Path(directory) / "free"),
                input_usd_per_m=0.0,
                cached_input_usd_per_m=0.0,
                output_usd_per_m=0.0,
                pricing_configured=True,
            )
            await free._json_call("system", {"x": 1})
            free_usage = free.usage_snapshot()
            self.assertEqual(free_usage["estimated_cost_usd"], 0.0)
            self.assertEqual(free_usage["pricing_status"], "complete")

    async def test_multimodal_prompt_is_not_falsely_priced_as_text_only(self) -> None:
        class ImageUsageProvider(OpenAICompatibleModelProvider):
            def _post(self, _body):
                return {"ok": True}, {"prompt_tokens": 100, "completion_tokens": 10}

        with tempfile.TemporaryDirectory() as directory:
            provider = ImageUsageProvider(
                api_key="test",
                base_url="https://example.invalid",
                model="gpt-5.5",
                cache=FileCache(Path(directory)),
                input_usd_per_m=5.0,
                cached_input_usd_per_m=0.5,
                output_usd_per_m=30.0,
                pricing_configured=True,
            )
            await provider._json_call(
                "system",
                {"x": 1},
                input_modalities={"text", "image"},
            )
            usage = provider.usage_snapshot()
            self.assertAlmostEqual(usage["estimated_cost_usd"], 0.0003)
            self.assertEqual(usage["pricing_status"], "partial")
            self.assertIn("modality-separated", usage["pricing_reason"])

    async def test_plan_repair_reuses_paid_cache_after_crash_window(self) -> None:
        provider = SequencedDeepSeekProvider(
            [
                {"answer_type": "short_text"},
                {
                    "answer_type": "short_text",
                    "slots": [
                        {"id": "answer", "description": "Answer", "required": True}
                    ],
                    "subgoals": [
                        {
                            "id": "sg-answer",
                            "question": "Find the answer",
                            "slot_ids": ["answer"],
                            "done_when": "Supported",
                        }
                    ],
                },
            ],
            Path(self.directory),
        )

        first = await provider.plan("A repairable question")
        second = await provider.plan("A repairable question")

        self.assertEqual(first.slots[0].id, "answer")
        self.assertEqual(second.slots[0].id, "answer")
        self.assertEqual(provider.post_calls, 2)
        self.assertEqual(provider.usage_snapshot()["model_cache_hits"], 2)

    async def test_extract_evidence_uses_page_url_and_exact_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "evidence": [
                        {
                            "slot_id": "creator",
                            "claim": "Guido created Python.",
                            "quote": "Python was created by Guido van Rossum.",
                            "stance": "supports",
                            "extraction_confidence": 0.12,
                        }
                    ]
                },
                Path(directory),
            )
            plan = ResearchPlan(
                "short_text",
                [AnswerSlot("creator", "Creator")],
                [Subgoal("sg-creator", "Find creator", ["creator"], "done")],
            )
            page = Page(
                url="https://docs.python.org/history",
                title="Python history",
                text="Python was created by Guido van Rossum.",
                source_type="official",
                content_hash="abc",
            )
            evidence = await provider.extract_evidence(plan, [page])
            self.assertEqual(evidence[0].source_url, page.url)
            self.assertEqual(evidence[0].extraction_confidence, 1.0)

    async def test_extract_evidence_rejects_translated_or_overstated_claims(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "evidence": [
                        {
                            "slot_id": "creator",
                            "claim": "Python由Guido van Rossum创建。",
                            "quote": "Python was created by Guido van Rossum.",
                            "stance": "supports",
                        },
                        {
                            "slot_id": "creator",
                            "claim": "Python was created by Guido van Rossum in 1991.",
                            "quote": "Python was created by Guido van Rossum.",
                            "stance": "supports",
                        },
                    ]
                },
                Path(directory),
            )
            plan = ResearchPlan(
                "short_text",
                [AnswerSlot("creator", "Creator")],
                [Subgoal("sg-creator", "Find creator", ["creator"], "done")],
            )
            page = Page(
                url="https://docs.python.org/history",
                title="Python history",
                text="Python was created by Guido van Rossum.",
                source_type="official",
                content_hash="abc",
            )

            evidence = await provider.extract_evidence(plan, [page])

            self.assertEqual(evidence, [])

    async def test_extract_evidence_accepts_router_schema_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "plan": {},
                    "source": {},
                    "schema": {
                        "evidence": [
                            {
                                "slot_id": "creator",
                                "claim": "Python was created by Guido van Rossum.",
                                "quote": "Python was created by Guido van Rossum.",
                                "stance": "supports",
                            }
                        ]
                    },
                },
                Path(directory),
            )
            plan = ResearchPlan(
                "short_text",
                [AnswerSlot("creator", "Creator")],
                [Subgoal("sg-creator", "Find creator", ["creator"], "done")],
            )
            page = Page(
                url="https://docs.python.org/history",
                title="Python history",
                text="Python was created by Guido van Rossum.",
                source_type="official",
                content_hash="abc",
            )

            evidence = await provider.extract_evidence(plan, [page])

            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].quote, page.text)

    async def test_extract_evidence_repairs_zero_external_output_with_raw_quote(self) -> None:
        class RecoveryProvider(StaticJsonDeepSeekProvider):
            def __init__(self, cache_dir: Path):
                super().__init__({"evidence": []}, cache_dir)
                self.prompts: list[str] = []

            async def _json_call(self, system, content):
                self.prompts.append(system)
                if "Repair an evidence extraction" in system:
                    return {
                        "evidence": [
                            {
                                "slot_id": "mechanism",
                                "claim": "3D structure\ncomplements appearance features.",
                                "quote": "3D structure\ncomplements appearance features.",
                                "stance": "supports",
                            }
                        ]
                    }
                return {"evidence": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = RecoveryProvider(Path(directory))
            plan = ResearchPlan(
                "short_text",
                [AnswerSlot("mechanism", "How structure complements appearance")],
                [
                    Subgoal(
                        "sg-mechanism",
                        "Find structural retrieval mechanism",
                        ["mechanism"],
                        "done",
                    )
                ],
            )
            page = Page(
                url="https://example.org/paper",
                title="A retrieval paper",
                text="3D structure\ncomplements appearance features.",
                source_type="paper",
                content_hash="abc",
            )

            evidence = await provider.extract_evidence(plan, [page])

            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].quote, page.text)
            self.assertEqual(evidence[0].claim, page.text)
            self.assertEqual(len(provider.prompts), 2)
            self.assertIn("Repair an evidence extraction", provider.prompts[-1])

    async def test_extract_evidence_repairs_pages_for_uncovered_required_slots(self) -> None:
        class PartialRecoveryProvider(StaticJsonDeepSeekProvider):
            def __init__(self, cache_dir: Path):
                super().__init__({}, cache_dir)
                self.calls: list[tuple[str, str]] = []

            async def _json_call(self, system, content):
                source_url = content["source"]["url"]
                self.calls.append((system, source_url))
                if "Repair an evidence extraction" in system:
                    return {
                        "evidence": [
                            {
                                "slot_id": "mechanism",
                                "claim": "3D structure complements appearance features.",
                                "quote": "3D structure complements appearance features.",
                                "stance": "supports",
                            }
                        ]
                    }
                if source_url.endswith("scope"):
                    return {
                        "evidence": [
                            {
                                "slot_id": "scope",
                                "claim": "Person Re-ID matches identities across cameras.",
                                "quote": "Person Re-ID matches identities across cameras.",
                                "stance": "supports",
                            }
                        ]
                    }
                return {"evidence": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = PartialRecoveryProvider(Path(directory))
            plan = ResearchPlan(
                "short_text",
                [
                    AnswerSlot("scope", "Task scope"),
                    AnswerSlot("mechanism", "Structural mechanism"),
                ],
                [
                    Subgoal("sg-scope", "Find scope", ["scope"], "done"),
                    Subgoal("sg-mechanism", "Find mechanism", ["mechanism"], "done"),
                ],
            )
            pages = [
                Page(
                    url="https://example.org/scope",
                    title="Scope",
                    text="Person Re-ID matches identities across cameras.",
                    source_type="paper",
                    content_hash="scope",
                ),
                Page(
                    url="https://example.org/mechanism",
                    title="Mechanism",
                    text="3D structure complements appearance features.",
                    source_type="paper",
                    content_hash="mechanism",
                ),
            ]

            evidence = await provider.extract_evidence(plan, pages)

        self.assertEqual({item.slot_id for item in evidence}, {"scope", "mechanism"})
        self.assertEqual(len(provider.calls), 3)
        self.assertEqual(provider.calls[-1][1], "https://example.org/mechanism")
        self.assertIn("Repair an evidence extraction", provider.calls[-1][0])

    async def test_extract_evidence_exposes_nonfactual_retrieval_slot_hints(self) -> None:
        class HintProvider(StaticJsonDeepSeekProvider):
            def __init__(self, cache_dir: Path):
                super().__init__({}, cache_dir)
                self.payloads: list[dict] = []

            async def _json_call(self, system, content):
                self.payloads.append(content)
                return {"evidence": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = HintProvider(Path(directory))
            plan = ResearchPlan(
                "short_text",
                [AnswerSlot("methods", "Recent method advances")],
                [
                    Subgoal(
                        "sg-methods",
                        "Find transformer ReID methods",
                        ["methods"],
                        "done",
                    )
                ],
            )
            page = Page(
                url="https://example.org/paper",
                title="Transformer ReID",
                text="Transformer methods for person re-identification.",
                source_type="paper",
                content_hash="hint-page",
                retrieval_query_texts=["person ReID transformer"],
                retrieval_subgoal_ids=["sg-methods"],
            )

            await provider.extract_evidence(plan, [page])

        self.assertEqual(
            provider.payloads[0]["retrieval_hints"],
            [
                {
                    "subgoal_id": "sg-methods",
                    "slot_ids": ["methods"],
                    "question": "Find transformer ReID methods",
                }
            ],
        )

    async def test_extract_evidence_recovers_routed_exact_quote_after_empty_model_output(self) -> None:
        class EmptyProvider(StaticJsonDeepSeekProvider):
            def __init__(self, cache_dir: Path):
                super().__init__({}, cache_dir)
                self.calls = 0

            async def _json_call(self, system, content):
                self.calls += 1
                return {"evidence": []}

        with tempfile.TemporaryDirectory() as directory:
            provider = EmptyProvider(Path(directory))
            plan = ResearchPlan(
                "short_text",
                [
                    AnswerSlot(
                        "challenges",
                        "Deployment challenges such as domain shift and occlusion",
                    )
                ],
                [
                    Subgoal(
                        "sg-challenges",
                        "Find real-world ReID limitations",
                        ["challenges"],
                        "done",
                    )
                ],
            )
            page = Page(
                url="https://example.org/reid-domain-shift",
                title="Cross-domain ReID",
                text=(
                    "Person re-identification suffers from domain shift across "
                    "camera networks."
                ),
                source_type="paper",
                content_hash="domain-shift",
                retrieval_query_texts=[
                    "person re-identification domain shift occlusion deployment"
                ],
                retrieval_subgoal_ids=["sg-challenges"],
            )

            evidence = await provider.extract_evidence(plan, [page])

        self.assertEqual(provider.calls, 2)
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0].slot_id, "challenges")
        self.assertEqual(
            evidence[0].quote,
            "Person re-identification suffers from domain shift across camera networks.",
        )
        self.assertEqual(evidence[0].claim, evidence[0].quote)

    async def test_future_slot_recovery_ignores_generic_model_label_and_finds_future_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "evidence": [
                        {
                            "slot_id": "future",
                            "claim": "A baseline keeps the newest gallery image.",
                            "quote": "A baseline keeps the newest gallery image.",
                            "stance": "supports",
                        }
                    ]
                },
                Path(directory),
            )
            plan = ResearchPlan(
                "text",
                [AnswerSlot("future", "Future research directions for ReID")],
                [
                    Subgoal(
                        "sg-future",
                        "Find practical future research directions for person ReID",
                        ["future"],
                        "done",
                    )
                ],
            )
            page = Page(
                url="https://example.org/reid-survey",
                title="ReID survey",
                text=(
                    "A baseline keeps the newest gallery image. "
                    "Future research should improve robustness to domain shift."
                ),
                source_type="paper",
                content_hash="future-recovery",
                retrieval_query_texts=["person ReID future research directions"],
                retrieval_subgoal_ids=["sg-future"],
            )

            evidence = await provider.extract_evidence(plan, [page])

        recovered = next(
            item
            for item in evidence
            if item.quote == "Future research should improve robustness to domain shift."
        )
        self.assertEqual(recovered.slot_id, "future")
        self.assertEqual(recovered.claim, recovered.quote)

    async def test_verifier_omission_is_unsupported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "items": [
                        {
                            "claim_id": "C1",
                            "claim": "First fact.",
                            "evidence_ids": ["E1234abcd"],
                            "status": "entailed",
                            "reason": "Supported.",
                        }
                    ]
                },
                Path(directory),
            )
            evidence = [
                Evidence(
                    id="E1234abcd",
                    subgoal_id="sg",
                    slot_id="slot",
                    claim="First fact.",
                    quote="First fact.",
                    source_url="https://example.org/a",
                    source_title="A",
                    stance="supports",
                    reliability=0.9,
                    extraction_confidence=1.0,
                    content_hash="a",
                    source_cluster_id="example.org",
                ),
                Evidence(
                    id="E5678abcd",
                    subgoal_id="sg",
                    slot_id="slot",
                    claim="Second fact.",
                    quote="Second fact.",
                    source_url="https://example.net/b",
                    source_title="B",
                    stance="supports",
                    reliability=0.9,
                    extraction_confidence=1.0,
                    content_hash="b",
                    source_cluster_id="example.net",
                ),
            ]
            report = await provider.verify(
                "First fact [E1234abcd]. Second fact [E5678abcd].", evidence
            )
            self.assertFalse(report.passed)
            self.assertEqual(report.items[1].status, "unsupported")
            self.assertIn("omitted", report.items[1].reason)

    async def test_verifier_cannot_add_uncited_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = StaticJsonDeepSeekProvider(
                {
                    "items": [
                        {
                            "claim_id": "C1",
                            "claim": "First fact.",
                            "evidence_ids": ["E1234abcd", "E5678abcd"],
                            "status": "entailed",
                            "reason": "Supported using both quotes.",
                        }
                    ]
                },
                Path(directory),
            )
            evidence = [
                Evidence("E1234abcd", "sg", "slot", "First fact.", "First fact.", "https://a.example", "A", "supports", .9, 1.0, "a", "a.example"),
                Evidence("E5678abcd", "sg", "slot", "Extra fact.", "Extra fact.", "https://b.example", "B", "supports", .9, 1.0, "b", "b.example"),
            ]
            report = await provider.verify("First fact [E1234abcd].", evidence)
            self.assertFalse(report.passed)
            self.assertEqual(report.items[0].status, "unsupported")
            self.assertFalse(report.items[0].citation_set_match)


if __name__ == "__main__":
    unittest.main()
