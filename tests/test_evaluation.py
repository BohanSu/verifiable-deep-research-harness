import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from deep_research.evaluation import evaluate_jsonl


class _StubEngine:
    def __init__(self, states: list[SimpleNamespace]) -> None:
        self._states = iter(states)

    async def run(self, question: str, *, run_id: str) -> SimpleNamespace:
        return next(self._states)


class _FailingThenPassingEngine:
    def __init__(self, state: SimpleNamespace) -> None:
        self._state = state
        self._calls = 0

    async def run(self, question: str, *, run_id: str) -> SimpleNamespace:
        self._calls += 1
        if self._calls == 1:
            raise RuntimeError("provider unavailable")
        return self._state


class EvaluationTest(unittest.TestCase):
    def test_missing_reports_are_serialized_as_unavailable(self) -> None:
        state = SimpleNamespace(
            status="failed",
            draft_answer="",
            closure=None,
            verification=None,
            counters=SimpleNamespace(search_calls=0, pages_fetched=0),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "tasks.jsonl"
            output = root / "results.json"
            dataset.write_text(
                json.dumps({"id": "t1", "question": "Q", "answers": []}) + "\n",
                encoding="utf-8",
            )

            records = asyncio.run(
                evaluate_jsonl(_StubEngine([state]), dataset, output)
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertIsNone(records[0].closure_score)
        self.assertEqual(records[0].closure_score_status, "unavailable")
        self.assertIsNone(records[0].citation_passed)
        self.assertEqual(records[0].citation_status, "not_run")
        self.assertIsNone(payload[0]["closure_score"])
        self.assertIsNone(payload[0]["citation_passed"])
        self.assertEqual(payload[0]["schema_version"], "deep-research-evaluation/2.0")
        self.assertEqual(payload[0]["input_task"], {"id": "t1", "question": "Q"})
        self.assertEqual(payload[0]["artifact_status"], "unavailable")
        self.assertIsNone(payload[0]["estimated_cost_usd"])

    def test_observed_zero_and_false_are_preserved(self) -> None:
        state = SimpleNamespace(
            status="completed",
            draft_answer="expected",
            closure=SimpleNamespace(score=0.0),
            verification=SimpleNamespace(passed=False),
            counters=SimpleNamespace(search_calls=0, pages_fetched=0),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "tasks.jsonl"
            dataset.write_text(
                json.dumps(
                    {"id": "t2", "question": "Q", "answers": ["expected"]}
                )
                + "\n",
                encoding="utf-8",
            )

            record = asyncio.run(
                evaluate_jsonl(_StubEngine([state]), dataset, root / "results.json")
            )[0]

        self.assertEqual(record.closure_score, 0.0)
        self.assertEqual(record.closure_score_status, "observed")
        self.assertFalse(record.citation_passed)
        self.assertEqual(record.citation_status, "observed")

    def test_complete_record_links_trace_evidence_usage_cost_and_latency(self) -> None:
        state = SimpleNamespace(
            status="completed",
            draft_answer="The supported answer [E12345678].",
            answer_delivery={"label": "verified"},
            closure=SimpleNamespace(score=0.82, score_status="observed"),
            verification=SimpleNamespace(
                passed=True,
                items=[{"claim": "supported", "status": "entailed"}],
            ),
            evidence=[{"id": "E12345678"}],
            failures=[{"type": "fetch_failed"}, {"type": "fetch_failed"}],
            counters=SimpleNamespace(
                iterations=3,
                search_calls=4,
                pages_fetched=5,
                model_calls=6,
                model_cache_hits=1,
                input_tokens=700,
                output_tokens=80,
                estimated_cost_usd=0.0123,
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "tasks.jsonl"
            output = root / "results.json"
            run_dir = root / "eval-t3"
            (run_dir / "artifacts").mkdir(parents=True)
            (run_dir / "sources").mkdir()
            (run_dir / "final.json").write_text("{}", encoding="utf-8")
            (run_dir / "checkpoints.sqlite").write_bytes(b"sqlite")
            (run_dir / "events.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"event_type": "node_finished"}),
                        json.dumps({"event_type": "tool_finished"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            dataset.write_text(
                json.dumps(
                    {
                        "id": "t3",
                        "question": "Research question",
                        "answers": ["supported answer"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            record = asyncio.run(
                evaluate_jsonl(_StubEngine([state]), dataset, output)
            )[0]

        self.assertTrue(record.task_completed)
        self.assertTrue(record.exact_match)
        self.assertEqual(record.answer_delivery, "verified")
        self.assertEqual(record.cited_evidence_ids, ["E12345678"])
        self.assertEqual(record.evidence_count, 1)
        self.assertEqual(record.model_calls, 6)
        self.assertEqual(record.input_tokens, 700)
        self.assertEqual(record.estimated_cost_usd, 0.0123)
        self.assertEqual(record.cost_status, "estimated")
        self.assertEqual(record.event_count, 2)
        self.assertEqual(record.tool_event_count, 1)
        self.assertEqual(record.failure_types, {"fetch_failed": 2})
        self.assertEqual(record.artifact_status, "complete")
        self.assertEqual(record.artifacts["event_trace"], "eval-t3/events.jsonl")
        self.assertTrue(record.run_reused)
        self.assertEqual(record.usage_scope, "cumulative_run")
        self.assertEqual(record.latency_scope, "resume_or_replay_invocation")
        self.assertGreaterEqual(record.duration_seconds, 0.0)

    def test_one_task_failure_does_not_abort_remaining_dataset(self) -> None:
        state = SimpleNamespace(
            status="completed",
            draft_answer="second",
            closure=None,
            verification=None,
            counters=SimpleNamespace(search_calls=0, pages_fetched=0),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "tasks.jsonl"
            dataset.write_text(
                "\n".join(
                    [
                        json.dumps({"id": "first", "question": "Q1"}),
                        json.dumps({"id": "second", "question": "Q2"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            records = asyncio.run(
                evaluate_jsonl(
                    _FailingThenPassingEngine(state),
                    dataset,
                    root / "results.json",
                )
            )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].status, "failed")
        self.assertIn("RuntimeError: provider unavailable", records[0].error or "")
        self.assertEqual(records[1].status, "completed")


if __name__ == "__main__":
    unittest.main()
