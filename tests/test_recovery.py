from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from deep_research.config import AppConfig
from deep_research.contracts import AgentInvocation, ArtifactRef, canonical_artifact_bytes
from deep_research.engine import ResearchEngine
from deep_research.recovery import recovery_for
from deep_research.resume import (
    ResumePreparationError,
    prepare_crash_recovery,
    prepare_resume,
)
from deep_research.schemas import AnswerSlot, Evidence, ResearchPlan, Subgoal
from deep_research.state import ResearchState
from deep_research.storage import ExecutionFenceLostError, RunStore


class RecoveryTest(unittest.TestCase):
    def test_resume_rechecks_saved_evidence_before_spending_new_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="resume-recheck-evidence",
                question="Summarize the result",
                status="evidence_incomplete",
                next_node="done",
                suspension={"resume_node": "generate_queries"},
                plan=ResearchPlan(
                    answer_type="text",
                    slots=[AnswerSlot("answer", "Answer the question")],
                    subgoals=[Subgoal("sg-answer", "Find evidence", ["answer"], "done")],
                ),
                evidence=[
                    Evidence(
                        id="E1",
                        subgoal_id="sg-answer",
                        slot_id="answer",
                        claim="A saved claim.",
                        quote="A saved claim.",
                        source_url="https://example.org/source",
                        source_title="Saved source",
                        stance="supports",
                        reliability=0.9,
                        extraction_confidence=1.0,
                        content_hash="saved-content-hash",
                        source_cluster_id="saved-source",
                    )
                ],
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})

            prepared = prepare_resume(
                config,
                state.run_id,
                {
                    "additional_iterations": 0,
                    "additional_search_calls": 0,
                    "additional_pages": 0,
                    "recheck_saved_evidence": True,
                },
                source="manual",
                idempotency_key="manual:resume-recheck-evidence:request",
            )

            self.assertEqual(prepared.response["next_node"], "assess_closure")
            self.assertEqual(store.latest().next_node, "assess_closure")

    def test_resume_rechecks_existing_verification_candidate_without_redrafting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="resume-recheck-candidate",
                question="Summarize the result",
                status="verification_failed",
                next_node="done",
                suspension={"resume_node": "generate_queries"},
                plan=ResearchPlan(
                    answer_type="text",
                    slots=[AnswerSlot("answer", "Answer the question")],
                    subgoals=[Subgoal("sg-answer", "Find evidence", ["answer"], "done")],
                ),
                evidence_revision=2,
                draft_revision=2,
                draft_answer="Saved final answer [E1].",
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})

            prepared = prepare_resume(
                config,
                state.run_id,
                {
                    "additional_iterations": 0,
                    "additional_search_calls": 0,
                    "additional_pages": 0,
                    "recheck_saved_evidence": True,
                },
                source="manual",
                idempotency_key="manual:resume-recheck-candidate:request",
            )

            restored = store.latest()
            self.assertEqual(prepared.response["next_node"], "verify")
            self.assertEqual(restored.next_node, "verify")
            self.assertEqual(restored.draft_answer, "Saved final answer [E1].")
            self.assertTrue(restored.resume_transition["recheck_existing_answer"])

    def test_concurrent_outbox_flush_is_exactly_once_and_repairs_partial_tail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "concurrent-outbox")
            concurrent_stores = [
                RunStore(runs_dir, store.run_id) for _ in range(8)
            ]
            records = [
                {
                    "event_id": f"event-{index}",
                    "created_at": f"2026-07-18T00:00:0{index}+00:00",
                    "run_id": store.run_id,
                    "event_type": "test",
                    "node": "recovery",
                    "payload": {"index": index},
                }
                for index in range(4)
            ]
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                connection.executemany(
                    "INSERT INTO outbox(event_id, created_at, event_json) VALUES (?, ?, ?)",
                    [
                        (
                            record["event_id"],
                            record["created_at"],
                            json.dumps(record),
                        )
                        for record in records
                    ],
                )
                connection.commit()
            store.events_path.write_bytes(b'{"event_id":"crash-fragment"')
            barrier = threading.Barrier(8)

            def flush_once(concurrent_store: RunStore) -> None:
                barrier.wait()
                concurrent_store._flush_outbox()

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(flush_once, concurrent_stores))

            lines = store.events_path.read_text(encoding="utf-8").splitlines()
            published = [json.loads(line) for line in lines]
            published_ids = [item["event_id"] for item in published]
            self.assertCountEqual(published_ids, [item["event_id"] for item in records])
            self.assertEqual(len(published_ids), len(set(published_ids)))
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                unpublished = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE published_at IS NULL"
                ).fetchone()[0]
            self.assertEqual(unpublished, 0)

    def test_agui_message_snapshot_round_trips_complete_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "message-history")
            messages = [
                {"id": "u1", "role": "user", "content": "Question"},
                {"id": "a1", "role": "assistant", "content": "Draft"},
                {"id": "u2", "role": "user", "content": "Continue"},
            ]

            store.save_agui_messages("thread-history", messages)

            self.assertEqual(store.load_agui_messages("thread-history"), messages)
            audit = store.agui_message_snapshot_audit()[0]
            self.assertEqual(audit["message_ids"], ["u1", "a1", "u2"])
            self.assertEqual(audit["roles"], ["user", "assistant", "user"])

    def test_citation_error_returns_targeted_search(self) -> None:
        action = recovery_for("citation_error")
        self.assertTrue(action.retryable)
        self.assertEqual(action.next_node, "generate_queries")

    def test_unknown_error_stops_safely(self) -> None:
        action = recovery_for("unknown")
        self.assertFalse(action.retryable)
        self.assertEqual(action.next_node, "finalize")

    def test_ambiguous_operation_authorization_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "atomic-retry")
            store.begin_operation("op-1", "plan", "hash-1")
            store.begin_operation("op-2", "draft", "hash-2")

            self.assertFalse(
                store.authorize_operation_retries(["op-1", "missing-op"])
            )
            self.assertEqual(
                {item["operation_key"] for item in store.ambiguous_operations()},
                {"op-1", "op-2"},
            )

            self.assertTrue(store.authorize_operation_retries(["op-1", "op-2"]))
            self.assertEqual(store.ambiguous_operations(), [])

    def test_stale_worker_recovery_requires_confirmation_and_preserves_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="stale-worker-recovery",
                question="Continue a saved multimodal research run",
                status="running",
                next_node="ingest_evidence",
                budget_limits={"iterations": 4, "search_calls": 12, "pages": 18},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.checkpoint("ingest_evidence", state)
            store.begin_operation("uncertain-model", "extract_evidence", "input-v1")

            with self.assertRaisesRegex(ResumePreparationError, "explicit confirmation"):
                prepare_crash_recovery(
                    config,
                    state.run_id,
                    {},
                    idempotency_key="manual:stale-worker-recovery:unconfirmed",
                )

            prepared = prepare_crash_recovery(
                config,
                state.run_id,
                {"confirm_ambiguous_retry": True},
                idempotency_key="manual:stale-worker-recovery:confirmed",
            )
            restored = store.latest()
            self.assertEqual(restored.status, "initialized")
            self.assertEqual(restored.next_node, "ingest_evidence")
            self.assertEqual(restored.budget_limits, state.budget_limits)
            self.assertTrue(prepared.response["crash_recovery"])
            self.assertEqual(prepared.response["ambiguous_operations_confirmed"], 1)
            self.assertEqual(store.ambiguous_operations(), [])
            self.assertEqual(
                store.operation_detail("uncertain-model")["status"],
                "retry_authorized",
            )

            replay = prepare_crash_recovery(
                config,
                state.run_id,
                {"confirm_ambiguous_retry": True},
                idempotency_key="manual:stale-worker-recovery:confirmed",
            )
            self.assertTrue(replay.replayed)

            with self.assertRaisesRegex(ResumePreparationError, "keeps the already approved budget"):
                prepare_crash_recovery(
                    config,
                    state.run_id,
                    {
                        "confirm_ambiguous_retry": True,
                        "additional_pages": 1,
                    },
                    idempotency_key="manual:stale-worker-recovery:budget",
                )

    def test_concurrent_same_operation_has_one_provider_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stores = [RunStore(Path(tmp), "operation-barrier") for _ in range(8)]
            barrier = threading.Barrier(len(stores))
            decision_barrier = threading.Barrier(len(stores))
            provider_lock = threading.Lock()
            provider_calls = 0

            def execute(index: int) -> str:
                nonlocal provider_calls
                barrier.wait()
                operation = stores[index].begin_operation(
                    "same-key",
                    "search",
                    "same-input",
                    kind="search",
                    idempotent=True,
                )
                if operation["status"] == "new":
                    with provider_lock:
                        provider_calls += 1
                decision_barrier.wait()
                if operation["status"] == "new":
                    stores[index].complete_operation("same-key", {"ok": True})
                return str(operation["status"])

            with ThreadPoolExecutor(max_workers=len(stores)) as executor:
                statuses = list(executor.map(execute, range(len(stores))))

            self.assertEqual(provider_calls, 1)
            self.assertEqual(statuses.count("new"), 1)
            self.assertEqual(statuses.count("in_progress"), len(stores) - 1)
            row = stores[0].operation_detail("same-key")
            self.assertEqual(row["status"], "succeeded")
            self.assertEqual(row["attempt_count"], 1)

    def test_idempotent_operation_retries_only_after_owner_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = RunStore(Path(tmp), "operation-expiry")
            started = first.begin_operation(
                "expiring-key",
                "fetch",
                "same-input",
                kind="fetch",
                idempotent=True,
                lease_ms=100,
            )
            concurrent = RunStore(Path(tmp), "operation-expiry").begin_operation(
                "expiring-key",
                "fetch",
                "same-input",
                kind="fetch",
                idempotent=True,
                lease_ms=100,
            )
            self.assertEqual(started["status"], "new")
            self.assertEqual(concurrent["status"], "in_progress")

            time.sleep(0.12)
            replacement = RunStore(Path(tmp), "operation-expiry")
            retried = replacement.begin_operation(
                "expiring-key",
                "fetch",
                "same-input",
                kind="fetch",
                idempotent=True,
                lease_ms=100,
            )
            self.assertEqual(retried["status"], "new")
            self.assertEqual(retried["retry_reason"], "expired_owner_fence")
            self.assertEqual(retried["attempt_count"], 2)
            with self.assertRaises(RuntimeError):
                first.complete_operation("expiring-key", {"stale": True})
            replacement.complete_operation("expiring-key", {"fresh": True})
            self.assertEqual(
                json.loads(replacement.operation_detail("expiring-key")["result_json"]),
                {"fresh": True},
            )

    def test_resource_limit_operation_is_not_automatically_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "resource-limit-operation")
            started = store.begin_operation(
                "resource-limit-key",
                "fetch",
                "resource-limit-input",
                kind="fetch",
                idempotent=True,
            )
            self.assertEqual(started["status"], "new")
            store.fail_operation(
                "resource-limit-key",
                "resource_limit_exceeded: response exceeded the hard limit",
            )

            retry = store.begin_operation(
                "resource-limit-key",
                "fetch",
                "resource-limit-input",
                kind="fetch",
                idempotent=True,
            )
            self.assertEqual(retry["status"], "non_retryable")
            self.assertFalse(retry["retryable"])
            self.assertEqual(
                store.operation_detail("resource-limit-key")["attempt_count"],
                1,
            )

    def test_replay_links_to_successful_retry_without_rewriting_unknown_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "retry-invocation-link")
            operation_key = "retry-result-key"
            store.begin_operation(operation_key, "plan", "same-input")
            first = AgentInvocation(
                invocation_id="first-unknown",
                agent_id="planner",
                role="research_planner",
                operation="plan",
                attempt=1,
                started_at="2026-07-19T00:00:00+00:00",
                ended_at=None,
                status="running",
                input_type="ResearchQuestion",
                provider_call_count=1,
                run_id=store.run_id,
                trace_id=store.run_id,
                operation_key=operation_key,
                side_effect_status="unknown",
            )
            store.save_invocation(first, operation_key=operation_key)
            self.assertTrue(store.authorize_operation_retry(operation_key))

            retried = store.begin_operation(operation_key, "plan", "same-input")
            self.assertEqual(retried["status"], "new")
            second = AgentInvocation(
                invocation_id="second-success",
                agent_id="planner",
                role="research_planner",
                operation="plan",
                attempt=2,
                started_at="2026-07-19T00:00:01+00:00",
                ended_at=None,
                status="running",
                input_type="ResearchQuestion",
                provider_call_count=1,
                run_id=store.run_id,
                trace_id=store.run_id,
                operation_key=operation_key,
                side_effect_status="unknown",
            )
            store.save_invocation(second, operation_key=operation_key)
            store.complete_operation(operation_key, {"answer": "persisted"})
            second.status = "succeeded"
            second.ended_at = "2026-07-19T00:00:02+00:00"
            second.side_effect_status = "committed"
            store.save_invocation(second, operation_key=operation_key)

            detail = store.operation_detail(operation_key)
            self.assertEqual(detail["original_invocation_id"], first.invocation_id)
            self.assertEqual(detail["result_invocation_id"], second.invocation_id)
            state = ResearchState(run_id=store.run_id, question="Replay")
            replay = ResearchEngine._record_replay_invocation(
                store,
                state,
                "plan",
                operation_key,
            )
            self.assertEqual(replay.replay_of_invocation_id, second.invocation_id)
            self.assertEqual(
                store.invocation(first.invocation_id).side_effect_status,
                "unknown",
            )

    def test_shared_resume_preparation_preserves_budget_and_paid_retry_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            run_id = "shared-resume"
            store = RunStore(config.runs_dir, run_id)
            state = ResearchState(
                run_id=run_id,
                question="Research a missing fact",
                status="evidence_incomplete",
                next_node="done",
                budget_limits={
                    "iterations": 3,
                    "search_calls": 8,
                    "pages": 12,
                },
                suspension={
                    "reason": "evidence_incomplete",
                    "resume_node": "plan",
                },
            )
            store.commit_stage("finalize", state, "run_finished", {})
            store.begin_operation("paid-1", "plan", "hash-1")
            store.begin_operation("paid-2", "plan", "hash-2")

            with self.assertRaises(ResumePreparationError):
                prepare_resume(
                    config,
                    run_id,
                    {},
                    source="manual",
                    idempotency_key="manual:shared-resume:first",
                )
            self.assertEqual(len(store.ambiguous_operations()), 2)

            prepared = prepare_resume(
                config,
                run_id,
                {
                    "confirm_ambiguous_retry": True,
                    "additional_iterations": 2,
                    "additional_search_calls": 4,
                    "additional_pages": 6,
                },
                source="manual",
                idempotency_key="manual:shared-resume:confirmed",
            )
            restored = store.latest()
            self.assertEqual(prepared.budget_limits["iterations"], 5)
            self.assertEqual(prepared.budget_limits["search_calls"], 12)
            self.assertEqual(prepared.budget_limits["pages"], 18)
            self.assertEqual(prepared.response["ambiguous_operations_confirmed"], 2)
            self.assertEqual(store.ambiguous_operations(), [])
            self.assertEqual(restored.status, "initialized")
            self.assertEqual(restored.next_node, "plan")

            replayed = prepare_resume(
                config,
                run_id,
                {
                    "confirm_ambiguous_retry": True,
                    "additional_iterations": 2,
                    "additional_search_calls": 4,
                    "additional_pages": 6,
                },
                source="manual",
                idempotency_key="manual:shared-resume:confirmed",
            )
            self.assertTrue(replayed.replayed)
            self.assertEqual(replayed.budget_limits, prepared.budget_limits)

    def test_resume_rejects_non_boolean_ambiguous_retry_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="strict-confirm",
                question="Reject ambiguous confirmation coercion",
                status="evidence_incomplete",
                suspension={"resume_node": "plan"},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})

            for value in ("false", "true", 0, 1, None):
                with self.subTest(value=value):
                    with self.assertRaisesRegex(
                        ResumePreparationError,
                        "confirm_ambiguous_retry must be a boolean",
                    ):
                        prepare_resume(
                            config,
                            state.run_id,
                            {"confirm_ambiguous_retry": value},
                            source="manual",
                            idempotency_key=(
                                f"manual:strict-confirm:{type(value).__name__}"
                            ),
                        )

    def test_persisted_budget_is_not_raised_by_new_environment_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            config.budget.max_iterations = 9
            config.budget.max_search_calls = 20
            config.budget.max_pages = 30
            state = ResearchState(
                run_id="budget-drift",
                question="Keep the approved budget",
                status="evidence_incomplete",
                next_node="done",
                budget_limits={
                    "iterations": 3,
                    "search_calls": 8,
                    "pages": 12,
                },
                suspension={"resume_node": "plan"},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})

            prepared = prepare_resume(
                config,
                state.run_id,
                {
                    "additional_iterations": 0,
                    "additional_search_calls": 0,
                    "additional_pages": 0,
                },
                source="manual",
                idempotency_key="manual:budget-drift:request",
            )
            self.assertEqual(
                prepared.budget_limits,
                {"iterations": 3, "search_calls": 8, "pages": 12},
            )

    def test_resume_cannot_cross_a_persisted_budget_ceiling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            config.budget.max_iterations = 50
            config.budget.max_search_calls = 50
            config.budget.max_pages = 50
            config.budget.max_total_iterations = 100
            config.budget.max_total_search_calls = 100
            config.budget.max_total_pages = 100
            state = ResearchState(
                run_id="persisted-budget-ceiling",
                question="Do not cross the approved ceiling",
                status="evidence_incomplete",
                next_node="done",
                budget_limits={
                    "iterations": 3,
                    "search_calls": 8,
                    "pages": 12,
                },
                budget_ceilings={
                    "iterations": 4,
                    "search_calls": 8,
                    "pages": 12,
                },
                suspension={"resume_node": "plan"},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})

            with self.assertRaisesRegex(
                ResumePreparationError,
                "persisted per-run ceiling",
            ) as raised:
                prepare_resume(
                    config,
                    state.run_id,
                    {
                        "additional_iterations": 2,
                        "additional_search_calls": 0,
                        "additional_pages": 0,
                    },
                    source="manual",
                    idempotency_key="manual:persisted-budget-ceiling:request",
                )
            self.assertEqual(
                raised.exception.details["budget_ceiling"]["iterations"],
                4,
            )

    def test_resume_commit_rejects_stale_checkpoint_and_partial_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "resume-cas")
            stale = ResearchState(
                run_id="resume-cas",
                question="CAS",
                status="evidence_incomplete",
                next_node="done",
            )
            store.commit_stage("first", stale, "saved", {})
            checkpoint_id, _ = store.latest_with_id()
            store.begin_operation("op-1", "plan", "hash-1")
            store.begin_operation("op-2", "plan", "hash-2")

            partial = store.commit_resume(
                stale,
                expected_checkpoint_id=checkpoint_id,
                idempotency_key="manual:resume-cas:partial",
                command_hash="partial-hash",
                source="manual",
                thread_id=None,
                protocol_run_id=None,
                payload={},
                confirmed_operation_keys=["op-1"],
                interrupt_responses=[],
            )
            self.assertEqual(partial["status"], "conflict")
            self.assertEqual(len(store.ambiguous_operations()), 2)

            newer = store.latest()
            newer.counters.search_calls = 7
            store.commit_stage("newer", newer, "saved", {})
            stale.status = "initialized"
            stale.next_node = "plan"
            stale_result = store.commit_resume(
                stale,
                expected_checkpoint_id=checkpoint_id,
                idempotency_key="manual:resume-cas:stale",
                command_hash="stale-hash",
                source="manual",
                thread_id=None,
                protocol_run_id=None,
                payload={},
                confirmed_operation_keys=["op-1", "op-2"],
                interrupt_responses=[],
            )
            self.assertEqual(stale_result["status"], "conflict")
            self.assertEqual(store.latest().counters.search_calls, 7)

    def test_agui_resume_requires_new_external_run_and_validates_optional_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="agui-resume",
                question="Resume through AG-UI",
                status="evidence_incomplete",
                next_node="done",
                suspension={
                    "reason": "evidence_incomplete",
                    "resume_node": "plan",
                },
                budget_limits={
                    "iterations": 3,
                    "search_calls": 8,
                    "pages": 12,
                },
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})
            interrupt_id = store.create_agui_interrupt(
                "thread-1",
                "parent-run-1",
                "evidence_incomplete",
            )
            response = {
                "interrupt_id": interrupt_id,
                "status": "resolved",
                "payload": {"additionalIterations": 1},
            }

            with self.assertRaisesRegex(
                ResumePreparationError,
                "parentRunId",
            ):
                prepare_resume(
                    config,
                    state.run_id,
                    {"additional_iterations": 1},
                    source="agui",
                    idempotency_key="agui:wrong-parent:request",
                    thread_id="thread-1",
                    protocol_run_id="resume-run-1",
                    parent_run_id="wrong-parent",
                    interrupt_responses=[response],
                )

            with self.assertRaisesRegex(
                ResumePreparationError,
                "new external AG-UI runId",
            ):
                prepare_resume(
                    config,
                    state.run_id,
                    {"additional_iterations": 1},
                    source="agui",
                    idempotency_key="agui:same-external-run:request",
                    thread_id="thread-1",
                    protocol_run_id="parent-run-1",
                    parent_run_id=None,
                    interrupt_responses=[response],
                )

            prepared = prepare_resume(
                config,
                state.run_id,
                {"additional_iterations": 1},
                source="agui",
                idempotency_key="agui:correct-parent:request",
                thread_id="thread-1",
                protocol_run_id="resume-run-1",
                parent_run_id=None,
                interrupt_responses=[response],
            )
            self.assertFalse(prepared.replayed)
            self.assertTrue(prepared.should_start_worker)
            self.assertEqual(store.open_agui_interrupts(), [])

    def test_agui_cancelled_interrupt_is_consumed_without_worker_or_checkpoint_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="agui-cancel-interrupt",
                question="Cancel the interrupt",
                status="evidence_incomplete",
                suspension={"resume_node": "plan"},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})
            checkpoint_before, _ = store.latest_with_id()
            interrupt_id = store.create_agui_interrupt(
                "thread-cancel",
                "external-run-cancelled",
                "evidence_incomplete",
            )
            prepared = prepare_resume(
                config,
                state.run_id,
                {},
                source="agui",
                idempotency_key="agui:cancel-interrupt:request",
                thread_id="thread-cancel",
                protocol_run_id="external-run-ack",
                parent_run_id="external-run-cancelled",
                interrupt_responses=[
                    {
                        "interrupt_id": interrupt_id,
                        "status": "cancelled",
                        "payload": {},
                    }
                ],
            )
            checkpoint_after, latest_state = store.latest_with_id()

            self.assertFalse(prepared.should_start_worker)
            self.assertEqual(checkpoint_before, checkpoint_after)
            self.assertEqual(latest_state.status, "evidence_incomplete")
            self.assertEqual(store.open_agui_interrupts(), [])
            self.assertEqual(prepared.response["status"], "interrupts_cancelled")

    def test_concurrent_same_resume_is_one_commit_plus_one_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="concurrent-resume",
                question="Concurrent resume",
                status="evidence_incomplete",
                next_node="done",
                suspension={"resume_node": "plan"},
                budget_limits={
                    "iterations": 3,
                    "search_calls": 8,
                    "pages": 12,
                },
            )
            RunStore(config.runs_dir, state.run_id).commit_stage(
                "finalize",
                state,
                "run_finished",
                {},
            )

            def resume_once():
                return prepare_resume(
                    config,
                    state.run_id,
                    {"additional_iterations": 1},
                    source="manual",
                    idempotency_key="manual:concurrent-resume:same-request",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: resume_once(), range(2)))

            self.assertEqual(sum(not item.replayed for item in results), 1)
            self.assertEqual(sum(item.replayed for item in results), 1)
            self.assertEqual(
                {item.response["resume_receipt_id"] for item in results},
                {"manual:concurrent-resume:same-request"},
            )
            self.assertEqual(
                RunStore(config.runs_dir, state.run_id).latest().budget_limits[
                    "iterations"
                ],
                4,
            )

    def test_resume_execution_claim_can_be_reclaimed_or_released_after_startup_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(runs_dir=Path(tmp))
            state = ResearchState(
                run_id="resume-claim-recovery",
                question="Recover a claimed resume",
                status="evidence_incomplete",
                suspension={"resume_node": "plan"},
            )
            store = RunStore(config.runs_dir, state.run_id)
            store.commit_stage("finalize", state, "run_finished", {})
            prepared = prepare_resume(
                config,
                state.run_id,
                {},
                source="manual",
                idempotency_key="manual:resume-claim-recovery:request",
            )
            first = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=1_000,
            )
            self.assertIsNotNone(first)
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=first["owner_token"],
                    fence=first["fence"],
                )
            )
            self.assertFalse(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=first["owner_token"],
                    fence=first["fence"],
                )
            )
            self.assertFalse(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    allow_reclaim=True,
                    owner_token=first["owner_token"],
                    fence=first["fence"],
                )
            )

            self.assertTrue(
                store.release_execution_lease(
                    first["owner_token"],
                    first["fence"],
                )
            )
            self.assertTrue(
                store.release_resume_execution_claim(
                    prepared.idempotency_key,
                    owner_token=first["owner_token"],
                    fence=first["fence"],
                )
            )

            second = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=1_000,
            )
            self.assertIsNotNone(second)
            self.assertGreater(second["fence"], first["fence"])
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=second["owner_token"],
                    fence=second["fence"],
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    second["owner_token"],
                    second["fence"],
                )
            )

            third = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=1_000,
            )
            self.assertIsNotNone(third)
            self.assertGreater(third["fence"], second["fence"])
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    allow_reclaim=True,
                    owner_token=third["owner_token"],
                    fence=third["fence"],
                )
            )
            self.assertFalse(
                store.release_resume_execution_claim(
                    prepared.idempotency_key,
                    owner_token=second["owner_token"],
                    fence=second["fence"],
                )
            )
            self.assertFalse(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    allow_reclaim=True,
                    owner_token=third["owner_token"],
                    fence=third["fence"],
                )
            )
            self.assertTrue(
                store.release_resume_execution_claim(
                    prepared.idempotency_key,
                    owner_token=third["owner_token"],
                    fence=third["fence"],
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    third["owner_token"],
                    third["fence"],
                )
            )
            fourth = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=1_000,
            )
            self.assertIsNotNone(fourth)
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=fourth["owner_token"],
                    fence=fourth["fence"],
                )
            )
            self.assertTrue(
                store.finish_resume_execution(
                    prepared.idempotency_key,
                    owner_token=fourth["owner_token"],
                    fence=fourth["fence"],
                    status="completed",
                    durable_run_status="evidence_incomplete",
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    fourth["owner_token"],
                    fourth["fence"],
                )
            )
            fifth = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=1_000,
            )
            self.assertIsNotNone(fifth)
            self.assertFalse(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    allow_reclaim=True,
                    owner_token=fifth["owner_token"],
                    fence=fifth["fence"],
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    fifth["owner_token"],
                    fifth["fence"],
                )
            )
            replay = prepare_resume(
                config,
                state.run_id,
                {},
                source="manual",
                idempotency_key=prepared.idempotency_key,
            )
            self.assertTrue(replay.replayed)
            self.assertFalse(replay.should_start_worker)
            self.assertEqual(replay.response["execution_status"], "completed")
            self.assertEqual(
                replay.response["durable_run_status"], "evidence_incomplete"
            )
            receipt_audit = store.resume_receipt_audit()[0]
            self.assertEqual(receipt_audit["execution_status"], "completed")
            self.assertFalse(receipt_audit["execution_claimed"])
            self.assertEqual(receipt_audit["claim_fence"], 0)
            self.assertEqual(
                receipt_audit["durable_run_status"], "evidence_incomplete"
            )
            self.assertEqual(
                store.resume_receipt(prepared.idempotency_key)["durable_run_status"],
                "evidence_incomplete",
            )
            self.assertEqual(
                [item["to_status"] for item in receipt_audit["transitions"]],
                [
                    "pending",
                    "running",
                    "startup_failed",
                    "running",
                    "running",
                    "startup_failed",
                    "running",
                    "completed",
                ],
            )

    def test_resume_receipt_migration_adds_durable_run_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_dir = runs_dir / "legacy-resume-receipt"
            run_dir.mkdir(parents=True)
            database = run_dir / "checkpoints.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE resume_receipts (
                        idempotency_key TEXT PRIMARY KEY,
                        command_hash TEXT NOT NULL,
                        source TEXT NOT NULL,
                        thread_id TEXT,
                        protocol_run_id TEXT,
                        parent_protocol_run_id TEXT,
                        checkpoint_id_before INTEGER NOT NULL,
                        checkpoint_id_after INTEGER NOT NULL,
                        response_json TEXT NOT NULL,
                        execution_claimed INTEGER NOT NULL DEFAULT 0,
                        claim_owner_token TEXT,
                        claim_fence INTEGER NOT NULL DEFAULT 0,
                        claim_expires_at_ms INTEGER,
                        execution_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                        execution_started_at TEXT,
                        execution_completed_at TEXT,
                        execution_error TEXT,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE resume_execution_transitions (
                        transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        idempotency_key TEXT NOT NULL,
                        from_status TEXT NOT NULL,
                        to_status TEXT NOT NULL,
                        owner_fence INTEGER,
                        owner_token_fingerprint TEXT,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()

            store = RunStore(runs_dir, run_dir.name)
            with closing(sqlite3.connect(database)) as connection, connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(resume_receipts)"
                    )
                }
                transition_columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(resume_execution_transitions)"
                    )
                }
                transition_indexes = {
                    row[1]: bool(row[2])
                    for row in connection.execute(
                        "PRAGMA index_list(resume_execution_transitions)"
                    )
                }
            self.assertIn("durable_run_status", columns)
            self.assertTrue(
                {
                    "transition_key",
                    "transition_kind",
                    "handoff_message_id",
                    "agent_invocation_id",
                    "agent_id",
                    "operation",
                    "superseded_handoff_message_id",
                }.issubset(transition_columns)
            )
            self.assertTrue(transition_indexes["resume_transition_key_idx"])
            self.assertEqual(store.resume_receipt_audit(), [])

    def test_execution_lease_has_owner_fence_heartbeat_and_stale_reclaim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "execution-lease")
            clock = time.time()
            with patch("deep_research.storage.time.time", return_value=clock):
                first = store.acquire_execution_lease("receipt-1", ttl_ms=40)

                self.assertIsNotNone(first)
                self.assertTrue(store.execution_lease_audit()["active"])
                self.assertIsNone(store.acquire_execution_lease("receipt-2", ttl_ms=40))
                self.assertFalse(
                    store.heartbeat_execution_lease(
                        "wrong-owner",
                        first["fence"],
                        ttl_ms=40,
                    )
                )
                self.assertTrue(
                    store.heartbeat_execution_lease(
                        first["owner_token"],
                        first["fence"],
                        ttl_ms=40,
                    )
                )
                self.assertFalse(
                    store.release_execution_lease("wrong-owner", first["fence"])
                )
            with patch("deep_research.storage.time.time", return_value=clock + 0.041):
                second = store.acquire_execution_lease("receipt-2", ttl_ms=40)

                self.assertIsNotNone(second)
                self.assertGreater(second["fence"], first["fence"])
                self.assertNotEqual(second["owner_token"], first["owner_token"])
                self.assertFalse(
                    store.release_execution_lease(
                        first["owner_token"],
                        first["fence"],
                    )
                )
                self.assertTrue(
                    store.release_execution_lease(
                        second["owner_token"],
                        second["fence"],
                    )
                )

    def test_usage_ledger_preserves_evidence_and_pricing_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            empty = RunStore(root, "usage-empty")
            self.assertEqual(empty.usage_totals()["usage_status"], "unavailable")

            mock = RunStore(root, "usage-mock")
            mock.begin_operation("mock-operation", "plan", "mock-input")
            mock.complete_operation(
                "mock-operation",
                {},
                {
                    "model_calls": 0,
                    "model_cache_hits": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                    "provider": "MockModelProvider",
                    "usage_snapshot_available": False,
                },
            )
            mock_usage = mock.usage_totals()
            self.assertEqual(mock_usage["usage_status"], "not_applicable")
            self.assertEqual(mock_usage["provider"], "MockModelProvider")
            self.assertEqual(mock_usage["pricing_status"], "not_applicable")

            unavailable = RunStore(root, "usage-deepseek-unavailable")
            unavailable.begin_operation("missing-usage", "plan", "missing-input")
            unavailable.complete_operation(
                "missing-usage",
                {},
                {
                    "provider": "DeepSeekModelProvider",
                    "usage_snapshot_available": False,
                },
            )
            missing_usage = unavailable.usage_totals()
            self.assertEqual(missing_usage["usage_status"], "unavailable")
            self.assertEqual(missing_usage["pricing_status"], "unavailable")

            unavailable.begin_operation("measured-usage", "draft", "measured-input")
            unavailable.complete_operation(
                "measured-usage",
                {},
                {
                    "model_calls": 1,
                    "model_cache_hits": 0,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "provider": "DeepSeekModelProvider",
                },
            )
            mixed_usage = unavailable.usage_totals()
            self.assertEqual(mixed_usage["usage_status"], "partial")
            self.assertNotEqual(mixed_usage["usage_status"], "complete")

            complete = RunStore(root, "usage-deepseek-complete")
            complete.begin_operation("deepseek-complete", "plan", "complete-input")
            complete.complete_operation(
                "deepseek-complete",
                {},
                {
                    "model_calls": 1,
                    "model_cache_hits": 0,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "estimated_cost_usd": 0.0,
                    "provider": "DeepSeekModelProvider",
                },
            )
            complete_usage = complete.usage_totals()
            self.assertEqual(complete_usage["usage_status"], "complete")
            self.assertEqual(complete_usage["input_tokens"], 120)
            self.assertEqual(complete_usage["output_tokens"], 30)
            self.assertEqual(complete_usage["pricing_status"], "unavailable")
            self.assertIn("not configured", complete_usage["pricing_reason"])

            partial = RunStore(root, "usage-deepseek-partial")
            partial.begin_operation("deepseek-partial", "plan", "partial-input")
            partial.complete_operation(
                "deepseek-partial",
                {},
                {
                    "model_calls": 1,
                    "input_tokens": 120,
                    "estimated_cost_usd": 0.0,
                    "provider": "DeepSeekModelProvider",
                },
            )
            self.assertEqual(partial.usage_totals()["usage_status"], "partial")

            legacy = RunStore(root, "usage-legacy")
            legacy_state = ResearchState(
                run_id=legacy.run_id,
                question="Legacy usage",
            )
            legacy_state.counters.model_calls = 2
            legacy_state.counters.input_tokens = 50
            legacy.ensure_legacy_usage_baseline(legacy_state.counters)
            legacy_usage = legacy.usage_totals()
            self.assertEqual(legacy_usage["usage_status"], "partial")
            self.assertEqual(legacy_usage["provider"], "legacy_unknown")
            self.assertEqual(legacy_usage["pricing_status"], "unavailable")

    def test_usage_settlement_is_visible_before_completion_and_survives_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "live-usage-settlement")
            operation_key = "paid-plan"
            store.begin_operation(operation_key, "plan", "input-v1")
            first_usage = {
                "model_calls": 1,
                "model_cache_hits": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.0012,
                "provider": "openai-compatible",
                "pricing_configured": True,
                "pricing_status": "complete",
            }

            self.assertTrue(store.settle_model_usage(operation_key, first_usage))
            self.assertFalse(store.settle_model_usage(operation_key, first_usage))
            live_usage = store.usage_totals()
            self.assertEqual(live_usage["model_calls"], 1)
            self.assertEqual(live_usage["input_tokens"], 100)
            self.assertAlmostEqual(live_usage["estimated_cost_usd"], 0.0012)
            self.assertEqual(live_usage["pending_model_operations"], 0)
            self.assertEqual(live_usage["settled_model_operations"], 1)
            self.assertIsNotNone(live_usage["updated_at"])
            self.assertIsNotNone(
                store.operation_detail(operation_key)["usage_settled_at"]
            )

            # A response may be billed even if local result processing fails.
            # A later retry must add its own measured usage rather than replace
            # the first settlement.
            store.fail_operation(operation_key, "synthetic result processing failure")
            retry = store.begin_operation(operation_key, "plan", "input-v1")
            self.assertEqual(retry["attempt_count"], 2)
            second_usage = {**first_usage, "input_tokens": 50, "output_tokens": 10, "estimated_cost_usd": 0.0006}
            self.assertTrue(store.settle_model_usage(operation_key, second_usage))
            store.complete_operation(operation_key, {"answer": "saved"}, second_usage)

            final_usage = store.usage_totals()
            self.assertEqual(final_usage["model_calls"], 2)
            self.assertEqual(final_usage["input_tokens"], 150)
            self.assertEqual(final_usage["output_tokens"], 30)
            self.assertAlmostEqual(final_usage["estimated_cost_usd"], 0.0018)
            self.assertEqual(final_usage["ledger_entry_count"], 2)
            self.assertEqual(final_usage["pending_model_operations"], 0)
            self.assertEqual(final_usage["settled_model_operations"], 0)

    def test_response_usage_events_update_totals_before_operation_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "live-response-settlement")
            operation_key = "multi-response-perception"
            store.begin_operation(operation_key, "perceive_inputs", "input-v1")
            first_response = {
                "model_calls": 1,
                "model_cache_hits": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.0012,
                "provider": "gpt",
                "pricing_configured": True,
                "pricing_status": "complete",
            }
            second_response = {
                **first_response,
                "input_tokens": 60,
                "output_tokens": 10,
                "estimated_cost_usd": 0.00075,
            }

            self.assertTrue(store.record_model_usage_event(operation_key, first_response))
            first_total = store.usage_totals()
            self.assertEqual(first_total["model_calls"], 1)
            self.assertEqual(first_total["input_tokens"], 100)
            self.assertAlmostEqual(first_total["estimated_cost_usd"], 0.0012)
            self.assertEqual(first_total["settled_model_responses"], 1)
            self.assertEqual(first_total["settled_model_operations"], 1)
            self.assertEqual(first_total["usage_revision"], 1)
            self.assertEqual(first_total["latest_entry"]["provider"], "gpt")

            self.assertTrue(store.record_model_usage_event(operation_key, second_response))
            second_total = store.usage_totals()
            self.assertEqual(second_total["model_calls"], 2)
            self.assertEqual(second_total["input_tokens"], 160)
            self.assertEqual(second_total["output_tokens"], 30)
            self.assertAlmostEqual(second_total["estimated_cost_usd"], 0.00195)
            self.assertEqual(second_total["ledger_entry_count"], 2)
            self.assertEqual(second_total["settled_model_responses"], 2)
            self.assertEqual(second_total["usage_revision"], 2)
            self.assertEqual(
                second_total["provider_breakdown"],
                [
                    {
                        "provider": "gpt",
                        "model_calls": 2,
                        "input_tokens": 160,
                        "output_tokens": 30,
                        "estimated_cost_usd": 0.00195,
                        "ledger_entry_count": 2,
                        "updated_at": second_total["updated_at"],
                        "usage_status": "complete",
                        "pricing_status": "complete",
                        "usage_reason": "Prompt and completion token counts were returned by the provider.",
                        "pricing_reason": "Pricing status supplied by the provider integration.",
                    }
                ],
            )

            # The end-of-operation snapshot is retained for recovery, but the
            # response records remain the cost source and must not be added twice.
            aggregate = {
                **first_response,
                "model_calls": 2,
                "input_tokens": 160,
                "output_tokens": 30,
                "estimated_cost_usd": 0.00195,
            }
            self.assertTrue(store.settle_model_usage(operation_key, aggregate))
            after_summary = store.usage_totals()
            self.assertEqual(after_summary["model_calls"], 2)
            self.assertEqual(after_summary["input_tokens"], 160)
            self.assertAlmostEqual(after_summary["estimated_cost_usd"], 0.00195)
            detail = store.operation_detail(operation_key)
            self.assertEqual(detail["live_usage_settlement_count"], 2)
            self.assertEqual(detail["model_calls"], 2)

    def test_usage_summary_corrects_a_partial_live_receipt_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "usage-receipt-reconciliation")
            operation_key = "multi-call-plan"
            store.begin_operation(operation_key, "plan", "input-v1")
            first_response = {
                "model_calls": 1,
                "model_cache_hits": 0,
                "input_tokens": 100,
                "output_tokens": 20,
                "estimated_cost_usd": 0.0012,
                "provider": "gpt",
                "pricing_configured": True,
                "pricing_status": "complete",
            }
            complete_summary = {
                **first_response,
                "model_calls": 2,
                "input_tokens": 160,
                "output_tokens": 30,
                "estimated_cost_usd": 0.00195,
            }

            # Simulate a response whose immediate write was delayed. The final
            # provider summary must correct the subtotal instead of letting
            # one surviving receipt hide the missing charge.
            self.assertTrue(store.record_model_usage_event(operation_key, first_response))
            self.assertTrue(store.settle_model_usage(operation_key, complete_summary))

            totals = store.usage_totals()
            self.assertEqual(totals["model_calls"], 2)
            self.assertEqual(totals["input_tokens"], 160)
            self.assertEqual(totals["output_tokens"], 30)
            self.assertAlmostEqual(totals["estimated_cost_usd"], 0.00195)
            self.assertEqual(totals["reconciled_model_operations"], 1)
            self.assertEqual(totals["ledger_entry_count"], 1)
            self.assertEqual(totals["latest_entry"]["accounting_source"], "operation_summary")

    def test_reclaimed_worker_resumes_nonterminal_checkpoint_and_fences_old_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "nonterminal-recovery")
            clock = time.time()
            state = ResearchState(
                run_id=store.run_id,
                question="Recover planning",
                status="planning",
                next_node="plan",
            )
            with patch("deep_research.storage.time.time", return_value=clock):
                first = store.acquire_execution_lease("first-worker", ttl_ms=30)
                store.bind_execution_fence(first["owner_token"], first["fence"])
                store.checkpoint("plan", state)
                unbound_store = RunStore(runs_dir, store.run_id)
                with self.assertRaises(ExecutionFenceLostError):
                    unbound_store.checkpoint("unbound-writer", state)

            with patch("deep_research.storage.time.time", return_value=clock + 0.031):
                second_store = RunStore(runs_dir, store.run_id)
                second = second_store.acquire_execution_lease(
                    "crash-recovery", ttl_ms=1000
                )
                self.assertGreater(second["fence"], first["fence"])
                recovered = second_store.latest()
                self.assertEqual(recovered.status, "planning")
                self.assertEqual(recovered.next_node, "plan")

                state.status = "failed"
                with self.assertRaises(ExecutionFenceLostError):
                    store.checkpoint("stale-worker", state)
                artifact_payload = {"run_id": store.run_id, "status": "stale"}
                artifact_content = canonical_artifact_bytes(artifact_payload)
                artifact = ArtifactRef(
                    artifact_id="Astalefence",
                    kind="research/test",
                    revision=1,
                    checksum=hashlib.sha256(artifact_content).hexdigest(),
                    producer="test",
                    content_uri="artifacts/Astalefence.json",
                    byte_length=len(artifact_content),
                )
                with self.assertRaises(ExecutionFenceLostError):
                    store.write_artifact(artifact, artifact_payload)
                self.assertFalse(
                    (store.run_dir / "artifacts/Astalefence.json").exists()
                )
                self.assertEqual(second_store.latest().status, "planning")

    def test_artifact_metadata_and_canonicalization_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "artifact-integrity")
            payload = {"run_id": store.run_id, "value": "canonical"}
            content = canonical_artifact_bytes(payload)
            artifact = ArtifactRef(
                artifact_id="Aintegrity",
                kind="research/test",
                revision=1,
                checksum=hashlib.sha256(content).hexdigest(),
                producer="test",
                content_uri="artifacts/Aintegrity.json",
                byte_length=len(content),
            )
            store.write_artifact(artifact, payload)
            self.assertEqual(store.read_artifact("Aintegrity")["canonical_json"], content.decode())
            with self.assertRaisesRegex(RuntimeError, "duplicate artifact id"):
                store.write_artifact(artifact, payload)

            metadata_path = store.run_dir / "artifacts/Aintegrity.meta.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["canonicalization"] = "unverified-format"
            metadata_path.write_text(json.dumps(metadata))
            with self.assertRaisesRegex(RuntimeError, "canonicalization"):
                store.read_artifact("Aintegrity")

    def test_artifact_with_missing_parent_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "artifact-parent")
            payload = {"run_id": store.run_id, "value": "child"}
            content = canonical_artifact_bytes(payload)
            child = ArtifactRef(
                artifact_id="Achild",
                kind="research/test",
                revision=2,
                checksum=hashlib.sha256(content).hexdigest(),
                producer="test",
                content_uri="artifacts/Achild.json",
                byte_length=len(content),
                parent_artifact_id="Amissing",
            )
            with self.assertRaisesRegex(RuntimeError, "parent is missing"):
                store.write_artifact(child, payload)
            self.assertFalse((store.run_dir / "artifacts/Achild.json").exists())

    def test_verified_legacy_artifact_is_migrated_as_readable_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = RunStore(root, "legacy-artifact")
            directory = store.run_dir / "artifacts"
            directory.mkdir()
            payload = {"run_id": store.run_id, "value": "legacy"}
            content = canonical_artifact_bytes(payload)
            legacy = {
                "artifact_id": "Alegacy",
                "kind": "research/legacy",
                "revision": 1,
                "checksum": hashlib.sha256(content).hexdigest(),
                "producer": "legacy",
                "content_uri": "artifacts/Alegacy.json",
                "byte_length": len(content),
                "media_type": "application/json",
                "canonicalization": "json-sort-keys-utf8-v1",
            }
            (directory / "Alegacy.json").write_bytes(content)
            (directory / "Alegacy.meta.json").write_text(json.dumps(legacy))

            migrated = RunStore(root, "legacy-artifact")
            self.assertEqual(migrated.load_artifact_ref("Alegacy").artifact_id, "Alegacy")
            audit = next(
                item
                for item in migrated.artifact_manifest_audit()
                if item["artifact_id"] == "Alegacy"
            )
            self.assertEqual(audit["status"], "legacy_verified")
            self.assertFalse(audit["manifest_valid"])

            child_payload = {"run_id": migrated.run_id, "value": "child"}
            child_content = canonical_artifact_bytes(child_payload)
            child = ArtifactRef(
                artifact_id="Alegacychild",
                kind="research/test",
                revision=2,
                checksum=hashlib.sha256(child_content).hexdigest(),
                producer="legacy",
                content_uri="artifacts/Alegacychild.json",
                byte_length=len(child_content),
                parent_artifact_id="Alegacy",
            )
            migrated.write_artifact(child, child_payload)
            self.assertEqual(
                migrated.load_artifact_ref("Alegacychild").parent_artifact_id,
                "Alegacy",
            )


if __name__ == "__main__":
    unittest.main()
