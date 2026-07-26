import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deep_research.cache import FileCache
from deep_research.config import AppConfig
from deep_research.engine import ResearchEngine
from deep_research.multimodal import (
    MAX_ATTACHMENT_BYTES,
    prepare_attachment_content,
    validate_attachment,
)
from deep_research.providers.base import AttachmentContent, RenderedPage
from deep_research.providers.deepseek import OpenAICompatibleModelProvider
from deep_research.providers.mock import MockModelProvider, ReplaySearchProvider
from deep_research.schemas import (
    AttachmentObservation,
    Evidence,
    GroundedObservation,
    InputAttachment,
)
from deep_research.storage import ArtifactIntegrityError, RunStore


PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"test-payload"
WAV_BYTES = b"RIFF\x04\x00\x00\x00WAVE"


def attachment(
    data: bytes,
    *,
    attachment_id: str = "I-test",
    name: str = "input.bin",
    media_type: str = "application/octet-stream",
    modality: str = "document",
) -> InputAttachment:
    return InputAttachment(
        id=attachment_id,
        name=name,
        media_type=media_type,
        modality=modality,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_length=len(data),
        content_uri=f"inputs/{attachment_id}",
        created_at="2026-07-21T00:00:00+00:00",
    )


class CapturingPerceptionProvider(OpenAICompatibleModelProvider):
    def __init__(self, cache_dir: Path, modalities: tuple[str, ...]) -> None:
        super().__init__(
            api_key="test",
            base_url="https://gateway.example/v1",
            model="vision-test-model",
            cache=FileCache(cache_dir),
            model_choice="gpt",
            modalities=modalities,
        )
        self.requests: list[tuple[dict, list[dict]]] = []

    async def _json_call(self, system, content, media_parts=None):
        self.requests.append((content, list(media_parts or [])))
        return {
            "summary": "Grounded input summary",
            "observations": [
                {
                    "locator": "full attachment",
                    "text": "Visible or audible content",
                    "kind": "description",
                    "confidence": 0.91,
                    "region": None,
                }
            ],
        }


class CountingPerceptionModel(MockModelProvider):
    def __init__(self) -> None:
        self.perception_calls = 0

    async def perceive(self, question, attachments):
        self.perception_calls += 1
        return await super().perceive(question, attachments)


class SimulatedPerceptionCrash(BaseException):
    pass


class CrashAfterFirstPerceptionOperation(ResearchEngine):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.perception_commits = 0

    def _after_operation_completed(self, node, operation_key):
        if node == "perceive_inputs":
            self.perception_commits += 1
            if self.perception_commits == 1:
                raise SimulatedPerceptionCrash()


class AttachmentValidationTest(unittest.TestCase):
    def test_binary_media_requires_matching_magic_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid image/png signature"):
            validate_attachment("forged.png", "image/png", b"not a png")

        name, media_type, modality = validate_attachment(
            "camera.bin", "application/octet-stream", PNG_BYTES
        )
        self.assertEqual((name, media_type, modality), ("camera.bin", "image/png", "image"))

    def test_declared_mime_cannot_override_detected_bytes(self) -> None:
        with self.assertRaisesRegex(ValueError, "not the declared"):
            validate_attachment("image.txt", "text/plain", PNG_BYTES)

    def test_json_is_utf8_and_structurally_validated(self) -> None:
        self.assertEqual(
            validate_attachment("input.json", "application/json", b'{"ok": true}')[1],
            "application/json",
        )
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            validate_attachment("input.json", "application/json", b"{broken")

    def test_filename_is_reduced_to_a_safe_basename(self) -> None:
        name, _, _ = validate_attachment("../../private/note.txt", "text/plain", b"hello")
        self.assertEqual(name, "note.txt")

    def test_per_file_limit_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "8 MB or smaller"):
            validate_attachment(
                "oversized.txt",
                "text/plain",
                b"x" * (MAX_ATTACHMENT_BYTES + 1),
            )


class AttachmentStorageTest(unittest.TestCase):
    def test_content_addressed_manifest_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "attachment-integrity")
            stored = store.store_input_attachment(
                name="note.txt",
                media_type="text/plain",
                modality="text",
                data=b"durable note",
            )

            self.assertEqual(stored.id, "I" + stored.sha256)
            self.assertTrue(store.input_attachment_audit()[0]["manifest_valid"])
            path = store.run_dir / stored.content_uri
            path.write_bytes(b"tampered")
            with self.assertRaisesRegex(ArtifactIntegrityError, "byte length|SHA-256"):
                store.read_input_attachment(stored.id)
            self.assertFalse(store.input_attachment_audit()[0]["manifest_valid"])

    def test_store_enforces_run_count_and_total_limits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "attachment-limits")
            with patch("deep_research.multimodal.MAX_ATTACHMENT_COUNT", 1):
                store.store_input_attachment(
                    name="one.txt", media_type="text/plain", modality="text", data=b"one"
                )
                with self.assertRaisesRegex(ValueError, "at most 1"):
                    store.store_input_attachment(
                        name="two.txt", media_type="text/plain", modality="text", data=b"two"
                    )

        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "attachment-total")
            with patch("deep_research.multimodal.MAX_TOTAL_ATTACHMENT_BYTES", 5):
                store.store_input_attachment(
                    name="one.txt", media_type="text/plain", modality="text", data=b"123"
                )
                with self.assertRaisesRegex(ValueError, "24 MB total"):
                    store.store_input_attachment(
                        name="two.txt", media_type="text/plain", modality="text", data=b"456"
                    )

    def test_prepare_rejects_bytes_outside_durable_manifest(self) -> None:
        item = attachment(b"original", media_type="text/plain", modality="text")
        with self.assertRaisesRegex(ValueError, "durable SHA-256"):
            prepare_attachment_content(item, b"replacement", render_pdf=False)

    def test_pdf_can_supply_text_and_rendered_pages(self) -> None:
        data = b"%PDF-1.7\nminimal"
        item = attachment(
            data,
            name="paper.pdf",
            media_type="application/pdf",
            modality="document",
        )
        rendered = RenderedPage(page=1, media_type="image/png", data=PNG_BYTES)
        with patch("deep_research.multimodal._extract_pdf", return_value="Extracted paper text"), patch(
            "deep_research.multimodal._render_pdf_pages", return_value=[rendered]
        ):
            content = prepare_attachment_content(item, data, render_pdf=True)

        self.assertEqual(content.extracted_text, "Extracted paper text")
        self.assertEqual(content.rendered_pages[0].page, 1)
        self.assertIn("pdftotext", content.parser_version)
        self.assertIn("pdftoppm", content.parser_version)


class NativeMediaPayloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_image_is_sent_as_an_openai_compatible_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = CapturingPerceptionProvider(
                Path(directory), ("text", "document", "image")
            )
            item = attachment(
                PNG_BYTES,
                name="figure.png",
                media_type="image/png",
                modality="image",
            )
            observations = await provider.perceive(
                "What is shown?", [AttachmentContent(item, PNG_BYTES)]
            )

        media = provider.requests[0][1][0]
        self.assertEqual(media["type"], "image_url")
        self.assertEqual(
            media["image_url"]["url"],
            "data:image/png;base64," + base64.b64encode(PNG_BYTES).decode("ascii"),
        )
        self.assertIsNone(observations[0].observations[0].region)
        self.assertEqual(observations[0].model_choice, "gpt")

    async def test_audio_is_sent_as_native_input_audio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = CapturingPerceptionProvider(
                Path(directory), ("text", "document", "audio")
            )
            item = attachment(
                WAV_BYTES,
                name="clip.wav",
                media_type="audio/wav",
                modality="audio",
            )
            await provider.perceive(
                "What was said?", [AttachmentContent(item, WAV_BYTES)]
            )

        media = provider.requests[0][1][0]
        self.assertEqual(media["type"], "input_audio")
        self.assertEqual(media["input_audio"]["format"], "wav")
        self.assertEqual(
            media["input_audio"]["data"],
            base64.b64encode(WAV_BYTES).decode("ascii"),
        )


class PerceptionRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_each_attachment_has_an_independent_replayable_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                runs_dir=root / "runs",
                replay_corpus=Path("examples/replay_corpus.json"),
            )
            config.budget.max_iterations = 0
            store = RunStore(config.runs_dir, "perception-replay")
            for name, data in (("one.txt", b"first grounded note"), ("two.txt", b"second grounded note")):
                store.store_input_attachment(
                    name=name,
                    media_type="text/plain",
                    modality="text",
                    data=data,
                )

            first_model = CountingPerceptionModel()
            with self.assertRaises(SimulatedPerceptionCrash):
                await CrashAfterFirstPerceptionOperation(
                    config,
                    first_model,
                    ReplaySearchProvider(config.replay_corpus),
                ).run("Who created Python?", run_id="perception-replay")
            self.assertEqual(first_model.perception_calls, 1)

            resumed_model = CountingPerceptionModel()
            state = await ResearchEngine(
                config,
                resumed_model,
                ReplaySearchProvider(config.replay_corpus),
            ).run("Who created Python?", run_id="perception-replay")

            self.assertEqual(resumed_model.perception_calls, 1)
            self.assertEqual(len(state.attachment_observations), 2)
            self.assertTrue(
                any(
                    item.operation == "perceive_inputs" and item.execution_mode == "replayed"
                    for item in state.agent_invocations
                )
            )
            perception_operations = [
                row for row in store.operation_rows() if row["node"] == "perceive_inputs"
            ]
            self.assertEqual(len(perception_operations), 2)
            self.assertTrue(all(row["status"] == "succeeded" for row in perception_operations))


class AttachmentGroundingTest(unittest.TestCase):
    def test_only_an_exact_observation_match_supplies_grounding_confidence(self) -> None:
        observation = AttachmentObservation(
            attachment_id="I-one",
            modality="image",
            summary="A chart",
            observations=[
                GroundedObservation(
                    locator="page 1, chart title",
                    text="Revenue increased by 20 percent.",
                    confidence=0.93,
                )
            ],
            model_choice="gpt",
            model_id="gpt-vision",
        )
        state = type("State", (), {"attachment_observations": [observation]})()

        matched = self._evidence("Revenue increased by 20 percent.")
        unmatched = self._evidence("The summary says this is a chart.")
        ResearchEngine._attach_multimodal_grounding(state, [matched, unmatched])

        self.assertEqual(matched.source_locator, "page 1, chart title")
        self.assertEqual(matched.grounding_confidence, 0.93)
        self.assertEqual(unmatched.source_locator, "")
        self.assertEqual(unmatched.grounding_confidence, 0.0)

    @staticmethod
    def _evidence(quote: str) -> Evidence:
        return Evidence(
            id="E" + hashlib.sha1(quote.encode()).hexdigest()[:8],
            subgoal_id="sg-answer",
            slot_id="answer",
            claim=quote,
            quote=quote,
            source_url="attachment://I-one",
            source_title="chart.png",
            stance="supports",
            reliability=0.6,
            extraction_confidence=1.0,
            content_hash="content",
            source_cluster_id="attachment",
            attachment_id="I-one",
            modality="image",
        )


if __name__ == "__main__":
    unittest.main()
