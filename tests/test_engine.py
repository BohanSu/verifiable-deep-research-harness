import asyncio
from contextlib import closing
import json
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from deep_research.config import AppConfig
from deep_research.contracts import AgentInvocation, HandoffEnvelope
from deep_research.engine import (
    AmbiguousOperationError,
    ResearchEngine,
    _claim_quote_consistency,
    _claim_target_relevance,
    _claim_target_relevance_variants,
    _compose_evidence_limited_answer,
    _query_similarity,
    _required_query_coverage_subgoals,
    _round_robin_results,
)
from deep_research.providers import MockModelProvider, ReplaySearchProvider
from deep_research.providers.base import ProviderRequestNotSent
from deep_research.resume import prepare_resume
from deep_research.schemas import (
    AttachmentObservation,
    AnswerSlot,
    ClosureReport,
    ContradictionAudit,
    Evidence,
    EvidenceGap,
    GroundedObservation,
    Query,
    ResearchPlan,
    SearchResult,
    SlotGateAudit,
    SourceRecord,
    Subgoal,
    VerificationReport,
)
from deep_research.state import ResearchState
from deep_research.storage import (
    ExecutionFenceLostError,
    HandoffValidationError,
    RunStore,
)


class EngineTest(unittest.IsolatedAsyncioTestCase):
    async def test_resume_emits_targeted_control_handoff_before_agent_consumption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 0
            first = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="resume-control-handoff")
            self.assertEqual(first.status, "evidence_incomplete")
            store = RunStore(config.runs_dir, first.run_id)
            terminal_handoff = store.handoff_audit()[-1]["envelope"]
            self.assertEqual(terminal_handoff["intended_consumer"], "user")

            prepared = prepare_resume(
                config,
                first.run_id,
                {"additional_iterations": 2},
                source="manual",
                idempotency_key="manual:resume-control-handoff:request",
            )
            lease = store.acquire_execution_lease(prepared.idempotency_key, ttl_ms=30_000)
            self.assertIsNotNone(lease)
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=lease["owner_token"],
                    fence=lease["fence"],
                )
            )
            resumed = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=lease,
            ).run(first.question, run_id=first.run_id)

            handoffs = store.handoff_audit()
            resume_handoff = next(
                item["envelope"]
                for item in handoffs
                if item["envelope"]["route_target"] == "generate_queries"
                and item["envelope"]["producer"] == "orchestrator"
                and item["envelope"]["message_id"]
                == resumed.resume_transition.get("handoff_message_id")
            )
            self.assertEqual(resume_handoff["intended_consumer"], "scout")
            self.assertNotEqual(
                resume_handoff["message_id"], terminal_handoff["message_id"]
            )
            consuming_invocation = next(
                item
                for item in resumed.agent_invocations
                if resume_handoff["message_id"]
                in item.consumed_handoff_message_ids
            )
            self.assertEqual(consuming_invocation.agent_id, "scout")
            self.assertEqual(resumed.resume_transition["status"], "consumed")
            receipt_audit = store.resume_receipt_audit()[0]
            handoff_transitions = [
                item
                for item in receipt_audit["transitions"]
                if item["transition_kind"] == "handoff"
            ]
            self.assertEqual(
                [item["to_status"] for item in handoff_transitions],
                ["handoff_emitted", "consumed"],
            )
            emitted_transition, consumed_transition = handoff_transitions
            self.assertEqual(
                emitted_transition["handoff_message_id"],
                resume_handoff["message_id"],
            )
            self.assertEqual(
                consumed_transition["handoff_message_id"],
                resume_handoff["message_id"],
            )
            self.assertEqual(
                consumed_transition["agent_invocation_id"],
                consuming_invocation.invocation_id,
            )
            self.assertEqual(consumed_transition["agent_id"], "scout")
            self.assertEqual(
                consumed_transition["operation"],
                consuming_invocation.operation,
            )
            self.assertEqual(
                {item["owner_fence"] for item in handoff_transitions},
                {lease["fence"]},
            )
            self.assertTrue(
                all(item["owner_token_fingerprint"] for item in handoff_transitions)
            )
            self.assertNotIn(
                lease["owner_token"],
                json.dumps(receipt_audit, ensure_ascii=False),
            )

            prior_transition = dict(resumed.resume_transition)
            resumed.resume_transition.update(
                {
                    "status": "handoff_emitted",
                    "handoff_emitted_at": emitted_transition["created_at"],
                    "superseded_handoff_message_id": None,
                }
            )
            store.bind_execution_fence(lease["owner_token"], lease["fence"])
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                store._record_resume_handoff_transition(
                    connection,
                    "resume",
                    resumed,
                    resume_handoff,
                    emitted_transition["created_at"],
                )
                connection.commit()
            resumed.resume_transition.clear()
            resumed.resume_transition.update(prior_transition)
            self.assertEqual(
                len(
                    [
                        item
                        for item in store.resume_receipt_audit()[0]["transitions"]
                        if item["transition_kind"] == "handoff"
                    ]
                ),
                2,
            )
            self.assertTrue(
                store.finish_resume_execution(
                    prepared.idempotency_key,
                    owner_token=lease["owner_token"],
                    fence=lease["fence"],
                    status="completed",
                    durable_run_status=resumed.status,
                )
            )
            self.assertTrue(
                store.release_execution_lease(lease["owner_token"], lease["fence"])
            )

    async def test_resume_handoff_fails_closed_on_receipt_state_and_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 0
            initial = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="resume-fence-contract")
            store = RunStore(config.runs_dir, initial.run_id)
            prepared = prepare_resume(
                config,
                initial.run_id,
                {"additional_iterations": 1},
                source="manual",
                idempotency_key="manual:resume-fence-contract:request",
            )
            lease = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=30_000,
            )
            self.assertIsNotNone(lease)
            lease = dict(lease)
            state = store.latest()
            self.assertIsNotNone(state)
            pending_engine = ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=lease,
            )
            with self.assertRaisesRegex(
                HandoffValidationError, "active receipt claim"
            ):
                pending_engine._ensure_resume_handoff(store, state)
            self.assertFalse(
                any(
                    item["envelope"].get("resume_receipt_id")
                    for item in store.handoff_audit()
                )
            )

            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=lease["owner_token"],
                    fence=lease["fence"],
                )
            )
            stale_lease = {**lease, "fence": int(lease["fence"]) + 1}
            stale_engine = ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=stale_lease,
            )
            with self.assertRaisesRegex(
                HandoffValidationError, "active receipt claim"
            ):
                stale_engine._ensure_resume_handoff(store, state)

            self.assertTrue(
                store.finish_resume_execution(
                    prepared.idempotency_key,
                    owner_token=lease["owner_token"],
                    fence=lease["fence"],
                    status="completed",
                    durable_run_status="evidence_incomplete",
                )
            )
            state.resume_transition["claim_fence"] = int(lease["fence"])
            store.bind_execution_fence(lease["owner_token"], lease["fence"])
            with self.assertRaisesRegex(
                HandoffValidationError, "active resume receipt claim"
            ):
                ResearchEngine._save(store, "resume", state, {})
            rejected = [item for item in store.receipt_audit() if not item["valid"]]
            self.assertTrue(rejected)
            self.assertIn("active resume receipt claim", rejected[-1]["reason"])
            self.assertFalse(
                any(
                    item["envelope"].get("resume_receipt_id")
                    for item in store.handoff_audit()
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    lease["owner_token"], lease["fence"]
                )
            )

    async def test_stale_resume_worker_cannot_emit_after_higher_fence_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 0
            initial = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="resume-stale-takeover")
            store = RunStore(config.runs_dir, initial.run_id)
            prepared = prepare_resume(
                config,
                initial.run_id,
                {"additional_iterations": 2},
                source="manual",
                idempotency_key="manual:resume-stale-takeover:request",
            )
            first = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=30_000,
            )
            self.assertIsNotNone(first)
            first = dict(first)
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=first["owner_token"],
                    fence=first["fence"],
                )
            )

            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterResumeHandoffEngine(
                    config,
                    MockModelProvider(),
                    ReplaySearchProvider(corpus),
                    execution_lease=first,
                ).run(initial.question, run_id=initial.run_id)
            first_state = store.latest()
            self.assertIsNotNone(first_state)
            self.assertEqual(first_state.resume_transition["status"], "handoff_emitted")
            first_handoff_id = first_state.resume_transition["handoff_message_id"]
            self.assertTrue(
                store.release_execution_lease(
                    first["owner_token"], first["fence"]
                )
            )

            second = store.acquire_execution_lease(
                prepared.idempotency_key,
                ttl_ms=30_000,
            )
            self.assertIsNotNone(second)
            second = dict(second)
            self.assertGreater(second["fence"], first["fence"])
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    allow_reclaim=True,
                    owner_token=second["owner_token"],
                    fence=second["fence"],
                )
            )
            resumed = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=second,
            ).run(initial.question, run_id=initial.run_id)
            self.assertIn(
                resumed.status,
                {"completed", "verification_failed", "evidence_incomplete"},
            )
            second_handoff_id = resumed.resume_transition["handoff_message_id"]
            self.assertNotEqual(first_handoff_id, second_handoff_id)
            self.assertEqual(resumed.resume_transition["status"], "consumed")

            resume_handoffs = [
                item["envelope"]
                for item in store.handoff_audit()
                if item["envelope"].get("resume_receipt_id")
            ]
            self.assertEqual(
                {item["claim_fence"] for item in resume_handoffs},
                {first["fence"], second["fence"]},
            )
            resume_transitions = store.resume_receipt_audit()[0]["transitions"]
            emitted_transitions = [
                item
                for item in resume_transitions
                if item["transition_kind"] == "handoff"
                and item["to_status"] == "handoff_emitted"
            ]
            consumed_transitions = [
                item
                for item in resume_transitions
                if item["transition_kind"] == "handoff"
                and item["to_status"] == "consumed"
            ]
            self.assertEqual(len(emitted_transitions), 2)
            self.assertEqual(
                {item["owner_fence"] for item in emitted_transitions},
                {first["fence"], second["fence"]},
            )
            second_emission = next(
                item
                for item in emitted_transitions
                if item["owner_fence"] == second["fence"]
            )
            self.assertEqual(
                second_emission["superseded_handoff_message_id"],
                first_handoff_id,
            )
            self.assertEqual(second_emission["handoff_message_id"], second_handoff_id)
            self.assertEqual(len(consumed_transitions), 1)
            self.assertEqual(
                consumed_transitions[0]["handoff_message_id"],
                second_handoff_id,
            )
            self.assertEqual(
                consumed_transitions[0]["owner_fence"],
                second["fence"],
            )
            stale_store = RunStore(config.runs_dir, initial.run_id)
            stale_store.bind_execution_fence(
                first["owner_token"], first["fence"]
            )
            with self.assertRaises(ExecutionFenceLostError):
                ResearchEngine._save(stale_store, "resume", first_state, {})
            self.assertEqual(
                len(
                    [
                        item
                        for item in store.handoff_audit()
                        if item["envelope"].get("resume_receipt_id")
                    ]
                ),
                2,
            )
            self.assertTrue(
                store.finish_resume_execution(
                    prepared.idempotency_key,
                    owner_token=second["owner_token"],
                    fence=second["fence"],
                    status="completed",
                    durable_run_status=resumed.status,
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    second["owner_token"], second["fence"]
                )
            )

    async def test_new_resume_does_not_revalidate_historical_resume_consumption(self) -> None:
        """A new resume may save state containing an old, already-consumed handoff."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 0
            config.budget.max_total_iterations = 1
            initial = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="resume-history-fence")
            self.assertEqual(initial.status, "evidence_incomplete")
            store = RunStore(config.runs_dir, initial.run_id)

            first = prepare_resume(
                config,
                initial.run_id,
                {
                    "additional_iterations": 1,
                    "additional_search_calls": 3,
                    "additional_pages": 5,
                },
                source="manual",
                idempotency_key="manual:resume-history-fence:first",
            )
            first_lease = store.acquire_execution_lease(first.idempotency_key)
            self.assertIsNotNone(first_lease)
            assert first_lease is not None
            self.assertTrue(
                store.claim_resume_execution(
                    first.idempotency_key,
                    owner_token=first_lease["owner_token"],
                    fence=first_lease["fence"],
                )
            )
            first_state = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=first_lease,
            ).run(initial.question, run_id=initial.run_id)
            self.assertEqual(first_state.status, "evidence_incomplete")
            self.assertEqual(first_state.resume_transition["status"], "consumed")
            first_handoff_id = first_state.resume_transition["handoff_message_id"]
            self.assertTrue(
                store.finish_resume_execution(
                    first.idempotency_key,
                    owner_token=first_lease["owner_token"],
                    fence=first_lease["fence"],
                    status="completed",
                    durable_run_status=first_state.status,
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    first_lease["owner_token"], first_lease["fence"]
                )
            )

            second = prepare_resume(
                config,
                initial.run_id,
                {
                    "additional_iterations": 0,
                    "additional_search_calls": 0,
                    "additional_pages": 0,
                },
                source="manual",
                idempotency_key="manual:resume-history-fence:second",
            )
            second_lease = store.acquire_execution_lease(second.idempotency_key)
            self.assertIsNotNone(second_lease)
            assert second_lease is not None
            self.assertTrue(
                store.claim_resume_execution(
                    second.idempotency_key,
                    owner_token=second_lease["owner_token"],
                    fence=second_lease["fence"],
                )
            )
            second_state = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                execution_lease=second_lease,
            ).run(initial.question, run_id=initial.run_id)
            self.assertEqual(second_state.status, "evidence_incomplete")
            self.assertEqual(second_state.resume_transition["status"], "handoff_emitted")
            self.assertNotEqual(
                second_state.resume_transition["handoff_message_id"], first_handoff_id
            )
            self.assertTrue(
                store.finish_resume_execution(
                    second.idempotency_key,
                    owner_token=second_lease["owner_token"],
                    fence=second_lease["fence"],
                    status="completed",
                    durable_run_status=second_state.status,
                )
            )
            self.assertTrue(
                store.release_execution_lease(
                    second_lease["owner_token"], second_lease["fence"]
                )
            )

    async def test_offline_run_completes_with_verified_citations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            engine = ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            )
            state = await engine.run(
                "Who created Python and when was it first released?",
                run_id="test-run",
            )
            self.assertEqual(state.status, "completed")
            self.assertTrue(state.closure and state.closure.closed)
            self.assertTrue(state.verification and state.verification.passed)
            self.assertGreaterEqual(len(state.evidence), 2)
            self.assertGreaterEqual(len(state.sources), 2)
            self.assertTrue(any(source.status == "fetched" for source in state.sources))
            self.assertEqual(
                {item.agent_id for item in state.agent_invocations},
                {"planner", "scout", "curator", "critic", "writer", "verifier"},
            )
            self.assertTrue(
                all(item.status == "succeeded" for item in state.agent_invocations)
            )
            retrieval_stage_ids = {
                item.invocation_id
                for item in state.agent_invocations
                if item.operation == "search_and_fetch"
            }
            self.assertTrue(retrieval_stage_ids)
            self.assertTrue(
                all(
                    item.parent_invocation_id in retrieval_stage_ids
                    for item in state.agent_invocations
                    if item.operation in {"search", "fetch"}
                )
            )
            self.assertTrue(
                all(
                    item.parent_invocation_id is None
                    for item in state.agent_invocations
                    if item.operation not in {"search", "fetch"}
                )
            )
            self.assertIsNone(state.agent_invocations[0].previous_in_log_id)
            for previous, current in zip(
                state.agent_invocations, state.agent_invocations[1:]
            ):
                self.assertEqual(current.previous_in_log_id, previous.invocation_id)
            critic = next(
                item for item in state.agent_invocations if item.agent_id == "critic"
            )
            self.assertEqual(critic.provider_call_count, 0)
            provider_operations = {
                "plan",
                "generate_queries",
                "search",
                "fetch",
                "extract_evidence",
                "draft",
                "verify",
            }
            self.assertTrue(
                all(
                    item.provider_call_count == 1
                    for item in state.agent_invocations
                    if item.execution_mode == "executed"
                    and item.operation in provider_operations
                )
            )
            self.assertTrue(
                all(
                    item.provider_call_count == 0
                    for item in state.agent_invocations
                    if item.execution_mode == "executed"
                    and item.operation in {"assess_closure", "search_and_fetch"}
                )
            )
            self.assertTrue(
                all(
                    item.provider_call_count == 0
                    for item in state.agent_invocations
                    if item.execution_mode == "replayed"
                )
            )
            for agent_id in {"planner", "scout", "curator", "critic", "writer", "verifier"}:
                calls = [
                    item for item in state.agent_invocations if item.agent_id == agent_id
                ]
                self.assertTrue(any(item.handoff_message_ids for item in calls))
            fetched = next(source for source in state.sources if source.status == "fetched")
            self.assertEqual(fetched.http_status, 200)
            self.assertEqual(fetched.parser_version, "replay-corpus-v1")
            self.assertTrue(fetched.fetched_at)
            self.assertTrue((root / "runs/test-run/final.json").exists())
            events = [
                json.loads(line)
                for line in (root / "runs/test-run/events.jsonl").read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            closure_consumers = [
                item["payload"]["handoff_envelope"]["consumer"]
                for item in events
                if item["node"] == "assess_closure"
            ]
            self.assertIn("scout", closure_consumers)
            self.assertEqual(closure_consumers[-1], "writer")
            verifier_event = next(item for item in events if item["node"] == "verify")
            self.assertEqual(
                verifier_event["payload"]["handoff_envelope"]["consumer"],
                "orchestrator",
            )
            self.assertFalse(
                any("-or-" in consumer for consumer in closure_consumers)
            )
            envelopes = [
                item["payload"]["handoff_envelope"]
                for item in events
                if "handoff_envelope" in item["payload"]
            ]
            self.assertTrue(
                all(
                    envelope["consumer"] == envelope["intended_consumer"]
                    for envelope in envelopes
                )
            )
            artifacts_dir = root / "runs/test-run/artifacts"
            previous_output = None
            for envelope in envelopes:
                output = envelope["output_artifacts"][0]
                artifact_path = root / "runs/test-run" / output["content_uri"]
                content = artifact_path.read_bytes()
                self.assertEqual(hashlib.sha256(content).hexdigest(), output["checksum"])
                self.assertEqual(len(content), output["byte_length"])
                self.assertEqual(output["canonicalization"], "json-sort-keys-utf8-v1")
                self.assertTrue(output["producer_invocation_id"])
                self.assertEqual(output["handoff_message_id"], envelope["message_id"])
                self.assertTrue(output["metadata_hash"])
                self.assertTrue(
                    (artifacts_dir / f"{output['artifact_id']}.meta.json").exists()
                )
                if previous_output is None:
                    self.assertEqual(envelope["input_artifacts"], [])
                else:
                    self.assertEqual(envelope["input_artifacts"], [previous_output])
                    self.assertNotEqual(
                        envelope["input_artifacts"][0]["checksum"], "referenced"
                    )
                previous_output = output
            plan_envelope = next(
                item["payload"]["handoff_envelope"]
                for item in events
                if item["node"] == "plan"
            )
            query_envelope = next(
                item["payload"]["handoff_envelope"]
                for item in events
                if item["node"] == "generate_queries"
            )
            self.assertEqual(plan_envelope["route_target"], "generate_queries")
            self.assertIsNone(plan_envelope["receipt"])
            self.assertEqual(
                query_envelope["receipt"]["message_id"], plan_envelope["message_id"]
            )
            consuming_invocation = next(
                item
                for item in state.agent_invocations
                if item.invocation_id
                == query_envelope["receipt"]["consumed_by_invocation_id"]
            )
            self.assertIn(
                plan_envelope["message_id"],
                consuming_invocation.consumed_handoff_message_ids,
            )
            store = RunStore(root / "runs", "test-run")
            invocation_ids = {
                item["invocation_id"] for item in store.invocation_rows()
            }
            self.assertTrue(
                all(
                    envelope["producer_invocation_id"] in invocation_ids
                    for envelope in envelopes
                )
            )
            tool_operations = [
                item
                for item in store.operation_rows()
                if item["kind"] in {"search", "fetch"}
            ]
            fresh_tool_invocations = [
                item
                for item in state.agent_invocations
                if item.operation in {"search", "fetch"}
                and item.execution_mode == "executed"
            ]
            self.assertEqual(len(fresh_tool_invocations), len(tool_operations))
            self.assertTrue(
                all(item.operation_key for item in fresh_tool_invocations)
            )
            self.assertTrue(
                all(
                    store.read_artifact(envelope["output_artifacts"][0]["artifact_id"])[
                        "manifest_valid"
                    ]
                    for envelope in envelopes
                )
            )
            self.assertTrue(
                all(item["receipt_status"] in {"valid", "not_consumed"}
                    for item in store.handoff_audit())
            )
            source_fetches = store.source_fetch_audit()
            self.assertTrue(source_fetches)
            self.assertTrue(
                all(
                    item["binding_status"] == "server_bound"
                    and item["binding_valid"]
                    and item["requested_url"] == item["canonical_requested_url"]
                    for item in source_fetches
                )
            )
            sources_by_id = {source.id: source for source in state.sources}
            self.assertTrue(state.evidence)
            self.assertTrue(
                all(
                    item.fetch_record_id
                    and item.snapshot_sha256
                    and item.content_hash_scope != "unknown"
                    for item in state.evidence
                )
            )
            for item in state.evidence:
                source = sources_by_id[item.source_id]
                bound_fetch = next(
                    row
                    for row in source_fetches
                    if row["fetch_record_id"] == item.fetch_record_id
                )
                self.assertEqual(bound_fetch["source_id"], source.id)
                self.assertEqual(item.fetch_record_id, bound_fetch["fetch_record_id"])
                self.assertEqual(item.content_hash, bound_fetch["content_hash"])
                self.assertEqual(item.snapshot_sha256, bound_fetch["snapshot_sha256"])
                self.assertEqual(item.content_hash_scope, bound_fetch["content_hash_scope"])
            bound_fetch = source_fetches[0]
            with self.assertRaisesRegex(ValueError, "source id"):
                store.record_source_fetch(
                    source_id="Sforged",
                    requested_url=bound_fetch["requested_url"],
                    operation_key=bound_fetch["operation_key"],
                    invocation_id=bound_fetch["invocation_id"],
                    result_invocation_id=bound_fetch["result_invocation_id"],
                    execution_mode=bound_fetch["execution_mode"],
                    provider=bound_fetch["provider"],
                    fetch_mode=bound_fetch["fetch_mode"],
                    status=bound_fetch["status"],
                    attempt=bound_fetch["attempt"],
                    final_url=bound_fetch["final_url"],
                    content_hash=bound_fetch["content_hash"],
                    snapshot_sha256=bound_fetch["snapshot_sha256"],
                    error=bound_fetch["error"],
                    fetched_at=bound_fetch["fetched_at"],
                )
            with self.assertRaisesRegex(ValueError, "provider"):
                store.record_source_fetch(
                    source_id=bound_fetch["source_id"],
                    requested_url=bound_fetch["requested_url"],
                    operation_key=bound_fetch["operation_key"],
                    invocation_id=bound_fetch["invocation_id"],
                    result_invocation_id=bound_fetch["result_invocation_id"],
                    execution_mode=bound_fetch["execution_mode"],
                    provider="ForgedProvider",
                    fetch_mode=bound_fetch["fetch_mode"],
                    status=bound_fetch["status"],
                    attempt=bound_fetch["attempt"],
                    final_url=bound_fetch["final_url"],
                    content_hash=bound_fetch["content_hash"],
                    snapshot_sha256=bound_fetch["snapshot_sha256"],
                    error=bound_fetch["error"],
                    fetched_at=bound_fetch["fetched_at"],
                )
            usage = store.usage_totals()
            self.assertEqual(usage["usage_status"], "not_applicable")
            self.assertEqual(usage["provider"], "MockModelProvider")
            self.assertEqual(usage["pricing_status"], "not_applicable")
            restored = store.latest()
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, "completed")
            self.assertEqual(len(restored.agent_invocations), len(state.agent_invocations))

    async def test_cancelled_run_is_checkpointed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            engine = ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
                cancel_check=lambda: True,
            )
            state = await engine.run("A question", run_id="cancelled-run")
            self.assertEqual(state.status, "cancelled")
            restored = RunStore(root / "runs", "cancelled-run").latest()
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, "cancelled")

    async def test_response_usage_is_visible_before_multi_call_plan_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 0
            model = LiveUsagePlanModel()
            task = asyncio.create_task(
                ResearchEngine(
                    config,
                    model,
                    ReplaySearchProvider(corpus),
                ).run("What is a durable usage ledger?", run_id="live-response-usage")
            )

            await asyncio.wait_for(model.first_response.wait(), timeout=2)
            live_usage = RunStore(config.runs_dir, "live-response-usage").usage_totals()
            self.assertEqual(live_usage["model_calls"], 1)
            self.assertEqual(live_usage["input_tokens"], 100)
            self.assertAlmostEqual(live_usage["estimated_cost_usd"], 0.0012)
            self.assertEqual(live_usage["settled_model_responses"], 1)
            self.assertEqual(live_usage["settled_model_operations"], 1)

            model.allow_completion.set()
            await asyncio.wait_for(task, timeout=5)
            final_usage = RunStore(config.runs_dir, "live-response-usage").usage_totals()
            self.assertEqual(final_usage["model_calls"], 2)
            self.assertEqual(final_usage["input_tokens"], 160)
            self.assertEqual(final_usage["output_tokens"], 30)
            self.assertAlmostEqual(final_usage["estimated_cost_usd"], 0.00195)
            self.assertEqual(final_usage["settled_model_responses"], 2)

    async def test_failed_contradiction_search_blocks_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            model = CountingMockModelProvider()
            engine = ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                model,
                EmptyContradictionSearchProvider(corpus),
            )
            state = await engine.run(
                "Who created Python and when was it first released?",
                run_id="contradiction-search-failed",
            )
            self.assertEqual(state.status, "evidence_incomplete")
            self.assertFalse(state.closure and state.closure.hard_gate_passed)
            self.assertEqual(model.draft_calls, 0)
            self.assertFalse(state.contradiction_checked_slots)
            self.assertTrue(
                any(item.status == "no_results" for item in state.contradiction_checks)
            )

    async def test_irrelevant_contradiction_pages_do_not_pass_check_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            model = CountingMockModelProvider()
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                model,
                IrrelevantContradictionSearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="irrelevant-contradiction-pages",
            )
            self.assertEqual(state.status, "evidence_incomplete")
            self.assertFalse(state.contradiction_checked_slots)
            audits = [
                item
                for item in state.contradiction_checks
                if item.status == "inspected_irrelevant_only"
            ]
            self.assertTrue(audits)
            self.assertTrue(all(item.pages_inspected > 0 for item in audits))
            self.assertTrue(all(item.relevant_pages_inspected == 0 for item in audits))
            self.assertEqual(model.draft_calls, 0)

    async def test_unrelated_sources_cannot_populate_answer_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run(
                "What is the launch date of Project Zephyr X?",
                run_id="unrelated-source-gate",
            )
            self.assertEqual(state.status, "evidence_incomplete")
            self.assertIsNone(state.plan.slots[0].value)
            self.assertFalse(state.plan.slots[0].supporting_evidence)
            self.assertEqual(state.answer_delivery["mode"], "evidence_limited")
            self.assertIsNotNone(state.draft_answer)
            self.assertIn("暂不能给出具体事实判断", state.draft_answer)
            self.assertIn("当前可交付回答", state.draft_answer)
            self.assertIn(
                "compose_limited_answer",
                {item.operation for item in state.agent_invocations},
            )
            self.assertIn(
                "check_limited_delivery",
                {item.operation for item in state.agent_invocations},
            )
            self.assertNotIn("Python was created", state.draft_answer)

    async def test_explicit_disclaimer_is_excluded_end_to_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            model = CapturingEvidenceModel()
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                model,
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="explicit-disclaimer-gate")
            unrelated = [
                item
                for item in state.evidence
                if item.source_url.endswith("/other-language")
            ]
            self.assertTrue(unrelated, "replay corpus must exercise disclaimer evidence")
            unrelated_ids = {item.id for item in unrelated}
            self.assertTrue(all(item.slot_relevance_score == 0.0 for item in unrelated))
            self.assertTrue(
                all(
                    unrelated_ids.isdisjoint(slot.supporting_evidence)
                    for slot in state.plan.slots
                )
            )
            self.assertTrue(
                all(
                    unrelated_ids.isdisjoint(audit.supporting_evidence_ids)
                    for audit in state.closure.slot_audits
                )
            )
            self.assertEqual(state.status, "completed")
            self.assertTrue(unrelated_ids.isdisjoint(model.draft_evidence_ids))
            self.assertTrue(unrelated_ids.isdisjoint(model.verify_evidence_ids))
            allowed_ids = set(state.closure.slot_audits[0].supporting_evidence_ids)
            self.assertEqual(model.draft_evidence_ids, allowed_ids)
            self.assertEqual(model.verify_evidence_ids, allowed_ids)

    async def test_run_id_cannot_resume_a_different_question(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            engine = ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            )
            await engine.run("First question", run_id="bound-question")
            with self.assertRaises(ValueError):
                await engine.run("Different question", run_id="bound-question")

    async def test_resume_after_fetch_reuses_persisted_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            search = CountingReplaySearchProvider(corpus)
            failing = ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                FailFirstExtractModelProvider(),
                search,
            )
            with self.assertRaises(RuntimeError):
                await failing.run(
                    "Who created Python and when was it first released?",
                    run_id="resume-after-fetch",
                )
            restored = RunStore(root / "runs", "resume-after-fetch").latest()
            self.assertEqual(restored.next_node, "ingest_evidence")
            self.assertTrue(restored.pending_pages)
            calls_before_resume = search.search_calls
            resumed = ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                MockModelProvider(),
                search,
            )
            state = await resumed.run(
                "Who created Python and when was it first released?",
                run_id="resume-after-fetch",
            )
            self.assertEqual(state.status, "completed")
            self.assertEqual(search.search_calls, calls_before_resume + 2)

    async def test_resume_after_draft_does_not_redraft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            with self.assertRaises(RuntimeError):
                await ResearchEngine(
                    AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                    FailFirstVerifyModelProvider(),
                    ReplaySearchProvider(corpus),
                ).run(
                    "Who created Python and when was it first released?",
                    run_id="resume-after-draft",
                )
            restored = RunStore(root / "runs", "resume-after-draft").latest()
            self.assertEqual(restored.next_node, "verify")
            model = CountingMockModelProvider()
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                model,
                ReplaySearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="resume-after-draft",
            )
            self.assertEqual(state.status, "completed")
            self.assertEqual(model.draft_calls, 0)

    async def test_verifier_timeout_delivers_local_citation_binding_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                TimeoutVerifyModelProvider(),
                ReplaySearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="verify-timeout-local-binding",
            )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.answer_delivery["mode"], "local_citation_binding")
        self.assertFalse(state.answer_delivery["verified"])
        self.assertIsNotNone(state.verification)
        self.assertFalse(state.verification.passed)
        self.assertIsNone(state.verification.provider_passed)
        self.assertEqual(
            state.verification.contract_version,
            "local-citation-binding-v1",
        )
        self.assertTrue(all(item.status == "partial" for item in state.verification.items))
        self.assertTrue(all(item.citation_set_match for item in state.verification.items))
        local_checks = [
            item
            for item in state.agent_invocations
            if item.operation == "confirm_local_citation_binding"
        ]
        self.assertEqual(len(local_checks), 1)
        self.assertEqual(local_checks[0].agent_id, "verifier")
        self.assertEqual(local_checks[0].status, "succeeded")
        self.assertEqual(local_checks[0].provider_call_count, 0)
        self.assertEqual(
            local_checks[0].model_id,
            "deterministic-citation-binding-v1",
        )
        self.assertTrue(
            any(
                item.operation == "verify" and item.status == "failed"
                for item in state.agent_invocations
            )
        )

    async def test_verifier_gateway_520_delivers_local_citation_binding_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                Gateway520VerifyModelProvider(),
                ReplaySearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="verify-520-local-binding",
            )

        self.assertEqual(state.status, "completed")
        self.assertEqual(state.answer_delivery["mode"], "local_citation_binding")
        self.assertFalse(state.answer_delivery["verified"])
        self.assertIsNotNone(state.verification)
        self.assertEqual(
            state.verification.contract_version,
            "local-citation-binding-v1",
        )
        self.assertTrue(all(item.status == "partial" for item in state.verification.items))
        self.assertTrue(all(item.citation_set_match for item in state.verification.items))
        self.assertIn(
            "confirm_local_citation_binding",
            {item.operation for item in state.agent_invocations},
        )

    async def test_resume_after_passed_verify_only_finalizes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterVerifyEngine(
                    AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                    MockModelProvider(),
                    ReplaySearchProvider(corpus),
                ).run(
                    "Who created Python and when was it first released?",
                    run_id="resume-after-verify",
                )
            restored = RunStore(root / "runs", "resume-after-verify").latest()
            self.assertEqual(restored.status, "completed")
            self.assertEqual(restored.next_node, "finalize")
            model = CountingAllCallsMockModelProvider()
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                model,
                ReplaySearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="resume-after-verify",
            )
            self.assertEqual(state.next_node, "done")
            self.assertEqual(model.calls, 0)

    async def test_cached_provider_pass_cannot_bypass_engine_verification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 2
            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterVerifyOperationEngine(
                    config,
                    PassedEmptyVerifierModel(),
                    ReplaySearchProvider(corpus),
                ).run(
                    "Who created Python and when was it first released?",
                    run_id="cached-malicious-verify",
                )
            restored = RunStore(root / "runs", "cached-malicious-verify").latest()
            self.assertEqual(restored.next_node, "verify")
            resumed_model = PassedEmptyVerifierModel()
            state = await ResearchEngine(
                config,
                resumed_model,
                ReplaySearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="cached-malicious-verify",
            )
            self.assertEqual(resumed_model.verify_calls, 0)
            self.assertEqual(state.status, "verification_failed")
            self.assertTrue(state.verification.provider_passed)
            self.assertFalse(state.verification.passed)
            self.assertEqual(state.verification.provider_item_count, 0)
            self.assertGreater(state.verification.expected_item_count, 0)

    async def test_response_committed_before_checkpoint_replays_without_model_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            first_model = CountingPlanModel()
            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterPlanOperationEngine(
                    config,
                    first_model,
                    ReplaySearchProvider(corpus),
                ).run("Who created Python?", run_id="operation-replay")
            self.assertEqual(first_model.plan_calls, 1)
            store = RunStore(root / "runs", "operation-replay")
            original_operation = store.operation_rows()[0]
            self.assertEqual(original_operation["status"], "succeeded")
            original = store.invocation(
                original_operation["original_invocation_id"]
            )
            self.assertIsNotNone(original)
            self.assertEqual(original.operation_key, original_operation["operation_key"])
            self.assertEqual(original.execution_mode, "executed")
            self.assertEqual(original.side_effect_status, "committed")
            self.assertFalse((root / "runs/operation-replay/final.json").exists())

            resumed_model = CountingPlanModel()
            state = await ResearchEngine(
                config,
                resumed_model,
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="operation-replay")
            self.assertEqual(resumed_model.plan_calls, 0)
            self.assertGreaterEqual(len(state.operation_replays), 1)
            replay = next(
                item
                for item in state.agent_invocations
                if item.output_type == "ReplayedResult"
            )
            self.assertEqual(replay.agent_id, "planner")
            self.assertIn("provider was not called", replay.output_summary)
            self.assertEqual(replay.execution_mode, "replayed")
            self.assertEqual(replay.provider_call_count, 0)
            self.assertIsNone(replay.parent_invocation_id)
            self.assertEqual(replay.replay_of_invocation_id, original.invocation_id)
            self.assertEqual(replay.operation_key, original.operation_key)
            self.assertEqual(replay.side_effect_status, "not_reexecuted")

    async def test_fence_loss_finalizes_only_the_last_durable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            run_id = "fence-lost-final"
            durable = ResearchState(run_id=run_id, question="Who created Python?")
            RunStore(root / "runs", run_id).checkpoint("seed", durable)

            with self.assertRaises(ExecutionFenceLostError):
                await FenceLostAfterPlanOperationEngine(
                    AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                    CountingPlanModel(),
                    ReplaySearchProvider(corpus),
                ).run(durable.question, run_id=run_id)

            final = json.loads(
                (root / f"runs/{run_id}/final.json").read_text(encoding="utf-8")
            )
            self.assertEqual(final["status"], "initialized")
            self.assertEqual(final["next_node"], "plan")
            self.assertEqual(final["agent_invocations"], [])

    async def test_lease_loss_cancels_external_call_and_marks_operation_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RunStore(root / "runs", "inflight-lease-loss")
            lease = store.acquire_execution_lease(
                "inflight-lease-loss",
                ttl_ms=30_000,
            )
            self.assertIsNotNone(lease)
            assert lease is not None
            store.bind_execution_fence(lease["owner_token"], lease["fence"])
            lost = asyncio.Event()
            cancelled = asyncio.Event()
            engine = ResearchEngine(
                AppConfig(runs_dir=root / "runs"),
                MockModelProvider(),
                ReplaySearchProvider(Path("examples/replay_corpus.json")),
                execution_lease=lease,
                lease_lost_check=lost.is_set,
            )
            state = ResearchState(
                run_id=store.run_id,
                question="Fence an in-flight provider call",
            )

            async def provider_call():
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            async def revoke_lease():
                await asyncio.sleep(0.03)
                self.assertTrue(
                    store.release_execution_lease(
                        lease["owner_token"], lease["fence"]
                    )
                )
                lost.set()

            revocation = asyncio.create_task(revoke_lease())
            with self.assertRaises(ExecutionFenceLostError):
                await engine._execute_tool_operation(
                    store,
                    state,
                    "search",
                    {"query": "in-flight"},
                    provider_call,
                    lambda value: value,
                    lambda value: value,
                    parent_invocation_id="parent-invocation",
                )
            await revocation

            self.assertTrue(cancelled.is_set())
            operation = store.operation_rows()[0]
            self.assertEqual(operation["status"], "external_outcome_unknown")
            self.assertEqual(operation["side_effect_status"], "unknown")

    def test_legacy_contract_fields_remain_loadable_without_receipt_claims(self) -> None:
        invocation = AgentInvocation(
            invocation_id="legacy-invocation",
            agent_id="planner",
            role="research_planner",
            operation="plan",
            attempt=1,
            started_at="2026-01-01T00:00:00+00:00",
            ended_at="2026-01-01T00:00:01+00:00",
            status="succeeded",
            input_type="ResearchQuestion",
        )
        envelope = HandoffEnvelope(
            schema_version="deep-research-handoff/1.0",
            message_id="legacy-message",
            trace_id="legacy-run",
            run_id="legacy-run",
            producer="planner",
            consumer="scout",
            attempt=1,
            idempotency_key="legacy-key",
            created_at="2026-01-01T00:00:01+00:00",
        )
        self.assertEqual(invocation.execution_mode, "executed")
        self.assertIsNone(invocation.provider_call_count)
        self.assertIsNone(invocation.previous_in_log_id)
        self.assertEqual(envelope.intended_consumer, "scout")
        self.assertEqual(envelope.route_target, "scout")
        self.assertIsNone(envelope.receipt)

    def test_historical_source_fetch_input_cannot_be_server_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(Path(directory), "legacy-source-binding")
            semantic_input = {
                "requested_url": "https://example.com/article?utm_source=old",
                "title": "Historical source",
                "source_type": "web",
                "fetch_policy": "public-http-ssrf-guard-v2",
                "parser_contract": "html-pdf-text-v2",
            }
            semantic_json = json.dumps(
                semantic_input,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            operation_key = "legacy-fetch-operation"
            store.begin_operation(
                operation_key,
                "fetch",
                hashlib.sha256(semantic_json.encode()).hexdigest(),
                kind="fetch",
                idempotent=True,
            )
            invocation = AgentInvocation(
                invocation_id="legacy-fetch-invocation",
                agent_id="scout",
                role="retrieval_strategist",
                operation="fetch",
                attempt=1,
                started_at="2026-01-01T00:00:00+00:00",
                ended_at=None,
                status="running",
                input_type="SearchResult",
                execution_mode="executed",
                provider_call_count=1,
                input_summary=json.dumps(
                    semantic_input,
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                run_id=store.run_id,
                trace_id=store.run_id,
                operation_key=operation_key,
                side_effect_status="unknown",
            )
            store.save_invocation(invocation, operation_key=operation_key)
            store.fail_operation(operation_key, "historical test failure")
            invocation.status = "failed"
            invocation.ended_at = "2026-01-01T00:00:01+00:00"
            invocation.error = "historical test failure"
            invocation.side_effect_status = "not_committed"
            store.save_invocation(invocation, operation_key=operation_key)

            result = store.record_source_fetch(
                source_id="Slegacy",
                requested_url="https://example.com/article",
                operation_key=operation_key,
                invocation_id=invocation.invocation_id,
                result_invocation_id=None,
                execution_mode="executed",
                provider="ReplaySearchProvider",
                fetch_mode="offline_corpus",
                status="failed",
                attempt=1,
                error="historical test failure",
            )
            self.assertEqual(result["binding_status"], "legacy_unverified")
            audit = store.source_fetch_audit()
            self.assertEqual(len(audit), 1)
            self.assertEqual(audit[0]["binding_status"], "legacy_unverified")
            self.assertFalse(audit[0]["binding_valid"])
            self.assertTrue(audit[0]["binding_digest_valid"])

    async def test_ambiguous_started_operation_is_not_automatically_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            with self.assertRaises(SimulatedProcessCrash):
                await ResearchEngine(
                    config,
                    DieDuringPlanModel(should_die=True),
                    ReplaySearchProvider(corpus),
                ).run("Who created Python?", run_id="ambiguous-operation")
            store = RunStore(root / "runs", "ambiguous-operation")
            ambiguous = store.operation_rows()[0]
            self.assertEqual(ambiguous["status"], "started")
            self.assertEqual(ambiguous["side_effect_status"], "unknown")
            original = store.invocation(ambiguous["original_invocation_id"])
            self.assertIsNotNone(original)
            self.assertEqual(original.side_effect_status, "unknown")

            resumed_model = DieDuringPlanModel(should_die=False)
            with self.assertRaises(AmbiguousOperationError):
                await ResearchEngine(
                    config,
                    resumed_model,
                    ReplaySearchProvider(corpus),
                ).run("Who created Python?", run_id="ambiguous-operation")
            self.assertEqual(resumed_model.plan_calls, 0)
            failed = store.latest()
            self.assertEqual(failed.status, "failed")
            self.assertTrue(failed.draft_answer)
            self.assertEqual(
                failed.answer_delivery["mode"], "interrupted_evidence_limited"
            )

    async def test_pre_request_model_failure_is_safe_to_resume_without_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            with self.assertRaises(ProviderRequestNotSent):
                await ResearchEngine(
                    config,
                    FailBeforeRequestPlanModel(),
                    ReplaySearchProvider(corpus),
                ).run("Who created Python?", run_id="safe-transport-retry")

            store = RunStore(config.runs_dir, "safe-transport-retry")
            failed = store.latest()
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.next_node, "plan")
            self.assertEqual(failed.suspension["resume_node"], "plan")
            self.assertEqual(failed.failures[-1]["type"], "model_transport_error")
            self.assertTrue(failed.failures[-1]["retryable"])
            self.assertTrue(failed.draft_answer)
            self.assertEqual(
                failed.answer_delivery["mode"], "interrupted_evidence_limited"
            )
            self.assertIn("当前可交付回答", failed.draft_answer)
            operation = store.operation_rows()[0]
            self.assertEqual(operation["status"], "failed")
            self.assertEqual(operation["side_effect_status"], "not_committed")
            invocation = store.invocation(operation["original_invocation_id"])
            self.assertIsNotNone(invocation)
            self.assertEqual(invocation.side_effect_status, "not_committed")

            prepared = prepare_resume(
                config,
                failed.run_id,
                {},
                source="manual",
                idempotency_key="manual:safe-transport-retry:request",
            )
            self.assertFalse(prepared.response.get("ambiguous_operations_confirmed"))
            self.assertEqual(store.ambiguous_operations(), [])

    async def test_legacy_failed_run_backfills_attachment_based_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            run_id = "legacy-failed-delivery"
            state = ResearchState(
                run_id=run_id,
                question="为什么 3D 可以增强 2D 检索？",
                status="failed",
                next_node="done",
                plan=ResearchPlan(
                    answer_type="long_text",
                    slots=[
                        AnswerSlot(
                            id="mechanism",
                            description="解释 3D 结构流与 2D 视觉流的互补。",
                        )
                    ],
                    subgoals=[],
                ),
                attachment_observations=[
                    AttachmentObservation(
                        attachment_id="Iimage1234567890",
                        modality="image",
                        summary="图中显示 2D 视觉特征 f_vis 与 3D 姿态结构特征 f_pose 经过可靠性门控融合为 f_out。",
                        observations=[
                            GroundedObservation(
                                locator="page 1 / fusion module",
                                text="Reliability-Aware Gating",
                                kind="ocr",
                                confidence=0.99,
                                page=1,
                            )
                        ],
                    )
                ],
                failures=[
                    {
                        "type": "ambiguous_operation",
                        "reason": "connection closed before response",
                        "next_node": "generate_queries",
                    }
                ],
                suspension={
                    "reason": "ambiguous_operation",
                    "resume_node": "generate_queries",
                },
            )
            store = RunStore(config.runs_dir, run_id)
            store.checkpoint("recover", state)

            restored = await ResearchEngine(
                config,
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run(state.question, run_id=run_id)

            self.assertEqual(restored.status, "failed")
            self.assertEqual(restored.next_node, "done")
            self.assertEqual(
                restored.answer_delivery["mode"], "interrupted_evidence_limited"
            )
            self.assertIn("3D 姿态结构特征", restored.draft_answer)
            self.assertIn("重复收费", restored.draft_answer)
            self.assertEqual(
                RunStore(config.runs_dir, run_id).latest().answer_delivery["mode"],
                "interrupted_evidence_limited",
            )

    async def test_search_results_replay_after_operation_commit_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            search = CountingReplaySearchProvider(corpus)
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterSearchOperationEngine(
                    config, MockModelProvider(), search
                ).run(
                    "Who created Python and when was it first released?",
                    run_id="search-operation-replay",
                )
            initial_calls = search.search_calls
            self.assertEqual(initial_calls, 2)
            state = await ResearchEngine(
                config, MockModelProvider(), search
            ).run(
                "Who created Python and when was it first released?",
                run_id="search-operation-replay",
            )
            self.assertEqual(state.status, "completed")
            self.assertEqual(search.search_calls, initial_calls + 2)
            self.assertGreaterEqual(len(state.operation_replays), 2)
            search_replays = [
                item
                for item in state.agent_invocations
                if item.operation == "search" and item.execution_mode == "replayed"
            ]
            self.assertGreaterEqual(len(search_replays), 2)
            self.assertTrue(
                all(item.provider_call_count == 0 for item in search_replays)
            )
            self.assertTrue(
                all(
                    item.replay_of_invocation_id
                    and item.operation_key
                    and RunStore(root / "runs", "search-operation-replay").invocation(
                        item.replay_of_invocation_id
                    )
                    for item in search_replays
                )
            )

    async def test_fetched_pages_replay_after_operation_commit_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            search = CountingReplaySearchProvider(corpus)
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            with self.assertRaises(SimulatedProcessCrash):
                await CrashAfterFetchOperationEngine(
                    config, MockModelProvider(), search
                ).run(
                    "Who created Python and when was it first released?",
                    run_id="fetch-operation-replay",
                )
            initial_fetches = search.fetch_calls
            self.assertEqual(initial_fetches, 3)
            state = await ResearchEngine(
                config, MockModelProvider(), search
            ).run(
                "Who created Python and when was it first released?",
                run_id="fetch-operation-replay",
            )
            self.assertEqual(state.status, "completed")
            self.assertEqual(search.fetch_calls, initial_fetches)
            self.assertEqual(len({source.id for source in state.sources}), len(state.sources))
            fetch_replays = [
                item
                for item in state.agent_invocations
                if item.operation == "fetch" and item.execution_mode == "replayed"
            ]
            self.assertGreaterEqual(len(fetch_replays), initial_fetches)
            self.assertTrue(all(item.provider_call_count == 0 for item in fetch_replays))
            self.assertTrue(
                all(
                    item.replay_of_invocation_id
                    and item.operation_key
                    and RunStore(root / "runs", "fetch-operation-replay").invocation(
                        item.replay_of_invocation_id
                    )
                    for item in fetch_replays
                )
            )

    async def test_receipt_validation_rejects_ghost_wrong_scope_consumer_and_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            state = await ResearchEngine(
                AppConfig(runs_dir=root / "runs", replay_corpus=corpus),
                MockModelProvider(),
                ReplaySearchProvider(corpus),
            ).run("Who created Python?", run_id="receipt-validation")
            store = RunStore(root / "runs", state.run_id)
            events = [
                json.loads(line)
                for line in store.events_path.read_text(encoding="utf-8").splitlines()
            ]
            envelopes = [
                item["payload"]["handoff_envelope"]
                for item in events
                if "handoff_envelope" in item["payload"]
            ]
            final_envelope = envelopes[-1]
            verifier = next(
                item for item in state.agent_invocations if item.agent_id == "verifier"
            )

            def candidate(invocation_id, agent_id, run_id=state.run_id, trace_id=state.run_id):
                return {
                    "producer_invocation_id": invocation_id,
                    "receipt": {
                        "message_id": final_envelope["message_id"],
                        "consumed_by_invocation_id": invocation_id,
                        "consumed_by_agent_id": agent_id,
                        "consumed_at": verifier.ended_at,
                        "run_id": run_id,
                        "trace_id": trace_id,
                        "valid": True,
                        "validation_error": None,
                    },
                }

            ghost = store.validate_handoff_receipt(candidate("ghost", "user"))
            self.assertFalse(ghost["valid"])
            self.assertIn("ghost invocation", ghost["reason"])
            wrong_scope = store.validate_handoff_receipt(
                candidate(verifier.invocation_id, verifier.agent_id, run_id="other-run")
            )
            self.assertFalse(wrong_scope["valid"])
            self.assertIn("receipt run", wrong_scope["reason"])
            wrong_trace = store.validate_handoff_receipt(
                candidate(
                    verifier.invocation_id,
                    verifier.agent_id,
                    trace_id="other-trace",
                )
            )
            self.assertFalse(wrong_trace["valid"])
            self.assertIn("receipt trace", wrong_trace["reason"])
            wrong_consumer = store.validate_handoff_receipt(
                candidate(verifier.invocation_id, verifier.agent_id)
            )
            self.assertFalse(wrong_consumer["valid"])
            self.assertIn("wrong consumer", wrong_consumer["reason"])
            consumed_envelope = next(item for item in envelopes if item["receipt"])
            duplicate = store.validate_handoff_receipt(consumed_envelope)
            self.assertFalse(duplicate["valid"])
            self.assertIn("duplicate receipt", duplicate["reason"])

            rejected_envelope = {
                "schema_version": "deep-research-handoff/1.1",
                "message_id": "rejected-receipt-message",
                "trace_id": state.run_id,
                "run_id": state.run_id,
                "producer": verifier.agent_id,
                "producer_invocation_id": verifier.invocation_id,
                "consumer": "user",
                "intended_consumer": "user",
                "route_target": "done",
                "attempt": 1,
                "idempotency_key": "rejected-receipt",
                "created_at": verifier.ended_at,
                "input_artifacts": [],
                "output_artifacts": [],
                "quality_gate": None,
                "receipt": candidate("ghost-audit", "user")["receipt"],
                "receipt_validation": "valid",
                "receipt_validation_error": None,
            }
            with self.assertRaisesRegex(HandoffValidationError, "ghost invocation"):
                store.commit_stage(
                    "invalid_receipt",
                    state,
                    "node_finished",
                    {"handoff_envelope": rejected_envelope},
                )
            rejected = [item for item in store.receipt_audit() if not item["valid"]]
            self.assertTrue(rejected)
            self.assertIn("ghost invocation", rejected[-1]["reason"])

            half_bound_envelope = {
                **final_envelope,
                "message_id": "half-bound-claim-fence",
                "idempotency_key": "half-bound-claim-fence",
                "receipt": None,
                "receipt_validation": "not_present",
                "receipt_validation_error": None,
                "resume_receipt_id": None,
                "claim_fence": 7,
                "output_artifacts": [],
            }
            with self.assertRaisesRegex(
                HandoffValidationError,
                "claim fence without a resume receipt",
            ):
                store.commit_stage(
                    "invalid_half_binding",
                    state,
                    "node_finished",
                    {"handoff_envelope": half_bound_envelope},
                )
            rejected = [item for item in store.receipt_audit() if not item["valid"]]
            self.assertEqual(rejected[-1]["message_id"], "half-bound-claim-fence")
            self.assertIn(
                "claim fence without a resume receipt",
                rejected[-1]["reason"],
            )

    def test_query_similarity_detects_near_duplicates(self) -> None:
        similarity = _query_similarity(
            "Python first release year official history",
            "official Python first release year history",
        )
        self.assertEqual(similarity, 1.0)

    def test_query_dedup_scopes_near_duplicates_to_their_answer_intent(self) -> None:
        state = ResearchState(
            run_id="target-scoped-query-dedup",
            question="Why does 3D help 2D retrieval?",
            queries=[
                Query(
                    "3D structural pose features improve 2D retrieval under viewpoint changes paper",
                    "sg-core",
                    "source_targeting",
                )
            ],
        )
        engine = ResearchEngine(
            AppConfig(),
            MockModelProvider(),
            ReplaySearchProvider(Path("examples/replay_corpus.json")),
        )

        selected = engine._deduplicate_queries(
            state,
            [
                Query(
                    "3D structural pose features improve 2D retrieval under viewpoint changes",
                    "sg-limitations",
                    "source_targeting",
                ),
                Query(
                    "3D structural pose features improve 2D retrieval under viewpoint changes",
                    "sg-core",
                    "source_targeting",
                ),
            ],
        )

        self.assertEqual([item.subgoal_id for item in selected], ["sg-limitations"])
        self.assertEqual(state.counters.duplicate_queries, 1)

    def test_query_dedup_keeps_alternate_lens_for_missing_independent_source(self) -> None:
        plan = ResearchPlan(
            "text",
            [AnswerSlot("methods", "Recent ReID method advances")],
            [
                Subgoal(
                    "sg-methods",
                    "Find recent ReID method advances",
                    ["methods"],
                    "done",
                )
            ],
        )
        state = ResearchState(
            run_id="source-recovery-query",
            question="What are the latest ReID advances?",
            plan=plan,
            pending_gaps=[
                EvidenceGap(
                    "missing_independent_source",
                    "methods",
                    "Recent ReID method advances",
                )
            ],
            queries=[
                Query(
                    "Find recent ReID method advances transformer multimodal paper",
                    "sg-methods",
                    "source_targeting",
                )
            ],
        )
        engine = ResearchEngine(
            AppConfig(),
            MockModelProvider(),
            ReplaySearchProvider(Path("examples/replay_corpus.json")),
        )

        selected = engine._deduplicate_queries(
            state,
            [
                Query(
                    "Find recent ReID method advances transformer multimodal independent source",
                    "sg-methods",
                    "source_targeting",
                )
            ],
        )

        self.assertEqual(len(selected), 1)
        self.assertEqual(state.counters.duplicate_queries, 0)

    def test_transient_contradiction_search_uses_independent_source_review(self) -> None:
        source_a = SourceRecord(
            id="source-a",
            url="https://papers.example/a",
            title="Benchmark A",
            source_type="paper",
            snippet="",
            status="fetched",
        )
        source_b = SourceRecord(
            id="source-b",
            url="https://papers.example/b",
            title="Benchmark B",
            source_type="paper",
            snippet="",
            status="fetched",
        )

        def evidence(identifier: str, source: SourceRecord, cluster: str) -> Evidence:
            return Evidence(
                id=identifier,
                subgoal_id="sg-benchmarks",
                slot_id="benchmarks",
                claim="A ReID benchmark reports a robustness result.",
                quote="A ReID benchmark reports a robustness result.",
                source_url=source.url,
                source_title=source.title,
                stance="supports",
                reliability=0.9,
                extraction_confidence=1.0,
                content_hash=f"hash-{identifier}",
                source_cluster_id=cluster,
                source_id=source.id,
                origin_cluster_id=cluster,
                independence_status="distinct_scholarly_work",
                slot_relevance_score=0.95,
                claim_quote_consistency=1.0,
                fetch_record_id=f"fetch-{identifier}",
                snapshot_sha256="a" * 64,
                snapshot_available=True,
                fetch_binding_status="server_bound",
                fetch_binding_valid=True,
                content_hash_scope="full_extracted_text",
            )

        audit = ContradictionAudit(
            slot_id="benchmarks",
            query_text="ReID benchmark counterevidence",
            status="search_failed",
            executed_at="2026-07-23T00:00:00+00:00",
            error="HTTP Error 429: Too Many Requests",
        )
        state = ResearchState(
            run_id="rate-limited-contradiction",
            question="What are current ReID benchmarks?",
            sources=[source_a, source_b],
            evidence=[
                evidence("E1", source_a, "work-a"),
                evidence("E2", source_b, "work-b"),
            ],
            contradiction_checks=[audit],
        )
        engine = ResearchEngine(
            AppConfig(),
            MockModelProvider(),
            ReplaySearchProvider(Path("examples/replay_corpus.json")),
        )

        engine._finalize_contradiction_checks(state)

        self.assertEqual(audit.status, "cross_source_review_after_search_failure")
        self.assertEqual(audit.relevant_source_ids, ["source-a", "source-b"])
        self.assertIn("benchmarks", state.contradiction_checked_slots)
        self.assertIn("rate-limit fallback", audit.error or "")

    async def test_open_closure_uses_a_bounded_budget_recovery_tranche(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = Path("examples/replay_corpus.json")
            config = AppConfig(runs_dir=root / "runs", replay_corpus=corpus)
            config.budget.max_iterations = 1
            config.budget.max_search_calls = 3
            config.budget.max_pages = 4
            config.budget.max_total_iterations = 3
            config.budget.max_total_search_calls = 7
            config.budget.max_total_pages = 10

            state = await ResearchEngine(
                config,
                MockModelProvider(),
                EmptyContradictionSearchProvider(corpus),
            ).run(
                "Who created Python and when was it first released?",
                run_id="bounded-budget-recovery",
            )

            self.assertEqual(state.status, "evidence_incomplete")
            self.assertTrue(state.budget_expansions)
            self.assertLessEqual(state.budget_limits["iterations"], 2)
            self.assertLessEqual(state.budget_limits["search_calls"], 5)
            self.assertLessEqual(state.budget_limits["pages"], 7)
            self.assertGreaterEqual(
                state.budget_ceilings["iterations"]
                - state.budget_limits["iterations"],
                1,
            )
            self.assertGreaterEqual(
                state.budget_ceilings["search_calls"]
                - state.budget_limits["search_calls"],
                2,
            )
            self.assertGreaterEqual(
                state.budget_ceilings["pages"] - state.budget_limits["pages"],
                3,
            )
            latest_expansion = state.budget_expansions[-1]
            self.assertEqual(
                latest_expansion["manual_resume_reserve"],
                {"iterations": 1, "search_calls": 2, "pages": 3},
            )
            self.assertIn("当前可交付回答", state.draft_answer)
            self.assertIn(
                "compose_limited_answer",
                {item.operation for item in state.agent_invocations},
            )
            self.assertIn(
                "check_limited_delivery",
                {item.operation for item in state.agent_invocations},
            )

    def test_claim_quote_consistency_rejects_added_year_and_negation(self) -> None:
        score, reasons = _claim_quote_consistency(
            "Python was not released in 1989.",
            "Python was released in 1991.",
        )
        self.assertEqual(score, 0.0)
        self.assertTrue(any("1989" in reason for reason in reasons))

    def test_stage_answer_explains_3d_2d_retrieval_without_diagram_labels(self) -> None:
        state = ResearchState(
            run_id="3d-2d-stage-answer",
            question="为什么3d可以增益纯2d的检索？",
            evidence=[
                Evidence(
                    id="E6308dbaf",
                    subgoal_id="fusion",
                    slot_id="mechanism",
                    claim="3D分支提供姿态/结构信息，并通过可靠性门控补充2D视觉特征",
                    quote="3D分支提供姿态/结构信息，并通过可靠性门控补充2D视觉特征",
                    source_url="attachment://I560f5a81655",
                    source_title="用户上传图示",
                    stance="supports",
                    reliability=0.7,
                    extraction_confidence=0.95,
                    content_hash="attachment-fusion",
                    source_cluster_id="attachment",
                    attachment_id="I560f5a81655",
                ),
                Evidence(
                    id="Ec5c45c38",
                    subgoal_id="structure",
                    slot_id="mechanism",
                    claim="3D representation captures not only 3D location but also detailed shape and body texture.",
                    quote="3D representation offers advantages, capturing not only 3D location but also detailed shape and body texture.",
                    source_url="https://example.test/3d-representation",
                    source_title="External 3D representation study",
                    stance="supports",
                    reliability=0.9,
                    extraction_confidence=0.95,
                    content_hash="external-structure",
                    source_cluster_id="external",
                ),
                Evidence(
                    id="Eceff5c43",
                    subgoal_id="fusion",
                    slot_id="mechanism",
                    claim="Scalable Edge Deployment, α=0, f_out",
                    quote="Scalable Edge Deployment, α=0, f_out",
                    source_url="attachment://I560f5a81655",
                    source_title="用户上传图示",
                    stance="supports",
                    reliability=0.7,
                    extraction_confidence=0.9,
                    content_hash="attachment-alpha",
                    source_cluster_id="attachment",
                    attachment_id="I560f5a81655",
                ),
            ],
            closure=ClosureReport(
                closed=False,
                score=0.0,
                slot_coverage=0.0,
                source_independence=0.0,
                evidence_entailment=0.0,
                source_reliability=0.0,
                conflict_resolution=0.0,
                slot_audits=[
                    SlotGateAudit(
                        slot_id="mechanism",
                        description="3D 与 2D 特征融合机制",
                        passed=False,
                        supporting_evidence_ids=["Ec5c45c38"],
                    )
                ],
            ),
        )

        answer, evidence_ids = _compose_evidence_limited_answer(state)

        self.assertIn("3D 可以增强纯 2D 检索", answer)
        self.assertIn("外观和结构一起用于相似度匹配", answer)
        self.assertIn("[E6308dbaf]", answer)
        self.assertIn("[Ec5c45c38]", answer)
        self.assertIn("[Eceff5c43]", answer)
        self.assertNotIn("- Scalable Edge Deployment, α=0, f_out", answer)
        self.assertEqual(
            set(evidence_ids),
            {"E6308dbaf", "Ec5c45c38", "Eceff5c43"},
        )

    def test_stage_answer_explains_3d_2d_retrieval_from_attachment_observation(self) -> None:
        state = ResearchState(
            run_id="3d-2d-attachment-only-answer",
            question="为什么3d可以增益纯2d的检索？",
            attachment_observations=[
                AttachmentObservation(
                    attachment_id="I560f5a81655",
                    modality="image",
                    summary=(
                        "图中显示 Scene-Aware Visual Stream 生成 f_vis，"
                        "3D Auxiliary Structural Stream 生成 f_pose，"
                        "Reliability-Aware Gating 融合为 f_out。"
                    ),
                    observations=[
                        GroundedObservation(
                            locator="page 1 / left blue stream",
                            text="2D visual stream produces f_vis from image tokens.",
                            confidence=0.95,
                        ),
                        GroundedObservation(
                            locator="page 1 / right structural stream",
                            text=(
                                "3D pose structural stream produces f_pose and "
                                "Reliability-Aware Gating outputs f_out."
                            ),
                            confidence=0.95,
                        ),
                    ],
                )
            ],
        )

        answer, evidence_ids = _compose_evidence_limited_answer(state)

        self.assertIn("姿态、几何结构和相对空间关系", answer)
        self.assertIn("图片中这条链路怎么读", answer)
        self.assertIn("2D 视觉支路", answer)
        self.assertIn("3D 辅助结构支路", answer)
        self.assertIn("可靠性门控", answer)
        self.assertIn("page 1 / right structural stream", answer)
        self.assertEqual(evidence_ids, [])

    def test_claim_target_relevance_rejects_explicitly_unrelated_claim(self) -> None:
        score, reasons = _claim_target_relevance(
            "This page describes an unrelated language and contains no facts about Python.",
            "Who created Python?",
        )
        self.assertEqual(score, 0.0)
        self.assertIn("excluded from support", reasons[0])

    def test_claim_target_relevance_rejects_unrelated_subject(self) -> None:
        unrelated, _ = _claim_target_relevance(
            "Python was created by Guido van Rossum.",
            "launch date of Project Zephyr X",
        )
        related, _ = _claim_target_relevance(
            "Python was created by Guido van Rossum.",
            "The creator of Python",
        )
        self.assertEqual(unrelated, 0.0)
        self.assertGreaterEqual(related, 0.45)

    def test_claim_target_relevance_uses_best_recorded_target_formulation(self) -> None:
        score, reasons = _claim_target_relevance_variants(
            "Python Documentation: History and License. Python was created by Guido van Rossum.",
            [
                "Name of the person(s) who created Python.",
                "Who is credited with creating the Python programming language?",
            ],
        )

        self.assertGreaterEqual(score, 0.45)
        self.assertIn("2 target formulations", reasons[0])

    def test_bound_route_bridges_mixed_language_claim_but_not_unrelated_subject(self) -> None:
        query = Query(
            "3D pose structural features improve 2D retrieval under viewpoint changes",
            "sg-core",
            "source_targeting",
        )
        plan = ResearchPlan(
            "text",
            [AnswerSlot("core", "说明3D结构特征如何补充2D检索")],
            [Subgoal("sg-core", "3D结构特征与2D检索的关系", ["core"], "done")],
        )
        source = SourceRecord(
            id="Sroute",
            url="https://papers.example/3d-retrieval",
            title="Auxiliary geometry study",
            source_type="paper",
            snippet="",
            query_texts=[query.text],
        )
        evidence = Evidence(
            id="Eroute",
            subgoal_id="sg-core",
            slot_id="core",
            claim="3D representation provides geometric cues.",
            quote="3D representation provides geometric cues.",
            source_url=source.url,
            source_title=source.title,
            stance="supports",
            reliability=0.8,
            extraction_confidence=0.95,
            content_hash="hash-route",
            source_cluster_id="papers.example",
        )
        state = ResearchState(
            run_id="mixed-language-route",
            question="为什么3D可以增益纯2D检索？",
            plan=plan,
            queries=[query],
            sources=[source],
        )
        engine = ResearchEngine(
            AppConfig(),
            MockModelProvider(),
            ReplaySearchProvider(Path("examples/replay_corpus.json")),
        )
        engine._attach_provenance(state, [evidence])
        self.assertGreaterEqual(evidence.slot_relevance_score, 0.45)
        self.assertIn("检索声明锚点", " ".join(evidence.slot_relevance_reasons))

        unrelated_query = Query(
            "launch date of Project Zephyr X official history",
            "sg-core",
            "source_targeting",
        )
        unrelated_source = SourceRecord(
            id="Sunrelated",
            url="https://papers.example/python",
            title="Python creator reference",
            source_type="reference",
            snippet="",
            query_texts=[unrelated_query.text],
        )
        unrelated = Evidence(
            id="Eunrelated",
            subgoal_id="sg-core",
            slot_id="core",
            claim="Python was created by Guido van Rossum.",
            quote="Python was created by Guido van Rossum.",
            source_url=unrelated_source.url,
            source_title=unrelated_source.title,
            stance="supports",
            reliability=0.8,
            extraction_confidence=0.95,
            content_hash="hash-unrelated",
            source_cluster_id="papers.example",
        )
        unrelated_state = ResearchState(
            run_id="unrelated-route",
            question="What is the launch date of Project Zephyr X?",
            plan=plan,
            queries=[unrelated_query],
            sources=[unrelated_source],
        )
        engine._attach_provenance(unrelated_state, [unrelated])
        self.assertEqual(unrelated.slot_relevance_score, 0.0)

    def test_future_route_does_not_promote_a_claim_without_a_future_signal(self) -> None:
        query = Query(
            "person ReID future directions foundation model deployment",
            "sg-future",
            "source_targeting",
        )
        plan = ResearchPlan(
            "text",
            [AnswerSlot("future", "Future research directions for ReID")],
            [Subgoal("sg-future", "Find future ReID directions", ["future"], "done")],
        )
        source = SourceRecord(
            id="Sfuture",
            url="https://papers.example/future",
            title="ReID survey",
            source_type="paper",
            snippet="",
            query_texts=[query.text],
        )
        evidence = Evidence(
            id="Efuture",
            subgoal_id="sg-future",
            slot_id="future",
            claim="The model keeps the most recent gallery image.",
            quote="The model keeps the most recent gallery image.",
            source_url=source.url,
            source_title=source.title,
            stance="supports",
            reliability=0.8,
            extraction_confidence=0.95,
            content_hash="future-route",
            source_cluster_id="papers.example",
        )
        state = ResearchState(
            run_id="future-route",
            question="What are future directions for ReID?",
            plan=plan,
            queries=[query],
            sources=[source],
        )
        engine = ResearchEngine(
            AppConfig(),
            MockModelProvider(),
            ReplaySearchProvider(Path("examples/replay_corpus.json")),
        )

        engine._attach_provenance(state, [evidence])

        self.assertLess(evidence.slot_relevance_score, 0.45)
        self.assertIn("future 不能替代", " ".join(evidence.slot_relevance_reasons))

    def test_query_coverage_uses_overlapping_required_subgoals_without_another_model_call(self) -> None:
        plan = ResearchPlan(
            "text",
            [
                AnswerSlot("core", "Core explanation"),
                AnswerSlot("mechanism", "Mechanism"),
                AnswerSlot("failure", "Failure cases"),
                AnswerSlot("framework", "Framework mapping"),
                AnswerSlot("gating", "Gating value"),
                AnswerSlot("optional", "Optional caveat", required=False),
            ],
            [
                Subgoal("sg-core", "Core and mechanism evidence", ["core", "mechanism"], "done"),
                Subgoal("sg-failure", "Failure evidence", ["failure"], "done"),
                Subgoal("sg-framework", "Framework and gating evidence", ["framework", "gating"], "done"),
                Subgoal("sg-optional", "Optional caveat", ["optional"], "done"),
            ],
        )
        state = ResearchState(
            run_id="query-coverage",
            question="Why does 3D help 2D tracking?",
            plan=plan,
        )
        provider_queries = [
            Query("core tracking evidence", "sg-core", "source_targeting"),
            Query("failure cases evidence", "sg-failure", "source_targeting"),
            Query("optional caveat evidence", "sg-optional", "source_targeting"),
        ]

        selected = ResearchEngine._enforce_required_query_coverage(
            state,
            provider_queries,
            [],
        )

        self.assertEqual(
            [item.subgoal_id for item in selected],
            ["sg-core", "sg-framework", "sg-failure"],
        )
        self.assertEqual(len(selected), 3)
        self.assertIn("Framework and gating evidence", selected[1].text)

    def test_query_coverage_rotates_to_untried_required_subgoals(self) -> None:
        plan = ResearchPlan(
            "text",
            [
                AnswerSlot("scope", "Scope"),
                AnswerSlot("methods", "Methods"),
                AnswerSlot("benchmarks", "Benchmarks"),
                AnswerSlot("challenges", "Challenges"),
                AnswerSlot("future", "Future directions"),
            ],
            [
                Subgoal("sg-scope", "Scope evidence", ["scope"], "done"),
                Subgoal("sg-methods", "Method evidence", ["methods"], "done"),
                Subgoal("sg-benchmarks", "Benchmark evidence", ["benchmarks"], "done"),
                Subgoal("sg-challenges", "Challenge evidence", ["challenges"], "done"),
                Subgoal("sg-future", "Future evidence", ["future"], "done"),
            ],
        )
        history = [
            Query("scope route", "sg-scope", "source_targeting"),
            Query("methods route", "sg-methods", "source_targeting"),
            Query("benchmarks route", "sg-benchmarks", "source_targeting"),
        ]

        selected = _required_query_coverage_subgoals(plan, [], history=history)

        self.assertEqual(
            [item.id for item in selected],
            ["sg-challenges", "sg-future", "sg-scope"],
        )

    def test_page_budget_round_robins_across_query_intents(self) -> None:
        groups = [
            [
                SearchResult("first-a", "https://a.example/1", ""),
                SearchResult("second-a", "https://a.example/2", ""),
            ],
            [
                SearchResult("first-b", "https://b.example/1", ""),
                SearchResult("second-b", "https://b.example/2", ""),
            ],
            [SearchResult("first-c", "https://c.example/1", "")],
        ]

        selected = _round_robin_results(groups, 4)

        self.assertEqual(
            [item.title for item in selected],
            ["first-a", "first-b", "first-c", "second-a"],
        )


class CountingMockModelProvider(MockModelProvider):
    def __init__(self) -> None:
        self.draft_calls = 0

    async def draft(self, question, plan, evidence):
        self.draft_calls += 1
        return await super().draft(question, plan, evidence)


class CountingPlanModel(MockModelProvider):
    def __init__(self) -> None:
        self.plan_calls = 0

    async def plan(self, question):
        self.plan_calls += 1
        return await super().plan(question)


class LiveUsagePlanModel(MockModelProvider):
    def __init__(self) -> None:
        self.first_response = asyncio.Event()
        self.allow_completion = asyncio.Event()
        self._listeners = {}
        self._listener_id = 0
        self._usage = {
            "model_calls": 0,
            "model_cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
        }

    def add_usage_listener(self, listener):
        self._listener_id += 1
        self._listeners[self._listener_id] = listener
        return self._listener_id

    def remove_usage_listener(self, listener_id):
        self._listeners.pop(listener_id, None)

    def usage_snapshot(self):
        return {
            **self._usage,
            "provider": "live-test",
            "pricing_configured": True,
            "pricing_status": "complete",
            "pricing_reason": "test-only configured rate",
        }

    def _record_response(self, input_tokens, output_tokens, cost):
        response = {
            "model_calls": 1,
            "model_cache_hits": 0,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": cost,
            "provider": "live-test",
            "pricing_configured": True,
            "pricing_status": "complete",
            "pricing_reason": "test-only configured rate",
        }
        for key in self._usage:
            self._usage[key] += response[key]
        for listener in list(self._listeners.values()):
            listener(dict(response))

    async def plan(self, question):
        self._record_response(100, 20, 0.0012)
        self.first_response.set()
        await self.allow_completion.wait()
        self._record_response(60, 10, 0.00075)
        return await super().plan(question)


class DieDuringPlanModel(MockModelProvider):
    def __init__(self, should_die: bool) -> None:
        self.should_die = should_die
        self.plan_calls = 0

    async def plan(self, question):
        self.plan_calls += 1
        if self.should_die:
            raise SimulatedProcessCrash()
        return await super().plan(question)


class FailBeforeRequestPlanModel(MockModelProvider):
    async def plan(self, question):
        raise ProviderRequestNotSent("injected pre-request TLS failure")


class EmptyContradictionSearchProvider(ReplaySearchProvider):
    async def search(self, query, limit=5):
        if query.strategy == "contradiction_check":
            return []
        return await super().search(query, limit)


class IrrelevantContradictionSearchProvider(ReplaySearchProvider):
    async def search(self, query, limit=5):
        results = await super().search(query, limit)
        if query.strategy == "contradiction_check":
            return [
                item for item in results if item.url.endswith("/other-language")
            ]
        return results


class CountingReplaySearchProvider(ReplaySearchProvider):
    def __init__(self, corpus_path):
        super().__init__(corpus_path)
        self.search_calls = 0
        self.fetch_calls = 0

    async def search(self, query, limit=5):
        self.search_calls += 1
        return await super().search(query, limit)

    async def fetch(self, result):
        self.fetch_calls += 1
        return await super().fetch(result)


class CapturingEvidenceModel(MockModelProvider):
    def __init__(self) -> None:
        self.draft_evidence_ids: set[str] = set()
        self.verify_evidence_ids: set[str] = set()

    async def draft(self, question, plan, evidence):
        self.draft_evidence_ids = {item.id for item in evidence}
        return await super().draft(question, plan, evidence)

    async def verify(self, answer, evidence):
        self.verify_evidence_ids = {item.id for item in evidence}
        return await super().verify(answer, evidence)


class FailFirstExtractModelProvider(MockModelProvider):
    def __init__(self):
        self.failed = False

    async def extract_evidence(self, plan, pages):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated extract crash")
        return await super().extract_evidence(plan, pages)


class FailFirstVerifyModelProvider(MockModelProvider):
    async def verify(self, answer, evidence):
        raise RuntimeError("simulated verifier crash")


class TimeoutVerifyModelProvider(MockModelProvider):
    async def verify(self, answer, evidence):
        raise RuntimeError("Model API HTTP 524; no successful result was returned")


class Gateway520VerifyModelProvider(MockModelProvider):
    async def verify(self, answer, evidence):
        raise RuntimeError("Model API HTTP 520; no successful result was returned")


class CountingAllCallsMockModelProvider(MockModelProvider):
    def __init__(self):
        self.calls = 0

    async def plan(self, question):
        self.calls += 1
        return await super().plan(question)

    async def generate_queries(self, question, plan, gaps, history):
        self.calls += 1
        return await super().generate_queries(question, plan, gaps, history)

    async def extract_evidence(self, plan, pages):
        self.calls += 1
        return await super().extract_evidence(plan, pages)

    async def draft(self, question, plan, evidence):
        self.calls += 1
        return await super().draft(question, plan, evidence)

    async def verify(self, answer, evidence):
        self.calls += 1
        return await super().verify(answer, evidence)


class SimulatedProcessCrash(BaseException):
    pass


class CrashAfterVerifyEngine(ResearchEngine):
    def _save(self, store, node, state, payload):
        ResearchEngine._save(store, node, state, payload)
        if node == "verify" and payload.get("passed"):
            raise SimulatedProcessCrash()


class CrashAfterResumeHandoffEngine(ResearchEngine):
    def _ensure_resume_handoff(self, store, state):
        super()._ensure_resume_handoff(store, state)
        raise SimulatedProcessCrash()


class CrashAfterPlanOperationEngine(ResearchEngine):
    def _after_operation_completed(self, node, operation_key):
        if node == "plan":
            raise SimulatedProcessCrash()


class CrashAfterSearchOperationEngine(ResearchEngine):
    def _after_operation_completed(self, node, operation_key):
        if node == "search":
            raise SimulatedProcessCrash()


class CrashAfterFetchOperationEngine(ResearchEngine):
    def _after_operation_completed(self, node, operation_key):
        if node == "fetch":
            raise SimulatedProcessCrash()


class CrashAfterVerifyOperationEngine(ResearchEngine):
    def _after_operation_completed(self, node, operation_key):
        if node == "verify":
            raise SimulatedProcessCrash()


class FenceLostAfterPlanOperationEngine(ResearchEngine):
    def _after_operation_completed(self, node, operation_key):
        if node == "plan":
            raise ExecutionFenceLostError("simulated execution fence loss")


class PassedEmptyVerifierModel(MockModelProvider):
    def __init__(self) -> None:
        self.verify_calls = 0

    async def verify(self, answer, evidence):
        self.verify_calls += 1
        return VerificationReport(passed=True, items=[])


if __name__ == "__main__":
    unittest.main()
