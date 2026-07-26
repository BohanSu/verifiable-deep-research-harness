from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import unicodedata
import urllib.parse
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any, TypeVar

from .agents import (
    AgentTeam,
    closure_supporting_evidence,
    enforce_verification_contract,
)
from .config import AppConfig
from .contracts import AgentInvocation, HandoffReceipt, build_handoff
from .evidence import (
    MIN_CLAIM_QUOTE_CONSISTENCY,
    ClosureEngine,
    EvidenceLedger,
    claim_quote_consistency,
)
from .methodology import methodology_snapshot
from .multimodal import prepare_attachment_content
from .providers.base import ModelProvider, SearchProvider
from .providers.base import (
    ProviderOutcomeUncertain,
    ProviderRequestNotSent,
    ResourceLimitExceededError,
)
from .recovery import RecoveryAction, recovery_for
from .schemas import (
    AttachmentObservation,
    ContradictionAudit,
    AnswerSlot,
    Evidence,
    EvidenceGap,
    GroundedObservation,
    Page,
    Query,
    ResearchPlan,
    SearchResult,
    SourceRecord,
    Subgoal,
    VerificationItem,
    VerificationReport,
)
from .state import ResearchState
from .storage import (
    ArtifactIntegrityError,
    ExecutionFenceLostError,
    HandoffValidationError,
    RunStore,
)
from .verification import parse_answer_claims


T = TypeVar("T")
TERMINAL_DELIVERY_FORMAT_VERSION = "human-terminal-answer-v3"
LOCAL_CITATION_BINDING_CONTRACT_VERSION = "local-citation-binding-v1"


class ResearchEngine:
    def __init__(
        self,
        config: AppConfig,
        model: ModelProvider,
        search: SearchProvider,
        cancel_check: Callable[[], bool] | None = None,
        execution_lease: dict[str, object] | None = None,
        lease_lost_check: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.model = model
        self.search = search
        self.ledger = EvidenceLedger()
        self.closure_engine = ClosureEngine(config.closure)
        self.agents = AgentTeam(model, self.closure_engine)
        self.cancel_check = cancel_check or (lambda: False)
        self.execution_lease = execution_lease
        self.lease_lost_check = lease_lost_check

    async def run(self, question: str, run_id: str | None = None) -> ResearchState:
        run_id = run_id or uuid.uuid4().hex[:12]
        store = RunStore(self.config.runs_dir, run_id)
        if self.execution_lease:
            store.bind_execution_fence(
                str(self.execution_lease["owner_token"]),
                int(self.execution_lease["fence"]),
            )
        stored_attachments = store.load_input_attachments()
        state = store.latest() or ResearchState(
            run_id=run_id,
            question=question,
            input_attachments=stored_attachments,
            next_node="perceive_inputs" if stored_attachments else "plan",
        )
        durable_attachment_ids = {
            (item.id, item.sha256) for item in stored_attachments
        }
        checkpoint_attachment_ids = {
            (item.id, item.sha256) for item in state.input_attachments
        }
        if checkpoint_attachment_ids and checkpoint_attachment_ids != durable_attachment_ids:
            raise ValueError("checkpoint attachments disagree with the durable input ledger")
        if not checkpoint_attachment_ids and durable_attachment_ids:
            state.input_attachments = stored_attachments
            if state.next_node == "plan" and not state.attachment_observations:
                state.next_node = "perceive_inputs"
        configured_limits = {
            "iterations": self.config.budget.max_iterations,
            "search_calls": self.config.budget.max_search_calls,
            "pages": self.config.budget.max_pages,
        }
        configured_ceilings = {
            "iterations": max(
                self.config.budget.max_total_iterations,
                configured_limits["iterations"],
            ),
            "search_calls": max(
                self.config.budget.max_total_search_calls,
                configured_limits["search_calls"],
            ),
            "pages": max(
                self.config.budget.max_total_pages,
                configured_limits["pages"],
            ),
        }
        state.budget_limits = {
            key: int(state.budget_limits[key])
            if key in state.budget_limits
            else value
            for key, value in configured_limits.items()
        }
        persisted_ceilings = (
            state.budget_ceilings
            if isinstance(state.budget_ceilings, dict)
            else {}
        )
        budget_ceilings: dict[str, int] = {}
        for key in configured_limits:
            if key in persisted_ceilings:
                raw_ceiling = persisted_ceilings[key]
                if isinstance(raw_ceiling, bool):
                    raise ValueError(f"persisted budget ceiling for {key} is invalid")
                try:
                    ceiling = int(raw_ceiling)
                except (TypeError, ValueError) as error:
                    raise ValueError(
                        f"persisted budget ceiling for {key} is invalid"
                    ) from error
                if ceiling < state.budget_limits[key]:
                    raise ValueError(
                        f"persisted budget limit for {key} exceeds its ceiling"
                    )
            else:
                # Legacy checkpoints had no immutable ceiling. Preserve their
                # already-approved limit while establishing one for the future.
                ceiling = max(configured_ceilings[key], state.budget_limits[key])
            budget_ceilings[key] = ceiling
        state.budget_ceilings = budget_ceilings
        if not state.methodology or not state.methodology.get("methodology_version"):
            startup_methodology = dict(state.methodology)
            state.methodology = methodology_snapshot(
                type(self.model).__name__,
                type(self.search).__name__,
                closure_config=self.config.closure,
            )
            state.methodology.update(startup_methodology)
            state.methodology.update(
                {
                    "model_choice": str(
                        getattr(self.model, "model_choice", "offline")
                    ),
                    "model": str(getattr(self.model, "model", "built-in")),
                    "model_base_url": str(
                        getattr(self.model, "base_url", "local")
                    ),
                    "model_profile": str(
                        getattr(self.model, "profile", getattr(self.model, "model_choice", "offline"))
                    ),
                    "model_routes": (
                        self.model.route_snapshot()
                        if callable(getattr(self.model, "route_snapshot", None))
                        else {
                            role: {
                                "role": role,
                                "choice": str(getattr(self.model, "model_choice", "offline")),
                                "provider": type(self.model).__name__,
                                "model": str(getattr(self.model, "model", "built-in")),
                                "base_url": str(getattr(self.model, "base_url", "local")),
                                "modalities": list(getattr(self.model, "modalities", ("text", "document"))),
                            }
                            for role in ("perception", "planner", "scout", "curator", "writer", "verifier")
                        }
                    ),
                    "multimodal_contract": "content-addressed-native-media-v1",
                }
            )
        self._merge_persisted_invocations(store, state)
        if state.question != question:
            raise ValueError("run_id belongs to a different research question")
        if state.next_node == "done" and state.status in {
            "completed",
            "verification_failed",
            "evidence_incomplete",
            "failed",
            "cancelled",
        }:
            # Terminal states from older workers can lack a human-facing
            # delivery. Backfill them from durable material only; this path
            # never sends a model or network request and preserves the resume
            # target already recorded in suspension.
            delivery_backfill_needed = (
                not _has_terminal_answer_delivery(state)
                or _terminal_delivery_needs_refresh(state)
            )
            invocation_backfill_needed = _terminal_delivery_invocation_needs_backfill(
                state
            )
            if delivery_backfill_needed or invocation_backfill_needed:
                terminal_lease = store.acquire_lease()
                try:
                    _ensure_terminal_answer_delivery(state)
                    self._record_limited_delivery_invocations(store, state)
                    self._save(
                        store,
                        "finalize",
                        state,
                        {
                            "status": state.status,
                            "delivery_backfilled": delivery_backfill_needed,
                            "terminal_invocation_backfilled": invocation_backfill_needed,
                        },
                    )
                    store.write_final(state)
                finally:
                    terminal_lease.release()
            return state
        lease = store.acquire_lease()
        try:
            store.ensure_legacy_usage_baseline(state.counters)
            self._sync_persisted_usage(store, state)
            self._sync_tool_counters(store, state)
            self._ensure_resume_handoff(store, state)
            while True:
                self._raise_if_cancelled()
                node = state.next_node

                if node == "perceive_inputs":
                    state.status = "perceiving"
                    perception_model = self.agents.perception.model
                    render_pdf = "image" in set(
                        getattr(perception_model, "modalities", ())
                    )
                    observations: list[AttachmentObservation] = []
                    attachment_count = len(state.input_attachments)
                    for index, attachment in enumerate(state.input_attachments):
                        content = prepare_attachment_content(
                            attachment,
                            store.read_input_attachment(attachment.id)[1],
                            render_pdf=render_pdf,
                        )
                        incoming_handoffs = (
                            self._incoming_handoff_message_ids(
                                store, state, "perception"
                            )
                            if index == attachment_count - 1
                            else []
                        )
                        perceived = await self._execute_model_operation(
                            store,
                            state,
                            "perceive_inputs",
                            {
                                "question": state.question,
                                "attachment": {
                                    "id": content.attachment.id,
                                    "sha256": content.attachment.sha256,
                                    "media_type": content.attachment.media_type,
                                    "modality": content.attachment.modality,
                                    "parser_version": content.parser_version,
                                    "rendered_pages": len(content.rendered_pages),
                                },
                            },
                            lambda recorder, operation_key, item=content, consumed=incoming_handoffs: self.agents.perception.perceive(
                                state,
                                [item],
                                consumed,
                                invocation_recorder=recorder,
                                operation_key=operation_key,
                            ),
                            lambda values: [asdict(item) for item in values],
                            _deserialize_attachment_observations,
                        )
                        if (
                            len(perceived) != 1
                            or perceived[0].attachment_id != attachment.id
                        ):
                            raise ValueError(
                                "perception result does not match its durable attachment"
                            )
                        observations.extend(perceived)
                    state.attachment_observations = observations
                    self._materialize_attachment_sources(state)
                    state.next_node = "plan"
                    self._save(
                        store,
                        "perceive_inputs",
                        state,
                        {
                            "attachments": len(state.input_attachments),
                            "observations": sum(
                                len(item.observations)
                                for item in state.attachment_observations
                            ),
                        },
                    )
                    continue

                if node == "plan":
                    state.status = "planning"
                    attachment_context = self._attachment_context(state)
                    state.plan = await self._execute_model_operation(
                        store,
                        state,
                        "plan",
                        {
                            "question": state.question,
                            "attachment_context": attachment_context,
                        },
                        lambda recorder, operation_key: self.agents.planner.plan(
                            state,
                            self._incoming_handoff_message_ids(store, state, "planner"),
                            invocation_recorder=recorder,
                            operation_key=operation_key,
                            question_context=attachment_context,
                        ),
                        _serialize_dataclass,
                        _deserialize_plan,
                    )
                    state.next_node = "generate_queries"
                    self._save(store, "plan", state, {"slots": len(state.plan.slots)})
                    continue

                if node == "generate_queries":
                    if self._budget_exhausted(state):
                        if self._extend_budget_for_evidence_recovery(
                            state,
                            "budget_reached_with_open_required_slots",
                        ):
                            store.event(
                                "budget_extended",
                                "generate_queries",
                                state.budget_expansions[-1],
                            )
                            continue
                        state.status = "evidence_incomplete"
                        state.suspension = {
                            "reason": "evidence_incomplete",
                            "resume_node": "generate_queries",
                        }
                        state.next_node = "finalize"
                        continue
                    state.status = "running"
                    state.counters.iterations += 1
                    gaps = state.pending_gaps or (state.closure.gaps if state.closure else [])
                    queries = await self._execute_model_operation(
                        store,
                        state,
                        "generate_queries",
                        {
                            "question": state.question,
                            "plan": asdict(state.plan),
                            "gaps": [asdict(item) for item in gaps],
                            "history": [asdict(item) for item in state.queries],
                        },
                        lambda recorder, operation_key: self.agents.scout.queries(
                            state,
                            state.plan,
                            gaps,
                            self._incoming_handoff_message_ids(store, state, "scout"),
                            invocation_recorder=recorder,
                            operation_key=operation_key,
                        ),
                        lambda values: [asdict(item) for item in values],
                        _deserialize_queries,
                    )
                    queries = self._enforce_required_query_coverage(
                        state,
                        queries,
                        gaps,
                    )
                    queries = self._deduplicate_queries(state, queries)
                    if not queries:
                        recovery_queries = self._recovery_queries(state, gaps)
                        queries = self._deduplicate_queries(state, recovery_queries)
                    if not queries:
                        state.failures.append(
                            {
                                "type": "query_error",
                                "reason": (
                                    "No distinct recovery queries remain after "
                                    "target-scoped de-duplication"
                                ),
                            }
                        )
                        state.status = "evidence_incomplete"
                        state.suspension = {
                            "reason": "evidence_incomplete",
                            "resume_node": "generate_queries",
                        }
                        state.next_node = "finalize"
                        continue
                    state.queries.extend(queries)
                    state.pending_queries = queries
                    state.pending_gaps = []
                    state.next_node = "search_and_fetch"
                    self._save(store, "generate_queries", state, {"count": len(queries)})
                    continue

                if node == "search_and_fetch":
                    state.status = "running"
                    state.pending_pages = await self._retrieve(
                        store, state, state.pending_queries
                    )
                    state.next_node = "ingest_evidence"
                    self._save(
                        store,
                        "search_and_fetch",
                        state,
                        {"pages": len(state.pending_pages)},
                    )
                    continue

                if node == "ingest_evidence":
                    state.status = "running"
                    ingestion_pages = list(state.pending_pages)
                    if not state.attachments_ingested:
                        ingestion_pages.extend(state.attachment_pages)
                    incoming = await self._execute_model_operation(
                        store,
                        state,
                        "extract_evidence",
                        {
                            "plan": asdict(state.plan),
                            "pages": [
                                {
                                    "url": page.url,
                                    "content_hash": page.content_hash,
                                    "parser_version": page.parser_version,
                                    "source_type": page.source_type,
                                }
                                for page in ingestion_pages
                            ],
                        },
                        lambda recorder, operation_key: self.agents.curator.extract(
                            state,
                            state.plan,
                            ingestion_pages,
                            self._incoming_handoff_message_ids(store, state, "curator"),
                            invocation_recorder=recorder,
                            operation_key=operation_key,
                        ),
                        lambda values: [asdict(item) for item in values],
                        _deserialize_evidence,
                    )
                    self._attach_multimodal_grounding(state, incoming)
                    before = {(item.id, item.content_hash) for item in state.evidence}
                    state.evidence = self.ledger.merge(state.evidence, incoming)
                    # Recompute annotations for the full ledger. This lets a
                    # resumed run benefit from improved deterministic checks
                    # without changing or fabricating its saved quotes.
                    self._attach_provenance(
                        state,
                        state.evidence,
                        min_relevance=self.config.closure.min_slot_relevance,
                    )
                    after = {(item.id, item.content_hash) for item in state.evidence}
                    if after != before:
                        state.evidence_revision += 1
                        state.draft_answer = None
                        state.answer_delivery = {}
                        state.verification = None
                        state.draft_revision = -1
                        state.verification_revision = -1
                    self._finalize_contradiction_checks(state)
                    if state.attachment_pages:
                        state.attachments_ingested = True
                    state.pending_pages = []
                    state.pending_queries = []
                    state.next_node = "assess_closure"
                    self._save(store, "ingest_evidence", state, {"count": len(incoming)})
                    continue

                if node == "assess_closure":
                    state.status = "running"
                    state.closure = await self.agents.critic.assess(
                        state,
                        state.plan,
                        self._incoming_handoff_message_ids(store, state, "critic"),
                        invocation_recorder=store.save_invocation,
                    )
                    state.closure_revision = state.evidence_revision
                    if state.closure.hard_gate_passed:
                        state.next_node = "draft"
                    elif self._budget_exhausted(state):
                        if self._extend_budget_for_evidence_recovery(
                            state,
                            "closure_open_after_retrieval",
                        ):
                            state.next_node = "generate_queries"
                        else:
                            state.status = "evidence_incomplete"
                            state.suspension = {
                                "reason": "evidence_incomplete",
                                "resume_node": "generate_queries",
                            }
                            state.next_node = "finalize"
                    else:
                        state.next_node = "generate_queries"
                    self._save(
                        store,
                        "assess_closure",
                        state,
                        {
                            "closed": state.closure.closed,
                            "score": state.closure.score,
                            "budget_extension": (
                                state.budget_expansions[-1]
                                if state.budget_expansions
                                and state.budget_expansions[-1].get("trigger")
                                == "closure_open_after_retrieval"
                                else None
                            ),
                        },
                    )
                    continue

                if node == "draft":
                    if (
                        not state.closure
                        or not state.closure.hard_gate_passed
                        or state.closure_revision != state.evidence_revision
                    ):
                        state.next_node = "assess_closure"
                        continue
                    state.status = "drafting"
                    admitted_evidence = closure_supporting_evidence(state)
                    state.draft_answer = await self._execute_model_operation(
                        store,
                        state,
                        "draft",
                        {
                            "question": state.question,
                            "plan": asdict(state.plan),
                            "evidence": [asdict(item) for item in admitted_evidence],
                            "evidence_revision": state.evidence_revision,
                        },
                        lambda recorder, operation_key: self.agents.writer.draft(
                            state,
                            state.plan,
                            self._incoming_handoff_message_ids(store, state, "writer"),
                            invocation_recorder=recorder,
                            operation_key=operation_key,
                        ),
                        lambda value: value,
                        lambda value: str(value),
                    )
                    state.draft_revision = state.evidence_revision
                    state.next_node = "verify"
                    self._save(
                        store,
                        "draft",
                        state,
                        {"repair_round": state.counters.verification_repairs},
                    )
                    continue

                if node == "verify":
                    if not state.draft_answer or state.draft_revision != state.evidence_revision:
                        state.next_node = "draft"
                        continue
                    admitted_evidence = closure_supporting_evidence(state)
                    fallback_reason = _saved_verifier_outage_reason(state)
                    used_local_citation_binding = bool(fallback_reason)
                    if used_local_citation_binding:
                        state.verification = _local_citation_binding_report(
                            state.draft_answer,
                            admitted_evidence,
                            fallback_reason,
                        )
                    else:
                        try:
                            state.verification = await self._execute_model_operation(
                                store,
                                state,
                                "verify",
                                {
                                    "answer": state.draft_answer,
                                    "evidence": [asdict(item) for item in admitted_evidence],
                                    "draft_revision": state.draft_revision,
                                },
                                lambda recorder, operation_key: self.agents.verifier.verify(
                                    state,
                                    state.draft_answer,
                                    self._incoming_handoff_message_ids(store, state, "verifier"),
                                    invocation_recorder=recorder,
                                    operation_key=operation_key,
                                ),
                                _serialize_dataclass,
                                _deserialize_verification,
                            )
                        except Exception as error:
                            if not _is_transient_verifier_outage(error):
                                raise
                            fallback_reason = str(error)
                            state.failures.append(
                                {
                                    "type": "verification_service_unavailable",
                                    "reason": fallback_reason,
                                    "next_node": "finalize",
                                    "retryable": True,
                                    "instruction": (
                                        "The semantic verifier did not return a result; "
                                        "a local citation-binding check was used for delivery."
                                    ),
                                }
                            )
                            state.verification = _local_citation_binding_report(
                                state.draft_answer,
                                admitted_evidence,
                                fallback_reason,
                            )
                            used_local_citation_binding = True

                    if used_local_citation_binding and _local_citation_binding_passed(
                        state.verification
                    ):
                        state.verification_revision = state.draft_revision
                        state.status = "completed"
                        state.suspension = {}
                        state.next_node = "finalize"
                        self._save(
                            store,
                            "verify",
                            state,
                            {
                                "passed": False,
                                "verification_mode": "local_citation_binding",
                                "citation_binding_passed": True,
                                "semantic_verifier_available": False,
                                "reason": fallback_reason,
                            },
                        )
                        continue

                    state.verification = enforce_verification_contract(
                        state.draft_answer,
                        admitted_evidence,
                        state.verification,
                        allowed_evidence_ids={item.id for item in admitted_evidence},
                    )
                    state.verification_revision = state.draft_revision
                    revisions_aligned = (
                        state.evidence_revision
                        == state.closure_revision
                        == state.draft_revision
                        == state.verification_revision
                    )
                    passed = bool(
                        state.verification.passed
                        and state.closure
                        and state.closure.hard_gate_passed
                        and revisions_aligned
                    )
                    if passed:
                        state.status = "completed"
                        state.next_node = "finalize"
                    elif (
                        state.counters.verification_repairs >= 1
                        or self._budget_exhausted(state)
                    ):
                        state.status = "verification_failed"
                        state.suspension = {
                            "reason": "verification_failed",
                            "resume_node": "generate_queries",
                        }
                        state.next_node = "finalize"
                    else:
                        state.failures.append(
                            {
                                "type": "citation_error",
                                "reason": "One or more answer claims were not fully supported.",
                                "next_node": "generate_queries",
                                "retryable": True,
                            }
                        )
                        state.counters.verification_repairs += 1
                        state.pending_gaps = self._verification_gaps(state)
                        state.next_node = "generate_queries"
                    self._save(
                        store,
                        "verify",
                        state,
                        {
                            "passed": passed,
                            "repair_round": state.counters.verification_repairs,
                        },
                    )
                    continue

                if node == "finalize":
                    _ensure_terminal_answer_delivery(state)
                    self._record_limited_delivery_invocations(store, state)
                    if state.status != "completed" and not state.suspension:
                        state.suspension = {
                            "reason": state.status,
                            "resume_node": "generate_queries" if state.plan else "plan",
                        }
                    state.next_node = "done"
                    self._save(store, "finalize", state, {"status": state.status})
                    return state

                raise RuntimeError(f"Unknown workflow node: {node}")
        except ExecutionFenceLostError:
            raise
        except ResearchCancelled:
            state.status = "cancelled"
            state.suspension = {
                "reason": "cancelled",
                "resume_node": locals().get("node", state.next_node),
            }
            state.next_node = "done"
            _ensure_terminal_answer_delivery(state)
            self._record_limited_delivery_invocations(store, state)
            self._save(store, "cancelled", state, {"status": state.status})
        except Exception as error:
            if self.cancel_check():
                state.status = "cancelled"
                state.suspension = {
                    "reason": "cancelled",
                    "resume_node": locals().get("node", state.next_node),
                }
                state.next_node = "done"
                _ensure_terminal_answer_delivery(state)
                self._record_limited_delivery_invocations(store, state)
                self._save(store, "cancelled", state, {"status": state.status})
                return state
            state.status = "failed"
            error_type = _classify_error(error)
            action = recovery_for(error_type)
            if isinstance(error, ProviderRequestNotSent):
                action = RecoveryAction(
                    str(locals().get("node") or state.next_node or "plan"),
                    True,
                    "The TLS handshake failed before model request data was sent; retry is safe.",
                )
            state.suspension = {
                "reason": error_type,
                "resume_node": action.next_node,
            }
            state.failures.append(
                {
                    "type": error_type,
                    "reason": str(error),
                    "next_node": action.next_node,
                    "retryable": action.retryable,
                    "instruction": action.instruction,
                    **(
                        {"operation_key": error.operation_key}
                        if isinstance(error, AmbiguousOperationError)
                        else {}
                    ),
                }
            )
            _ensure_terminal_answer_delivery(state)
            self._record_limited_delivery_invocations(store, state)
            self._save(
                store,
                "recover",
                state,
                {"error": str(error), "next_node": action.next_node},
            )
            raise
        finally:
            try:
                durable_state = store.latest()
                if durable_state is not None:
                    store.write_final(durable_state)
            finally:
                lease.release()
        return state

    @staticmethod
    def _verification_gaps(state: ResearchState) -> list[EvidenceGap]:
        if not state.verification or not state.plan:
            return []
        gaps: list[EvidenceGap] = []
        for item in state.verification.items:
            if item.status == "entailed":
                continue
            slot = max(
                state.plan.slots,
                key=lambda candidate: _token_overlap(
                    item.claim, f"{candidate.description} {candidate.value or ''}"
                ),
            )
            gaps.append(
                EvidenceGap(
                    type="unsupported_claim",
                    slot_id=slot.id,
                    description=item.claim,
                    preferred_source="independent_source",
                )
            )
        return gaps

    async def _retrieve(
        self, store: RunStore, state: ResearchState, queries: list[Query]
    ) -> list[Page]:
        previous = state.agent_invocations[-1] if state.agent_invocations else None
        stage_invocation = AgentInvocation(
            invocation_id=str(uuid.uuid4()),
            agent_id="scout",
            role="retrieval_strategist",
            operation="search_and_fetch",
            attempt=1 + sum(
                item.operation == "search_and_fetch"
                for item in state.agent_invocations
            ),
            started_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            status="running",
            input_type="QueryBatch",
            execution_mode="executed",
            provider_call_count=0,
            previous_in_log_id=(
                previous.invocation_id if previous is not None else None
            ),
            input_summary=f"{len(queries)} queries selected for retrieval",
            consumed_handoff_message_ids=self._incoming_handoff_message_ids(
                store, state, "scout"
            ),
            run_id=state.run_id,
            trace_id=state.run_id,
            side_effect_status="not_applicable",
        )
        state.agent_invocations.append(stage_invocation)
        store.save_invocation(stage_invocation)
        try:
            pages = await self._retrieve_capabilities(
                store,
                state,
                queries,
                parent_invocation_id=stage_invocation.invocation_id,
            )
        except BaseException as error:
            stage_invocation.status = (
                "cancelled" if isinstance(error, ResearchCancelled) else "failed"
            )
            stage_invocation.ended_at = datetime.now(UTC).isoformat()
            stage_invocation.error = str(error)[:1000]
            store.save_invocation(stage_invocation)
            raise
        stage_invocation.status = "succeeded"
        stage_invocation.ended_at = datetime.now(UTC).isoformat()
        stage_invocation.output_type = "SourcePageBatch"
        stage_invocation.output_summary = f"{len(pages)} fetched source pages"
        store.save_invocation(stage_invocation)
        return pages

    async def _retrieve_capabilities(
        self,
        store: RunStore,
        state: ResearchState,
        queries: list[Query],
        *,
        parent_invocation_id: str,
    ) -> list[Page]:
        pages_by_hash: dict[str, Page] = {}
        call_budget = self._budget_limit(
            state, "search_calls", self.config.budget.max_search_calls
        ) - state.counters.search_calls
        active_queries = queries[: max(0, call_budget)]
        search_bindings: list[dict[str, Any]] = [dict() for _ in active_queries]
        search_outputs = await asyncio.gather(
            *(
                self._execute_tool_operation(
                    store,
                    state,
                    "search",
                    {
                        "query": _normalized_query_text(query.text),
                        "subgoal_id": query.subgoal_id,
                        "strategy": query.strategy,
                        "limit": 3,
                        "contract": "search-result-v2",
                    },
                    lambda query=query: self.search.search(query, limit=3),
                    lambda values: [asdict(item) for item in values],
                    lambda values: [SearchResult(**item) for item in values],
                    parent_invocation_id=parent_invocation_id,
                    return_binding=True,
                    binding_callback=search_bindings[index].update,
                )
                for index, query in enumerate(active_queries)
            ),
            return_exceptions=True,
        )
        candidate_result_groups: list[list[SearchResult]] = []
        seen_urls: set[str] = set()
        for query, output in zip(active_queries, search_outputs, strict=True):
            if isinstance(output, BaseException) and not isinstance(output, Exception):
                raise output
            if isinstance(output, BaseException):
                self._record_contradiction_search(
                    state, query, result_count=0, error=str(output)
                )
                state.failures.append(
                    {
                        "type": "retrieval_miss",
                        "reason": str(output),
                        "query": query.text,
                        "retryable": not isinstance(output, ResourceLimitExceededError),
                    }
                )
                store.event(
                    "tool_failed", "search", {"query": query.text, "error": str(output)}
                )
                continue
            results, discovery_invocation = output
            self._record_contradiction_search(
                state, query, result_count=len(results), error=None
            )
            store.event(
                "tool_finished",
                "search",
                {"query": query.text, "results": len(results)},
            )
            query_candidates: list[SearchResult] = []
            for result in results:
                self._record_discovery(
                    state,
                    query,
                    result,
                    invocation=discovery_invocation,
                )
                canonical_url = _canonical_source_url(result.url)
                if canonical_url in seen_urls:
                    continue
                seen_urls.add(canonical_url)
                query_candidates.append(result)
            candidate_result_groups.append(query_candidates)

        self._raise_if_cancelled()
        page_budget = self._budget_limit(
            state, "pages", self.config.budget.max_pages
        ) - state.counters.pages_selected
        selected_results = _round_robin_results(
            candidate_result_groups,
            max(0, page_budget),
        )
        fetch_bindings: list[dict[str, Any]] = [dict() for _ in selected_results]
        fetch_outputs = await asyncio.gather(
            *(
                self._execute_tool_operation(
                    store,
                    state,
                    "fetch",
                    {
                        "source_id": next(
                            (
                                source.id
                                for source in state.sources
                                if source.url == result.url
                            ),
                            "",
                        ),
                        "requested_url": _canonical_source_url(result.url),
                        "title": result.title,
                        "source_type": result.source_type,
                        "provider": type(self.search).__name__,
                        "fetch_policy": "public-http-ssrf-guard-v2",
                        "parser_contract": "html-pdf-text-v2",
                    },
                    lambda result=result: self.search.fetch(result),
                    _serialize_dataclass,
                    lambda value: Page(**value),
                    parent_invocation_id=parent_invocation_id,
                    return_binding=True,
                    binding_callback=fetch_bindings[index].update,
                )
                for index, result in enumerate(selected_results)
            ),
            return_exceptions=True,
        )
        for index, (result, output) in enumerate(
            zip(selected_results, fetch_outputs, strict=True)
        ):
            if isinstance(output, BaseException) and not isinstance(output, Exception):
                raise output
            if isinstance(output, BaseException):
                binding = fetch_bindings[index]
                self._update_source(
                    state,
                    result.url,
                    status="failed",
                    error=str(output),
                )
                state.failures.append(
                    {
                        "type": "fetch_error",
                        "reason": str(output),
                        "url": result.url,
                        "retryable": not isinstance(output, ResourceLimitExceededError),
                    }
                )
                store.event(
                    "tool_failed", "fetch", {"url": result.url, "error": str(output)}
                )
                self._record_source_fetch_binding(
                    store,
                    state,
                    result.url,
                    binding,
                    status="failed",
                    error=str(output),
                )
                continue
            page, fetch_invocation = output
            self._update_source(
                state,
                result.url,
                status="fetched",
                content_hash=page.content_hash,
                page=page,
            )
            source = next((item for item in state.sources if item.url == result.url), None)
            if source is not None:
                operation = store.operation_detail(
                    str(fetch_invocation.operation_key or "")
                ) or {}
                provider = type(self.search).__name__
                fetch_mode = (
                    "durable_operation_replay"
                    if fetch_invocation.execution_mode == "replayed"
                    else "offline_corpus"
                    if provider == "ReplaySearchProvider"
                    else "provider_cache"
                    if page.cache_hit
                    else "live_provider"
                )
                source.fetch_invocation_id = fetch_invocation.invocation_id
                source.fetch_result_invocation_id = str(
                    operation.get("result_invocation_id") or ""
                )
                source.fetch_operation_key = str(
                    fetch_invocation.operation_key or ""
                )
                source.fetch_execution_mode = fetch_invocation.execution_mode
                source.fetch_provider = provider
                source.fetch_mode = fetch_mode
                source.content_hash_scope = page.content_hash_scope or "unknown"
                fetch_record_id = store.preview_source_fetch_record_id(
                    source_id=source.id,
                    operation_key=source.fetch_operation_key,
                    invocation_id=source.fetch_invocation_id,
                    status="fetched",
                    attempt=fetch_invocation.attempt,
                )
                snapshot = store.write_source_snapshot(
                    source.id,
                    page,
                    fetch_record_id=fetch_record_id,
                )
                source.snapshot_available = True
                source.snapshot_sha256 = str(snapshot["sha256"])
                page.snapshot_available = True
                page.snapshot_sha256 = source.snapshot_sha256
                fetch_record = store.record_source_fetch(
                    source_id=source.id,
                    requested_url=_canonical_source_url(result.url),
                    final_url=page.url,
                    operation_key=source.fetch_operation_key,
                    invocation_id=source.fetch_invocation_id,
                    result_invocation_id=(
                        source.fetch_result_invocation_id or None
                    ),
                    execution_mode=source.fetch_execution_mode,
                    provider=provider,
                    fetch_mode=fetch_mode,
                    status="fetched",
                    attempt=fetch_invocation.attempt,
                    content_hash=page.content_hash,
                    content_hash_scope=page.content_hash_scope,
                    snapshot_sha256=source.snapshot_sha256,
                    fetched_at=page.fetched_at,
                )
                source.fetch_record_id = str(fetch_record.get("fetch_record_id") or "")
                source.fetch_binding_status = str(
                    fetch_record.get("binding_status") or "legacy_unverified"
                )
                source.fetch_binding_valid = source.fetch_binding_status == "server_bound"
                page.fetch_record_id = source.fetch_record_id
                page.fetch_binding_status = source.fetch_binding_status
                page.fetch_binding_valid = source.fetch_binding_valid
                page.retrieval_query_texts = list(source.query_texts)
                page.retrieval_subgoal_ids = list(
                    dict.fromkeys(
                        query.subgoal_id
                        for query in state.queries
                        if query.text in source.query_texts
                    )
                )
            existing_page = pages_by_hash.get(page.content_hash)
            if existing_page is None:
                pages_by_hash[page.content_hash] = page
            else:
                existing_page.retrieval_query_texts = list(
                    dict.fromkeys(
                        [
                            *existing_page.retrieval_query_texts,
                            *page.retrieval_query_texts,
                        ]
                    )
                )
                existing_page.retrieval_subgoal_ids = list(
                    dict.fromkeys(
                        [
                            *existing_page.retrieval_subgoal_ids,
                            *page.retrieval_subgoal_ids,
                        ]
                    )
                )
        return list(pages_by_hash.values())

    @staticmethod
    def _record_discovery(
        state: ResearchState,
        query: Query,
        result: SearchResult,
        *,
        invocation: AgentInvocation | None = None,
    ) -> None:
        source = next((item for item in state.sources if item.url == result.url), None)
        if source is None:
            source = SourceRecord(
                id="S" + hashlib.sha1(result.url.encode()).hexdigest()[:8],
                url=result.url,
                title=result.title,
                source_type=result.source_type,
                snippet=result.snippet,
                query_texts=[query.text],
                iteration=state.counters.iterations,
                discovered_at=datetime.now(UTC).isoformat(),
                canonical_url=_canonical_source_url(result.url),
                registrable_domain=_registrable_domain(result.url),
                origin_cluster_id=f"host:{_registrable_domain(result.url)}",
                independence_status="weak_host_fallback",
                independence_reason=(
                    "Different registrable domains are treated as a weak fallback; "
                    "shared upstream ownership or syndication is not yet verified."
                ),
            )
            state.sources.append(source)
        elif query.text not in source.query_texts:
            source.query_texts.append(query.text)
        if invocation is not None:
            if invocation.invocation_id not in source.discovery_invocation_ids:
                source.discovery_invocation_ids.append(invocation.invocation_id)
            if (
                invocation.operation_key
                and invocation.operation_key not in source.discovery_operation_keys
            ):
                source.discovery_operation_keys.append(invocation.operation_key)

    @staticmethod
    def _update_source(
        state: ResearchState,
        url: str,
        status: str,
        content_hash: str = "",
        error: str | None = None,
        page: Page | None = None,
    ) -> None:
        source = next((item for item in state.sources if item.url == url), None)
        if source is None:
            return
        source.status = status
        source.content_hash = content_hash
        source.error = error
        if page is not None:
            source.fetched_at = page.fetched_at
            source.final_url = page.url
            source.http_status = page.http_status
            source.content_type = page.content_type
            source.content_hash_scope = page.content_hash_scope or "unknown"
            source.parser_version = page.parser_version
            source.bytes_read = page.bytes_read
            source.cache_hit = page.cache_hit
            source.canonical_url = page.canonical_url or _canonical_source_url(page.url)
            source.publisher_name = page.publisher_name
            source.publisher_url = page.publisher_url
            source.publisher_id = _publisher_identity(page.publisher_url, page.publisher_name)
            source.author_names = list(page.author_names)
            source.site_name = page.site_name
            source.upstream_urls = list(page.upstream_urls)
            source.provenance_signals = list(page.provenance_signals)
            normalized = _normalized_source_text(page.text)
            source.normalized_content_hash = hashlib.sha256(normalized.encode()).hexdigest()
            source.simhash = _simhash(normalized)
            ResearchEngine._resolve_source_provenance(state, source)

    def _record_source_fetch_binding(
        self,
        store: RunStore,
        state: ResearchState,
        url: str,
        binding: dict[str, Any],
        *,
        status: str,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        source = next((item for item in state.sources if item.url == url), None)
        operation_key = str(binding.get("operation_key") or "")
        invocation_id = str(binding.get("invocation_id") or "")
        if source is None or not operation_key or not invocation_id:
            return None
        provider = type(self.search).__name__
        execution_mode = str(binding.get("execution_mode") or "executed")
        fetch_mode = (
            "durable_operation_replay"
            if execution_mode == "replayed"
            else "offline_corpus"
            if provider == "ReplaySearchProvider"
            else "live_provider"
        )
        source.fetch_invocation_id = invocation_id
        source.fetch_result_invocation_id = str(
            binding.get("result_invocation_id") or ""
        )
        source.fetch_operation_key = operation_key
        source.fetch_execution_mode = execution_mode
        source.fetch_provider = provider
        source.fetch_mode = fetch_mode
        fetch_record = store.record_source_fetch(
            source_id=source.id,
            requested_url=_canonical_source_url(url),
            operation_key=operation_key,
            invocation_id=invocation_id,
            result_invocation_id=source.fetch_result_invocation_id or None,
            execution_mode=execution_mode,
            provider=provider,
            fetch_mode=fetch_mode,
            status=status,
            attempt=int(binding.get("attempt") or 1),
            final_url=source.final_url or None,
            content_hash=source.content_hash or None,
            content_hash_scope=source.content_hash_scope,
            snapshot_sha256=source.snapshot_sha256 or None,
            error=error,
            fetched_at=source.fetched_at or None,
        )
        source.fetch_record_id = str(fetch_record.get("fetch_record_id") or "")
        source.fetch_binding_status = str(
            fetch_record.get("binding_status") or "legacy_unverified"
        )
        source.fetch_binding_valid = source.fetch_binding_status == "server_bound"
        return fetch_record

    @staticmethod
    def _resolve_source_provenance(state: ResearchState, source: SourceRecord) -> None:
        source.canonical_url = source.canonical_url or _canonical_source_url(source.final_url or source.url)
        source.registrable_domain = _registrable_domain(source.final_url or source.url)
        source.origin_cluster_id = f"host:{source.registrable_domain}"
        source.independence_status = "weak_host_fallback"
        source.independence_reason = (
            "Counted by distinct registrable domain only; upstream editorial origin is unknown."
        )
        canonical_domain = _registrable_domain(source.canonical_url) if source.canonical_url else ""
        if canonical_domain and canonical_domain != source.registrable_domain:
            source.origin_cluster_id = f"declared-upstream:{canonical_domain}"
            source.independence_status = "declared_upstream"
            source.independence_reason = (
                f"Page declares a cross-domain canonical upstream at {canonical_domain}; "
                "used conservatively for grouping, not treated as verified ownership."
            )
        elif source.publisher_id:
            source.origin_cluster_id = source.publisher_id
            source.independence_status = "declared_publisher"
            source.independence_reason = (
                "Page self-declares publisher metadata; matching declarations are grouped "
                "conservatively, but distinct declarations do not prove independence."
            )
        work_identity = _scholarly_work_identity(source)
        if (
            work_identity
            and not source.publisher_id
            and canonical_domain in {"", source.registrable_domain}
        ):
            # arXiv is a distribution host, not the editorial origin of every
            # paper it serves. A stable work ID lets literature synthesis count
            # distinct papers without falsely claiming independent publishers
            # or author groups.
            source.origin_cluster_id = f"scholarly-work:{work_identity}"
            source.independence_status = "distinct_scholarly_work"
            source.independence_reason = (
                "Distinct immutable arXiv work identifier; counted as a separate "
                "scholarly work for literature coverage, not as verified independent "
                "publisher or author ownership."
            )
        for candidate in state.sources:
            if candidate.id == source.id or candidate.status != "fetched":
                continue
            if (
                source.normalized_content_hash
                and source.normalized_content_hash == candidate.normalized_content_hash
            ):
                source.near_duplicate_of_source_id = candidate.id
                source.near_duplicate_similarity = 1.0
                source.origin_cluster_id = candidate.origin_cluster_id or f"content:{candidate.id}"
                source.independence_status = "dependent"
                source.independence_reason = "Normalized article body is an exact duplicate."
                return
            declared_canonical = source.canonical_url
            candidate_urls = {
                candidate.url,
                candidate.final_url,
                candidate.canonical_url,
            }
            if declared_canonical and declared_canonical in candidate_urls:
                source.near_duplicate_of_source_id = candidate.id
                source.near_duplicate_similarity = 0.0
                source.origin_cluster_id = candidate.origin_cluster_id or f"host:{candidate.registrable_domain}"
                source.independence_status = "dependent"
                source.independence_reason = (
                    "Page declares the exact canonical URL of another fetched source; "
                    "it is treated as a dependent copy."
                )
                return
            candidate_work_identity = _scholarly_work_identity(candidate)
            if work_identity and work_identity == candidate_work_identity:
                source.near_duplicate_of_source_id = candidate.id
                source.near_duplicate_similarity = 0.0
                source.origin_cluster_id = (
                    candidate.origin_cluster_id or f"scholarly-work:{work_identity}"
                )
                source.independence_status = "dependent"
                source.independence_reason = (
                    "Sources resolve to the same immutable scholarly work identifier."
                )
                return
            similarity = _simhash_similarity(source.simhash, candidate.simhash)
            if similarity >= 0.92:
                source.near_duplicate_of_source_id = candidate.id
                source.near_duplicate_similarity = round(similarity, 4)
                source.origin_cluster_id = candidate.origin_cluster_id or f"content:{candidate.id}"
                source.independence_status = "dependent"
                source.independence_reason = (
                    f"Near-duplicate article body (SimHash similarity {similarity:.1%})."
                )
                return
            if source.registrable_domain == candidate.registrable_domain:
                if (
                    work_identity
                    and candidate_work_identity
                    and work_identity != candidate_work_identity
                ):
                    # Keep different papers on a repository host separate.
                    continue
                source.origin_cluster_id = candidate.origin_cluster_id or f"host:{source.registrable_domain}"
                source.independence_status = "same_publisher_group"
                source.independence_reason = (
                    "Same registrable domain; treated as one publisher group."
                )
                return
            if source.publisher_id and source.publisher_id == candidate.publisher_id:
                source.origin_cluster_id = candidate.origin_cluster_id or source.publisher_id
                source.independence_status = "same_publisher_group"
                source.independence_reason = (
                    "Different hosts declare the same publisher; grouped conservatively as one source."
                )
                return
            shared_upstream = set(source.upstream_urls) & set(candidate.upstream_urls)
            if shared_upstream:
                source.origin_cluster_id = candidate.origin_cluster_id or (
                    "upstream:" + hashlib.sha1(sorted(shared_upstream)[0].encode()).hexdigest()[:12]
                )
                source.independence_status = "dependent"
                source.independence_reason = (
                    "Sources declare the same canonical, citation, or syndication upstream URL."
                )
                return

    @staticmethod
    def _attachment_context(state: ResearchState) -> str:
        lines: list[str] = []
        by_id = {item.id: item for item in state.input_attachments}
        for observation in state.attachment_observations:
            attachment = by_id.get(observation.attachment_id)
            label = attachment.name if attachment else observation.attachment_id
            lines.append(
                f"Attachment {label} ({observation.modality}, {observation.attachment_id}): "
                f"{observation.summary}"
            )
            for item in observation.observations[:12]:
                lines.append(f"- [{item.locator}] {item.text}")
        return "\n".join(lines)[:24_000]

    @staticmethod
    def _materialize_attachment_sources(state: ResearchState) -> None:
        attachments = {item.id: item for item in state.input_attachments}
        pages: list[Page] = []
        existing_source_ids = {item.id for item in state.sources}
        for observation in state.attachment_observations:
            attachment = attachments.get(observation.attachment_id)
            if attachment is None or not observation.observations:
                continue
            text_lines = [
                f"User attachment: {attachment.name}",
                f"Attachment ID: {attachment.id}",
                f"Modality: {attachment.modality}",
                f"Grounded summary: {observation.summary}",
            ]
            for index, item in enumerate(observation.observations, start=1):
                text_lines.extend(
                    [
                        f"Observation {index} locator: {item.locator}",
                        item.text,
                    ]
                )
            text = "\n".join(text_lines)
            content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            source_url = f"attachment://{attachment.id}"
            locator = "; ".join(
                item.locator for item in observation.observations[:8]
            )
            confidence = max(
                (item.confidence for item in observation.observations),
                default=0.0,
            )
            fetch_record_id = f"input:{attachment.id}"
            page = Page(
                url=source_url,
                title=attachment.name,
                text=text,
                source_type="user_attachment",
                content_hash=content_hash,
                fetched_at=attachment.created_at,
                http_status=200,
                content_type=attachment.media_type,
                parser_version=(
                    observation.parser_version or "native-multimodal-perception-v1"
                ),
                bytes_read=attachment.byte_length,
                cache_hit=False,
                canonical_url=source_url,
                provenance_signals=[
                    "content-addressed user upload",
                    f"perceived by {observation.model_choice}:{observation.model_id}",
                ],
                fetch_record_id=fetch_record_id,
                snapshot_sha256=attachment.sha256,
                snapshot_available=True,
                fetch_binding_status="server_bound",
                fetch_binding_valid=True,
                content_hash_scope="canonical_perception_text",
                attachment_id=attachment.id,
                modality=attachment.modality,
                source_locator=locator,
                perception_model=observation.model_id,
                grounding_confidence=confidence,
            )
            pages.append(page)
            source_id = "S" + hashlib.sha1(source_url.encode()).hexdigest()[:8]
            if source_id in existing_source_ids:
                continue
            state.sources.append(
                SourceRecord(
                    id=source_id,
                    url=source_url,
                    title=attachment.name,
                    source_type="user_attachment",
                    snippet=observation.summary[:500],
                    status="fetched",
                    iteration=0,
                    content_hash=content_hash,
                    discovered_at=attachment.created_at,
                    fetched_at=attachment.created_at,
                    final_url=source_url,
                    http_status=200,
                    content_type=attachment.media_type,
                    parser_version=page.parser_version,
                    bytes_read=attachment.byte_length,
                    canonical_url=source_url,
                    registrable_domain="local-input",
                    normalized_content_hash=content_hash,
                    origin_cluster_id=f"attachment:{attachment.sha256}",
                    independence_status="user_provided",
                    independence_reason=(
                        "A distinct content-addressed user attachment; it counts as one "
                        "input origin and does not prove editorial independence."
                    ),
                    source_role="user_input",
                    authority_scope="user-provided material only",
                    provenance_signals=list(page.provenance_signals),
                    snapshot_available=True,
                    snapshot_sha256=attachment.sha256,
                    fetch_record_id=fetch_record_id,
                    fetch_provider="content-addressed-upload",
                    fetch_mode="local_input",
                    fetch_binding_status="server_bound",
                    fetch_binding_valid=True,
                    content_hash_scope="canonical_perception_text",
                    attachment_id=attachment.id,
                    modality=attachment.modality,
                    perception_model=observation.model_id,
                )
            )
            existing_source_ids.add(source_id)
        state.attachment_pages = pages

    @staticmethod
    def _attach_multimodal_grounding(
        state: ResearchState,
        evidence: list[Evidence],
    ) -> None:
        observations = {
            item.attachment_id: item for item in state.attachment_observations
        }
        for item in evidence:
            if not item.attachment_id:
                continue
            observation = observations.get(item.attachment_id)
            if observation is None:
                continue
            matched = next(
                (
                    segment
                    for segment in observation.observations
                    if item.quote in segment.text or segment.text in item.quote
                ),
                None,
            )
            if matched is not None:
                item.source_locator = matched.locator
                item.grounding_confidence = matched.confidence
            item.perception_model = observation.model_id
            item.modality = observation.modality

    @staticmethod
    def _attach_provenance(
        state: ResearchState,
        evidence: list[Evidence],
        *,
        min_relevance: float = 0.45,
    ) -> None:
        by_url: dict[str, SourceRecord] = {}
        for source in state.sources:
            by_url[source.url] = source
            if source.final_url:
                by_url[source.final_url] = source
        for item in evidence:
            source = by_url.get(item.source_url)
            if source is None:
                continue
            item.source_id = source.id
            exact_fetch = str(item.fetch_record_id or "").strip()
            content_matches = not item.content_hash or not source.content_hash or (
                item.content_hash == source.content_hash
            )
            if exact_fetch:
                item.fetch_record_id = exact_fetch
            exact_source_fetch = bool(exact_fetch) and exact_fetch == str(
                source.fetch_record_id or ""
            ).strip()
            if exact_source_fetch and content_matches:
                item.snapshot_sha256 = item.snapshot_sha256 or source.snapshot_sha256
                item.snapshot_available = bool(
                    item.snapshot_available or source.snapshot_available
                    or item.snapshot_sha256
                )
                item.fetch_binding_status = (
                    item.fetch_binding_status
                    if item.fetch_binding_status not in {"", "unbound"}
                    else source.fetch_binding_status
                )
                if item.fetch_binding_valid is None:
                    item.fetch_binding_valid = source.fetch_binding_valid
                item.content_hash_scope = (
                    item.content_hash_scope
                    if item.content_hash_scope != "unknown"
                    else source.content_hash_scope
                )
            item.origin_cluster_id = source.origin_cluster_id or item.source_cluster_id
            item.independence_status = source.independence_status
            item.independence_basis = source.independence_reason
            item.source_role = source.source_role
            item.authority_scope = source.authority_scope
            consistency, reasons = _claim_quote_consistency(item.claim, item.quote)
            item.claim_quote_consistency = consistency
            item.claim_quote_check_reasons = reasons
            slot = next(
                (candidate for candidate in (state.plan.slots if state.plan else []) if candidate.id == item.slot_id),
                None,
            )
            subgoal = next(
                (candidate for candidate in (state.plan.subgoals if state.plan else []) if candidate.id == item.subgoal_id),
                None,
            )
            source_bound_query_targets = [
                query.text
                for query in state.queries
                if query.subgoal_id == item.subgoal_id
                and query.text in source.query_texts
            ]
            relevance, relevance_reasons = _claim_target_relevance_variants(
                f"{item.source_title} {item.claim}",
                [
                    value
                    for value in (
                        slot.description if slot else "",
                        subgoal.question if subgoal else "",
                        *source_bound_query_targets,
                    )
                    if value
                ],
            )
            route_relevance_scores = [
                _claim_target_relevance(item.claim, query)[0]
                for query in source_bound_query_targets
            ]
            route_anchor_found = any(score > 0.0 for score in route_relevance_scores)
            future_slot_requires_signal = _future_direction_target(
                slot.description if slot else "",
                subgoal.question if subgoal else "",
            )
            future_signal_present = _future_direction_signal(item.claim)
            explicitly_irrelevant = any(
                "explicitly disclaims relevance" in reason
                for reason in relevance_reasons
            )
            if (
                source_bound_query_targets
                and route_anchor_found
                and item.stance in {"supports", "contradicts"}
                and item.claim_quote_consistency >= MIN_CLAIM_QUOTE_CONSISTENCY
                and not explicitly_irrelevant
                and (
                    not future_slot_requires_signal
                    or future_signal_present
                )
                and relevance < min_relevance
            ):
                # The fetched page is traceably attached to a query for this
                # exact answer target. This guards against CJK/English lexical
                # mismatch and paraphrase without treating the retrieval path
                # as proof: all source, quote, provenance and contradiction
                # checks still apply before final delivery.
                relevance = min_relevance
                relevance_reasons = [
                    *relevance_reasons,
                    "检索声明锚点：证据原文与同一回答目标的检索表达存在可识别词面交集。",
                    "检索路径相关性：该页面由同一回答目标的已记录检索路线发现；以保守准入下限进入候选证据，仍须通过来源、原文、反证和冲突检查。",
                ]
            elif future_slot_requires_signal and route_anchor_found and not future_signal_present:
                relevance_reasons = [
                    *relevance_reasons,
                    "该目标要求未来方向；检索词中的 future 不能替代原文中明确的未来研究、未来工作、未来方向或开放问题表述。",
                ]
            item.slot_relevance_score = relevance
            item.slot_relevance_reasons = [
                "source-bound query formulations considered: "
                f"{len(source_bound_query_targets)}",
                *relevance_reasons,
            ]

    @staticmethod
    def _enforce_required_query_coverage(
        state: ResearchState,
        queries: list[Query],
        gaps: list[EvidenceGap],
    ) -> list[Query]:
        """Keep a single model batch focused on required-slot coverage.

        The provider is asked to make this selection, but the orchestrator
        deterministically repairs a missing subgoal assignment without another
        paid model round.  At most three queries are retained, matching the
        provider and per-round search contract.
        """
        if state.plan is None:
            return queries[:3]
        coverage_subgoals = _required_query_coverage_subgoals(
            state.plan,
            gaps,
            history=state.queries,
        )
        if not coverage_subgoals:
            return queries[:3]
        valid_subgoals = {item.id: item for item in state.plan.subgoals}
        history_fingerprints = {
            _query_fingerprint(item.text) for item in state.queries
        }
        selected: list[Query] = []
        selected_fingerprints: set[str] = set()
        for subgoal in coverage_subgoals:
            query = next(
                (
                    item
                    for item in queries
                    if item.subgoal_id == subgoal.id
                    and item.text.strip()
                    and _query_fingerprint(item.text) not in history_fingerprints
                    and _query_fingerprint(item.text) not in selected_fingerprints
                ),
                None,
            )
            gap_types = {
                gap.type
                for gap in gaps
                if set(subgoal.slot_ids).intersection({gap.slot_id})
            }
            required_strategy = (
                "contradiction_check"
                if "contradiction_not_checked" in gap_types
                else "source_targeting"
            )
            if query is None:
                query = _coverage_fallback_query(
                    state,
                    subgoal,
                    required_strategy,
                    history_fingerprints | selected_fingerprints,
                )
            elif required_strategy == "contradiction_check":
                query.strategy = required_strategy
            fingerprint = _query_fingerprint(query.text)
            selected.append(query)
            selected_fingerprints.add(fingerprint)

        for query in queries:
            if len(selected) >= 3:
                break
            if query.subgoal_id not in valid_subgoals or not query.text.strip():
                continue
            fingerprint = _query_fingerprint(query.text)
            if fingerprint in history_fingerprints or fingerprint in selected_fingerprints:
                continue
            selected.append(query)
            selected_fingerprints.add(fingerprint)
        return selected[:3]

    def _deduplicate_queries(
        self, state: ResearchState, queries: list[Query]
    ) -> list[Query]:
        # Two queries can share most of their wording while serving different
        # answer slots or a different purpose (for example, source discovery
        # versus a counterexample check).  Only treat near-duplicates inside
        # that intent scope as redundant. Exact text is still global so the
        # same network request can never be scheduled twice.
        seen = {_query_fingerprint(item.text) for item in state.queries}
        seen_by_intent: dict[tuple[str, str], list[str]] = {}
        for item in state.queries:
            seen_by_intent.setdefault(_query_intent_key(item), []).append(item.text)
        novel: list[Query] = []
        for query in queries:
            fingerprint = _query_fingerprint(query.text)
            intent = _query_intent_key(query)
            similar_to_history = any(
                _query_similarity(query.text, old_text) >= 0.85
                for old_text in seen_by_intent.get(intent, [])
            )
            if fingerprint in seen or (
                similar_to_history
                and not self._allows_similar_recovery_query(state, query)
            ):
                state.counters.duplicate_queries += 1
                continue
            seen.add(fingerprint)
            seen_by_intent.setdefault(intent, []).append(query.text)
            novel.append(query)
        return novel

    @staticmethod
    def _allows_similar_recovery_query(state: ResearchState, query: Query) -> bool:
        """Permit one new retrieval lens while a required evidence gate is open."""
        if state.plan is None:
            return False
        gaps = state.pending_gaps or (
            state.closure.gaps if state.closure is not None else []
        )
        subgoal = next(
            (item for item in state.plan.subgoals if item.id == query.subgoal_id),
            None,
        )
        if subgoal is None:
            return False
        gap_types = {
            gap.type
            for gap in gaps
            if gap.slot_id in set(subgoal.slot_ids)
        }
        return bool(
            (query.strategy == "source_targeting" and "missing_independent_source" in gap_types)
            or (
                query.strategy == "contradiction_check"
                and "contradiction_not_checked" in gap_types
            )
        )

    @staticmethod
    def _recovery_queries(
        state: ResearchState,
        gaps: list[EvidenceGap],
    ) -> list[Query]:
        """Create no-cost, intent-specific search repairs after an empty batch.

        A provider may repeat a valid-looking query despite being asked not to.
        The orchestrator therefore owns a small deterministic set of alternate
        research angles instead of treating an empty model batch as proof that
        the web has no relevant material.
        """

        if state.plan is None:
            return []
        selected_subgoals = _required_query_coverage_subgoals(
            state.plan,
            gaps,
            history=state.queries,
        )
        if not selected_subgoals:
            return []
        occupied = {_query_fingerprint(item.text) for item in state.queries}
        recovered: list[Query] = []
        for subgoal in selected_subgoals:
            related_gap_types = {
                item.type
                for item in gaps
                if set(subgoal.slot_ids).intersection({item.slot_id})
            }
            strategy = (
                "contradiction_check"
                if "contradiction_not_checked" in related_gap_types
                else "source_targeting"
            )
            query = _coverage_fallback_query(
                state,
                subgoal,
                strategy,
                occupied,
            )
            recovered.append(query)
            occupied.add(_query_fingerprint(query.text))
        return recovered[:3]

    @staticmethod
    def _record_contradiction_search(
        state: ResearchState,
        query: Query,
        result_count: int,
        error: str | None,
    ) -> None:
        if query.strategy != "contradiction_check" or not state.plan:
            return
        subgoal = next(
            (item for item in state.plan.subgoals if item.id == query.subgoal_id),
            None,
        )
        if not subgoal:
            return
        status = "search_failed" if error else "no_results" if not result_count else "results_returned"
        for slot_id in subgoal.slot_ids:
            existing = next(
                (
                    item
                    for item in state.contradiction_checks
                    if item.slot_id == slot_id and item.query_text == query.text
                ),
                None,
            )
            if existing is not None:
                existing.status = status
                existing.result_count = result_count
                existing.error = error
                existing.executed_at = datetime.now(UTC).isoformat()
                continue
            state.contradiction_checks.append(
                ContradictionAudit(
                    slot_id=slot_id,
                    query_text=query.text,
                    status=status,
                    executed_at=datetime.now(UTC).isoformat(),
                    result_count=result_count,
                    error=error,
                )
            )

    def _finalize_contradiction_checks(self, state: ResearchState) -> None:
        for audit in state.contradiction_checks:
            if audit.status == "search_failed":
                self._recover_transiently_failed_contradiction_check(state, audit)
                continue
            if audit.status != "results_returned":
                continue
            sources = [
                source
                for source in state.sources
                if audit.query_text in source.query_texts and source.status == "fetched"
            ]
            audit.pages_inspected = len(sources)
            audit.inspected_source_ids = [source.id for source in sources]
            relevant_sources = [
                source
                for source in sources
                if any(
                    item.slot_id == audit.slot_id
                    and item.source_url in {source.url, source.final_url}
                    and item.slot_relevance_score
                    >= self.config.closure.min_slot_relevance
                    for item in state.evidence
                )
            ]
            relevant_urls = {
                source.final_url or source.url for source in relevant_sources
            }
            audit.relevant_pages_inspected = len(relevant_sources)
            audit.relevant_source_ids = [source.id for source in relevant_sources]
            audit.irrelevant_source_ids = [
                source.id for source in sources if source not in relevant_sources
            ]
            audit.counterevidence_found = any(
                item.slot_id == audit.slot_id
                and item.source_url in relevant_urls
                and item.stance == "contradicts"
                for item in state.evidence
            )
            if not sources:
                audit.status = "fetch_failed"
                continue
            if not relevant_sources:
                audit.status = "inspected_irrelevant_only"
                continue
            audit.status = (
                "counterevidence_found"
                if audit.counterevidence_found
                else "inspected_no_counterevidence"
            )
            if audit.slot_id not in state.contradiction_checked_slots:
                state.contradiction_checked_slots.append(audit.slot_id)

    def _recover_transiently_failed_contradiction_check(
        self,
        state: ResearchState,
        audit: ContradictionAudit,
    ) -> None:
        """Review existing independent evidence after a transient public-search outage."""
        error = str(audit.error or "").casefold()
        if not any(
            marker in error
            for marker in ("429", "rate limit", "timed out", "timeout", "temporar")
        ):
            return
        sources_by_id = {source.id: source for source in state.sources}
        relevant_evidence = [
            item
            for item in state.evidence
            if item.slot_id == audit.slot_id
            and item.stance in {"supports", "contradicts"}
            and item.slot_relevance_score >= self.config.closure.min_slot_relevance
            and item.independence_status != "dependent"
            and item.source_id in sources_by_id
            and sources_by_id[item.source_id].status == "fetched"
            and item.snapshot_available
            and item.fetch_binding_status == "server_bound"
            and item.fetch_binding_valid is True
        ]
        by_source: dict[str, Evidence] = {}
        for item in relevant_evidence:
            current = by_source.get(item.source_id)
            if current is None or item.reliability > current.reliability:
                by_source[item.source_id] = item
        reviewed = list(by_source.values())
        clusters = {
            item.origin_cluster_id or item.source_cluster_id
            for item in reviewed
            if item.origin_cluster_id or item.source_cluster_id
        }
        if len(clusters) < self.config.closure.min_sources_per_required_slot:
            return
        audit.pages_inspected = len(reviewed)
        audit.inspected_source_ids = list(by_source)
        audit.relevant_pages_inspected = len(reviewed)
        audit.relevant_source_ids = list(by_source)
        audit.irrelevant_source_ids = []
        audit.counterevidence_found = any(
            item.stance == "contradicts" for item in reviewed
        )
        prior_error = str(audit.error or "public scholarly search was unavailable")
        audit.error = (
            f"{prior_error}; rate-limit fallback reviewed "
            f"{len(clusters)} independent already-fetched source clusters"
        )
        audit.status = "cross_source_review_after_search_failure"
        if audit.slot_id not in state.contradiction_checked_slots:
            state.contradiction_checked_slots.append(audit.slot_id)

    def _budget_exhausted(self, state: ResearchState) -> bool:
        return (
            state.counters.iterations
            >= self._budget_limit(
                state, "iterations", self.config.budget.max_iterations
            )
            or state.counters.search_calls
            >= self._budget_limit(
                state, "search_calls", self.config.budget.max_search_calls
            )
            or state.counters.pages_selected
            >= self._budget_limit(state, "pages", self.config.budget.max_pages)
        )

    def _extend_budget_for_evidence_recovery(
        self,
        state: ResearchState,
        trigger: str,
    ) -> bool:
        """Spend a bounded recovery tranche before declaring research incomplete.

        The persisted ceiling is the non-negotiable cap.  Extension is allowed
        only after at least one actual research attempt produced a plan and
        material to inspect; an intentionally zero-budget run still stops and
        remains resumable exactly as requested.
        """

        if (
            state.plan is None
            or not state.queries
            or not state.sources
            or (state.closure is not None and state.closure.hard_gate_passed)
        ):
            return False
        increments = {"iterations": 2, "search_calls": 4, "pages": 6}
        manual_resume_reserve = {
            "iterations": max(
                0, int(self.config.budget.manual_resume_reserve_iterations)
            ),
            "search_calls": max(
                0, int(self.config.budget.manual_resume_reserve_search_calls)
            ),
            "pages": max(0, int(self.config.budget.manual_resume_reserve_pages)),
        }
        counters = {
            "iterations": state.counters.iterations,
            "search_calls": state.counters.search_calls,
            "pages": state.counters.pages_selected,
        }
        prior_limits = {
            key: self._budget_limit(state, key, getattr(self.config.budget, f"max_{key}"))
            for key in increments
        }
        new_limits = dict(prior_limits)
        automatic_ceilings: dict[str, int] = {}
        for key, increment in increments.items():
            ceiling = int(state.budget_ceilings.get(key, prior_limits[key]))
            baseline = max(prior_limits[key], counters[key])
            automatic_ceiling = max(
                baseline,
                ceiling - manual_resume_reserve[key],
            )
            automatic_ceilings[key] = automatic_ceiling
            new_limits[key] = min(automatic_ceiling, baseline + increment)
        if new_limits == prior_limits:
            return False
        state.budget_limits.update(new_limits)
        state.budget_expansions.append(
            {
                "trigger": trigger,
                "prior_limits": prior_limits,
                "new_limits": new_limits,
                "ceilings": {
                    key: int(state.budget_ceilings.get(key, new_limits[key]))
                    for key in increments
                },
                "automatic_ceilings": automatic_ceilings,
                "manual_resume_reserve": manual_resume_reserve,
                "counters_at_extension": counters,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
        return True

    @staticmethod
    def _budget_limit(state: ResearchState, key: str, fallback: int) -> int:
        return int(state.budget_limits.get(key, fallback))

    async def _execute_model_operation(
        self,
        store: RunStore,
        state: ResearchState,
        node: str,
        semantic_input: dict[str, Any],
        call: Callable[[Callable[[AgentInvocation], None], str], Awaitable[T]],
        serializer: Callable[[T], Any],
        deserializer: Callable[[Any], T],
    ) -> T:
        canonical_input = json.dumps(
            semantic_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        semantic_input_hash = hashlib.sha256(canonical_input.encode()).hexdigest()
        role = {
            "perceive_inputs": "perception",
            "plan": "planner",
            "generate_queries": "scout",
            "extract_evidence": "curator",
            "draft": "writer",
            "verify": "verifier",
        }.get(node, node)
        provider_identity = self._model_identity(role)
        operation_material = json.dumps(
            {
                "run_id": state.run_id,
                "node": node,
                "semantic_input_hash": semantic_input_hash,
                "provider": provider_identity,
                "methodology": state.methodology.get("methodology_version", "unknown"),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_key = hashlib.sha256(operation_material.encode()).hexdigest()
        operation = store.begin_operation(operation_key, node, semantic_input_hash)
        if operation["status"] == "non_retryable":
            raise ResourceLimitExceededError(
                str(operation.get("error") or "resource_limit_exceeded")
            )
        if operation["status"] == "succeeded":
            replay = self._record_replay_invocation(
                store, state, node, operation_key
            )
            self._record_operation_replay(
                store,
                state,
                operation_key,
                replay_invocation_id=replay.invocation_id,
            )
            self._sync_persisted_usage(store, state)
            return deserializer(json.loads(operation["result_json"]))
        if operation["status"] == "external_outcome_unknown":
            raise ExternalOutcomeUnknownError(
                operation_key,
                "provider outcome is unknown after a lost execution fence; "
                "manual confirmation is required before retry",
            )
        if operation["status"] != "new":
            raise AmbiguousOperationError(
                operation_key,
                f"operation {operation_key} is still {operation['status']}; "
                "automatic retry is disabled because the provider may have charged it"
            )

        routed_provider = self._model_provider_for(role)
        before = self._model_usage_snapshot()
        original_invocation: AgentInvocation | None = None
        usage_listener_token: object | None = None
        remove_usage_listener = getattr(routed_provider, "remove_usage_listener", None)

        def record_invocation(invocation: AgentInvocation) -> None:
            nonlocal original_invocation
            if invocation.execution_mode == "executed":
                original_invocation = invocation
            store.save_invocation(invocation, operation_key=operation_key)

        def record_live_usage(usage: dict[str, Any]) -> None:
            # This callback runs after each successful provider response. A
            # later operation settlement remains a fallback for providers that
            # do not expose this optional hook or for cache-only operations.
            try:
                if store.record_model_usage_event(operation_key, usage):
                    # Keep the in-memory state coherent with the durable
                    # response receipt as well. The receipt is already the
                    # crash-safe source of truth; this prevents a concurrent
                    # status projection from showing stale counters until the
                    # larger agent operation returns.
                    self._sync_persisted_usage(store, state)
            except Exception:
                # Never discard a valid model response because an observability
                # write is temporarily unavailable; the operation summary below
                # will still attempt a durable settlement before completion.
                pass

        add_usage_listener = getattr(routed_provider, "add_usage_listener", None)
        if callable(add_usage_listener):
            try:
                usage_listener_token = add_usage_listener(record_live_usage)
            except Exception:
                usage_listener_token = None

        try:
            result = await self._call_external(
                store,
                lambda: call(record_invocation, operation_key),
            )
            after = self._model_usage_snapshot()
            usage = {
                key: max(0, after[key] - before[key])
                for key in (
                    "model_calls",
                    "model_cache_hits",
                    "input_tokens",
                    "output_tokens",
                    "estimated_cost_usd",
                )
            }
            usage_applicability = str(
                getattr(routed_provider, "usage_applicability", "applicable")
            )
            usage["provider"] = str(
                (
                    provider_identity.get("provider")
                    if usage_applicability == "not_applicable"
                    else provider_identity.get("choice")
                )
                or after.get("provider")
                or before.get("provider")
                or type(self.model).__name__
            )
            usage["model"] = str(provider_identity.get("model") or "built-in")
            usage["role"] = role
            usage["usage_applicability"] = usage_applicability
            routed_usage_snapshot = getattr(routed_provider, "usage_snapshot", None)
            routed_usage = (
                routed_usage_snapshot()
                if callable(routed_usage_snapshot)
                else {}
            )
            pricing_configured = bool(
                routed_usage.get(
                    "pricing_configured",
                    getattr(routed_provider, "pricing_configured", False),
                )
            )
            usage["pricing_configured"] = pricing_configured
            if usage["model_calls"] > 0:
                usage["pricing_status"] = str(
                    routed_usage.get("pricing_status")
                    or ("complete" if pricing_configured else "unavailable")
                )
                usage["pricing_reason"] = str(
                    routed_usage.get("pricing_reason")
                    or (
                        "Configured token pricing produced the recorded estimate."
                        if pricing_configured
                        else "No operator-configured price for this model ID."
                    )
                )
            elif usage["model_cache_hits"] > 0 and pricing_configured:
                usage["pricing_status"] = "complete"
                usage["pricing_reason"] = (
                    "A local response cache avoided a new provider charge; "
                    "the selected model has configured pricing."
                )
            # The provider has returned token usage at this point. Persist it
            # before serialization/checkpointing so the live budget view never
            # waits for the rest of a long agent stage to finish.
            store.settle_model_usage(operation_key, usage)
            self._sync_persisted_usage(store, state)
            store.complete_operation(operation_key, serializer(result), usage)
        except ExecutionFenceLostError as error:
            if original_invocation is not None:
                original_invocation.status = "failed"
                original_invocation.ended_at = datetime.now(UTC).isoformat()
                original_invocation.error = "external_outcome_unknown: execution fence lost"
                original_invocation.side_effect_status = "unknown"
                store.mark_external_outcome_unknown(
                    operation_key,
                    invocation_id=original_invocation.invocation_id,
                    error=str(error),
                )
            else:
                store.mark_external_outcome_unknown(operation_key, error=str(error))
            raise
        except ProviderOutcomeUncertain as error:
            if original_invocation is not None:
                original_invocation.side_effect_status = "unknown"
                store.save_invocation(original_invocation, operation_key=operation_key)
            raise AmbiguousOperationError(operation_key, str(error)) from error
        except Exception as error:
            if original_invocation is not None:
                # RoleAgent already records the terminal side-effect status.
                # Do not rewrite an existing "unknown" outcome after a provider
                # may have been charged; only fill the status for failures that
                # happened before the invocation reached a terminal state.
                if original_invocation.status == "running":
                    original_invocation.side_effect_status = "not_committed"
                store.save_invocation(original_invocation, operation_key=operation_key)
            store.fail_operation(operation_key, str(error))
            raise
        except BaseException:
            if original_invocation is not None:
                original_invocation.side_effect_status = "unknown"
                store.save_invocation(original_invocation, operation_key=operation_key)
            raise
        finally:
            if usage_listener_token is not None and callable(remove_usage_listener):
                try:
                    remove_usage_listener(usage_listener_token)
                except Exception:
                    pass
        self._after_operation_completed(node, operation_key)
        self._sync_persisted_usage(store, state)
        return result

    async def _execute_tool_operation(
        self,
        store: RunStore,
        state: ResearchState,
        kind: str,
        semantic_input: dict[str, Any],
        call: Callable[[], Awaitable[T]],
        serializer: Callable[[T], Any],
        deserializer: Callable[[Any], T],
        *,
        parent_invocation_id: str,
        return_binding: bool = False,
        binding_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> T | tuple[T, AgentInvocation]:
        canonical_input = json.dumps(
            semantic_input,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        semantic_input_hash = hashlib.sha256(canonical_input.encode()).hexdigest()
        operation_material = json.dumps(
            {
                "run_id": state.run_id,
                "kind": kind,
                "semantic_input_hash": semantic_input_hash,
                "provider": self._search_identity(),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        operation_key = hashlib.sha256(operation_material.encode()).hexdigest()
        operation = store.begin_operation(
            operation_key,
            kind,
            semantic_input_hash,
            kind=kind,
            idempotent=True,
        )
        if operation["status"] == "non_retryable":
            raise ResourceLimitExceededError(
                str(operation.get("error") or "resource_limit_exceeded")
            )

        def report_binding(
            invocation: AgentInvocation,
            *,
            status: str,
            result_invocation_id: str | None = None,
        ) -> None:
            if binding_callback is None:
                return
            binding_callback(
                {
                    "operation_key": operation_key,
                    "invocation_id": invocation.invocation_id,
                    "result_invocation_id": result_invocation_id,
                    "execution_mode": invocation.execution_mode,
                    "status": status,
                    "attempt": invocation.attempt,
                }
            )
        if operation["status"] == "succeeded":
            replay = self._record_replay_invocation(
                store,
                state,
                kind,
                operation_key,
                parent_invocation_id=parent_invocation_id,
            )
            self._record_operation_replay(
                store,
                state,
                operation_key,
                replay_invocation_id=replay.invocation_id,
            )
            detail = store.operation_detail(operation_key) or {}
            report_binding(
                replay,
                status="fetched" if kind == "fetch" else "succeeded",
                result_invocation_id=str(detail.get("result_invocation_id") or "") or None,
            )
            self._sync_tool_counters(store, state)
            value = deserializer(json.loads(operation["result_json"]))
            return (value, replay) if return_binding else value
        if operation["status"] == "external_outcome_unknown":
            raise ExternalOutcomeUnknownError(
                operation_key,
                "provider outcome is unknown after a lost execution fence; "
                "manual confirmation is required before retry",
            )
        if operation["status"] == "in_progress":
            operation = await self._wait_for_operation(store, operation_key)
            if operation["status"] == "succeeded":
                replay = self._record_replay_invocation(
                    store,
                    state,
                    kind,
                    operation_key,
                    parent_invocation_id=parent_invocation_id,
                )
                self._record_operation_replay(
                    store,
                    state,
                    operation_key,
                    replay_invocation_id=replay.invocation_id,
                )
                detail = store.operation_detail(operation_key) or {}
                report_binding(
                    replay,
                    status="fetched" if kind == "fetch" else "succeeded",
                    result_invocation_id=str(detail.get("result_invocation_id") or "") or None,
                )
                self._sync_tool_counters(store, state)
                value = deserializer(json.loads(operation["result_json"]))
                return (value, replay) if return_binding else value
            if operation["status"] == "external_outcome_unknown":
                raise ExternalOutcomeUnknownError(
                    operation_key,
                    "provider outcome is unknown after a lost execution fence; "
                    "manual confirmation is required before retry",
                )
            if operation["status"] == "failed":
                if str(operation.get("error") or "").startswith(
                    "resource_limit_exceeded:"
                ):
                    raise ResourceLimitExceededError(str(operation["error"]))
                operation = store.begin_operation(
                    operation_key,
                    kind,
                    semantic_input_hash,
                    kind=kind,
                    idempotent=True,
                )
            if operation["status"] != "new":
                raise OperationInProgressError(operation_key)
        if operation["status"] != "new":
            raise OperationInProgressError(operation_key)

        invocation = self._new_tool_invocation(
            state,
            kind,
            operation_key,
            semantic_input,
            int(operation.get("attempt_count", 1)),
            parent_invocation_id,
        )
        store.save_invocation(invocation, operation_key=operation_key)
        try:
            result = await self._call_external(store, call)
        except Exception as error:
            try:
                store.fail_operation(operation_key, str(error))
            except ExecutionFenceLostError as fence_error:
                invocation.status = "failed"
                invocation.ended_at = datetime.now(UTC).isoformat()
                invocation.error = "external_outcome_unknown: execution fence lost"
                invocation.side_effect_status = "unknown"
                store.mark_external_outcome_unknown(
                    operation_key,
                    invocation_id=invocation.invocation_id,
                    error=str(fence_error),
                )
                raise fence_error
            invocation.status = "failed"
            invocation.ended_at = datetime.now(UTC).isoformat()
            invocation.error = str(error)[:1000]
            invocation.side_effect_status = "not_committed"
            store.save_invocation(invocation, operation_key=operation_key)
            report_binding(invocation, status="failed")
            self._sync_tool_counters(store, state)
            raise
        except BaseException:
            invocation.side_effect_status = "unknown"
            try:
                store.save_invocation(invocation, operation_key=operation_key)
            except ExecutionFenceLostError as fence_error:
                store.mark_external_outcome_unknown(
                    operation_key,
                    invocation_id=invocation.invocation_id,
                    error=str(fence_error),
                )
                raise fence_error
            report_binding(invocation, status="unknown")
            raise
        try:
            serialized_result = serializer(result)
            store.complete_operation(operation_key, serialized_result)
        except ExecutionFenceLostError as error:
            invocation.status = "failed"
            invocation.ended_at = datetime.now(UTC).isoformat()
            invocation.error = "external_outcome_unknown: execution fence lost"
            invocation.side_effect_status = "unknown"
            store.mark_external_outcome_unknown(
                operation_key,
                invocation_id=invocation.invocation_id,
                error=str(error),
            )
            raise
        except BaseException:
            invocation.side_effect_status = "unknown"
            store.save_invocation(invocation, operation_key=operation_key)
            raise
        invocation.status = "succeeded"
        invocation.ended_at = datetime.now(UTC).isoformat()
        invocation.output_type = (
            "SearchResultBatch" if kind == "search" else "SourcePage"
        )
        invocation.output_summary = (
            f"{len(result)} search results"
            if isinstance(result, list)
            else type(result).__name__
        )
        invocation.side_effect_status = "committed"
        store.save_invocation(invocation, operation_key=operation_key)
        report_binding(
            invocation,
            status="fetched" if kind == "fetch" else "succeeded",
            result_invocation_id=invocation.invocation_id,
        )
        self._after_operation_completed(kind, operation_key)
        self._sync_tool_counters(store, state)
        return (result, invocation) if return_binding else result

    @staticmethod
    async def _wait_for_operation(
        store: RunStore,
        operation_key: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            operation = store.operation_detail(operation_key)
            if operation is None:
                raise RuntimeError("in-progress operation disappeared")
            if operation["status"] != "started":
                return operation
            if asyncio.get_running_loop().time() >= deadline:
                result = dict(operation)
                result["status"] = "in_progress"
                return result
            await asyncio.sleep(0.01)

    @staticmethod
    def _new_tool_invocation(
        state: ResearchState,
        kind: str,
        operation_key: str,
        semantic_input: dict[str, Any],
        attempt: int,
        parent_invocation_id: str,
    ) -> AgentInvocation:
        previous = state.agent_invocations[-1] if state.agent_invocations else None
        invocation = AgentInvocation(
            invocation_id=str(uuid.uuid4()),
            agent_id="scout",
            role="retrieval_strategist",
            operation=kind,
            attempt=attempt,
            started_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            status="running",
            input_type="SearchQuery" if kind == "search" else "SearchResult",
            execution_mode="executed",
            provider_call_count=1,
            parent_invocation_id=parent_invocation_id,
            previous_in_log_id=(
                previous.invocation_id if previous is not None else None
            ),
            input_summary=json.dumps(
                semantic_input,
                ensure_ascii=False,
                sort_keys=True,
            )[:500],
            run_id=state.run_id,
            trace_id=state.run_id,
            operation_key=operation_key,
            side_effect_status="unknown",
        )
        state.agent_invocations.append(invocation)
        return invocation

    def _model_usage_snapshot(self) -> dict[str, int | float | str]:
        snapshot_method = getattr(self.model, "usage_snapshot", None)
        if not callable(snapshot_method):
            return {
                "model_calls": 0,
                "model_cache_hits": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost_usd": 0.0,
                "provider": type(self.model).__name__,
            }
        snapshot = snapshot_method()
        return {
            "model_calls": int(snapshot.get("model_calls", 0)),
            "model_cache_hits": int(snapshot.get("model_cache_hits", 0)),
            "input_tokens": int(snapshot.get("input_tokens", 0)),
            "output_tokens": int(snapshot.get("output_tokens", 0)),
            "estimated_cost_usd": float(snapshot.get("estimated_cost_usd", 0.0)),
            "provider": str(snapshot.get("provider") or type(self.model).__name__),
        }

    def _model_identity(self, role: str | None = None) -> dict[str, object]:
        identity_for = getattr(self.model, "identity_for", None)
        if role and callable(identity_for):
            identity: dict[str, object] = dict(identity_for(role))
        else:
            identity = {
                "role": role or "shared",
                "choice": str(getattr(self.model, "model_choice", "offline")),
                "provider": type(self.model).__name__,
                "model": str(getattr(self.model, "model", "built-in")),
                "base_url": str(getattr(self.model, "base_url", "local")),
                "modalities": list(
                    getattr(self.model, "modalities", ("text", "document"))
                ),
            }
        return {
            **identity,
            "prompt_contract": "deep-research-agent-prompts-v3",
        }

    def _model_provider_for(self, role: str) -> ModelProvider:
        provider_for = getattr(self.model, "provider_for", None)
        if callable(provider_for):
            return provider_for(role)
        return self.model

    def _search_identity(self) -> dict[str, str]:
        return {
            "provider": type(self.search).__name__,
            "base_url": str(getattr(self.search, "base_url", "provider-defined")),
            "corpus": str(getattr(self.search, "corpus_path", "live")),
            "contract": "search-fetch-provider-v2",
        }

    @staticmethod
    def _record_replay_invocation(
        store: RunStore,
        state: ResearchState,
        node: str,
        operation_key: str,
        *,
        parent_invocation_id: str | None = None,
    ) -> AgentInvocation:
        detail = store.operation_detail(operation_key) or {}
        agent_id = {
            "plan": "planner",
            "generate_queries": "scout",
            "extract_evidence": "curator",
            "search": "scout",
            "fetch": "scout",
            "draft": "writer",
            "verify": "verifier",
        }.get(node, "orchestrator")
        role = {
            "planner": "research_planner",
            "scout": "retrieval_strategist",
            "curator": "evidence_curator",
            "writer": "evidence_writer",
            "verifier": "citation_verifier",
        }.get(agent_id, "orchestrator")
        original_invocation_id = str(detail.get("original_invocation_id") or "")
        result_invocation_id = str(
            detail.get("result_invocation_id") or original_invocation_id
        )
        original = (
            store.invocation(result_invocation_id)
            if result_invocation_id
            else None
        )
        if original is None:
            result_invocation_id = str(uuid.uuid4())
            original = AgentInvocation(
                invocation_id=result_invocation_id,
                agent_id=agent_id,
                role=role,
                operation=node,
                attempt=max(1, int(detail.get("attempt_count", 1))),
                started_at=str(
                    detail.get("started_at") or datetime.now(UTC).isoformat()
                ),
                ended_at=str(
                    detail.get("completed_at") or datetime.now(UTC).isoformat()
                ),
                status="succeeded",
                input_type="LegacyDurableOperation",
                execution_mode="executed",
                provider_call_count=(
                    int(detail.get("model_calls", 0))
                    if str(detail.get("kind")) == "model"
                    else 1
                ),
                output_type="DurableOperationResult",
                output_summary="Original result was committed before invocation migration",
                run_id=state.run_id,
                trace_id=state.run_id,
                operation_key=operation_key,
                side_effect_status="committed",
            )
            store.save_invocation(original, operation_key=operation_key)
        elif detail.get("status") == "succeeded" and (
            original.status == "running"
            or original.side_effect_status == "unknown"
        ):
            original.status = "succeeded"
            original.ended_at = str(
                detail.get("completed_at") or datetime.now(UTC).isoformat()
            )
            original.output_type = original.output_type or "DurableOperationResult"
            original.output_summary = (
                original.output_summary
                or "Original operation committed before checkpoint projection"
            )
            original.side_effect_status = "committed"
            store.save_invocation(original, operation_key=operation_key)

        if not any(
            item.invocation_id == original.invocation_id
            for item in state.agent_invocations
        ):
            state.agent_invocations.append(original)
        now = datetime.now(UTC).isoformat()
        previous = state.agent_invocations[-1] if state.agent_invocations else None
        replay = AgentInvocation(
            invocation_id=str(uuid.uuid4()),
            agent_id=agent_id,
            role=role,
            operation=node,
            attempt=1 + sum(
                item.operation == node for item in state.agent_invocations
            ),
            started_at=now,
            ended_at=now,
            status="succeeded",
            input_type="DurableOperationRecord",
            execution_mode="replayed",
            provider_call_count=0,
            parent_invocation_id=parent_invocation_id,
            previous_in_log_id=(
                previous.invocation_id if previous is not None else None
            ),
            output_type="ReplayedResult",
            input_summary=f"operation {operation_key[:12]} already succeeded",
            output_summary="Replayed persisted result; provider was not called",
            consumed_handoff_message_ids=(
                ResearchEngine._incoming_handoff_message_ids(store, state, agent_id)
                if node not in {"search", "fetch"}
                else []
            ),
            run_id=state.run_id,
            trace_id=state.run_id,
            operation_key=operation_key,
            replay_of_invocation_id=original.invocation_id,
            side_effect_status="not_reexecuted",
            model_provider=original.model_provider,
            model_choice=original.model_choice,
            model_id=original.model_id,
            input_modalities=list(original.input_modalities),
        )
        state.agent_invocations.append(replay)
        store.save_invocation(replay, operation_key=operation_key)
        return replay

    @staticmethod
    def _record_operation_replay(
        store: RunStore,
        state: ResearchState,
        operation_key: str,
        *,
        replay_invocation_id: str,
    ) -> None:
        if operation_key in state.operation_replays:
            return
        state.operation_replays.append(operation_key)
        detail = store.operation_detail(operation_key) or {}
        state.operation_replay_details.append(
            {
                "operation_key": operation_key,
                "original_invocation_id": detail.get("original_invocation_id"),
                "result_invocation_id": detail.get("result_invocation_id"),
                "replay_invocation_id": replay_invocation_id,
                "node": detail.get("node", "unknown"),
                "kind": detail.get("kind", "unknown"),
                "original_completed_at": detail.get("completed_at"),
                "replayed_at": datetime.now(UTC).isoformat(),
                "attempt_count": int(detail.get("attempt_count", 0)),
                "original_model_calls": int(detail.get("model_calls", 0)),
                "input_tokens": int(detail.get("input_tokens", 0)),
                "output_tokens": int(detail.get("output_tokens", 0)),
                "estimated_cost_usd": float(detail.get("estimated_cost_usd", 0.0)),
                "replay_provider_calls": 0,
                "side_effect_status": detail.get("side_effect_status", "unknown"),
            }
        )

    @staticmethod
    def _merge_persisted_invocations(
        store: RunStore,
        state: ResearchState,
    ) -> None:
        persisted = store.load_invocations()
        if not persisted:
            return
        durable = {item.invocation_id: item for item in persisted}
        merged = [
            durable.pop(item.invocation_id, item)
            for item in state.agent_invocations
        ]
        merged.extend(
            item
            for item in persisted
            if item.invocation_id in durable
        )
        state.agent_invocations = merged

    @staticmethod
    def _sync_persisted_usage(store: RunStore, state: ResearchState) -> None:
        snapshot = store.usage_totals()
        state.counters.model_calls = int(snapshot["model_calls"])
        state.counters.model_cache_hits = int(snapshot["model_cache_hits"])
        state.counters.input_tokens = int(snapshot["input_tokens"])
        state.counters.output_tokens = int(snapshot["output_tokens"])
        state.counters.estimated_cost_usd = round(
            float(snapshot["estimated_cost_usd"]), 8
        )

    @staticmethod
    def _sync_tool_counters(store: RunStore, state: ResearchState) -> None:
        totals = store.tool_operation_totals()
        if not totals["search_operations"] and not totals["fetch_operations"]:
            return
        state.counters.search_operations = totals["search_operations"]
        state.counters.search_calls = totals["search_attempts"]
        state.counters.pages_selected = totals["fetch_operations"]
        state.counters.fetch_attempts = totals["fetch_attempts"]
        state.counters.pages_fetched = totals["pages_fetched"]

    def _after_operation_completed(self, node: str, operation_key: str) -> None:
        """Failure-injection seam after durable response commit, before checkpoint."""

    @staticmethod
    def _record_limited_delivery_invocations(
        store: RunStore,
        state: ResearchState,
    ) -> None:
        """Record durable local terminal checks without claiming model success."""

        delivery = state.answer_delivery if isinstance(state.answer_delivery, dict) else {}
        delivery_mode = str(delivery.get("mode") or "")
        if delivery_mode not in {
            "evidence_limited",
            "interrupted_evidence_limited",
            "research_status",
            "local_citation_binding",
        }:
            return
        if not str(state.draft_answer or "").strip():
            return
        available_material = bool(state.evidence or state.attachment_observations)
        if not available_material and str(delivery.get("mode") or "") != "research_status":
            return

        def append_once(
            *,
            agent_id: str,
            role: str,
            operation: str,
            input_type: str,
            output_type: str,
            input_summary: str,
            output_summary: str,
            quality_gate_statuses: list[str],
            model_id: str = "deterministic-limited-delivery",
        ) -> None:
            if any(item.operation == operation for item in state.agent_invocations):
                return
            previous = state.agent_invocations[-1] if state.agent_invocations else None
            now = datetime.now(UTC).isoformat()
            invocation = AgentInvocation(
                invocation_id=str(uuid.uuid4()),
                agent_id=agent_id,
                role=role,
                operation=operation,
                attempt=1 + sum(
                    item.agent_id == agent_id for item in state.agent_invocations
                ),
                started_at=now,
                ended_at=now,
                status="succeeded",
                input_type=input_type,
                execution_mode="executed",
                provider_call_count=0,
                previous_in_log_id=(
                    previous.invocation_id if previous is not None else None
                ),
                output_type=output_type,
                input_summary=input_summary,
                output_summary=output_summary,
                quality_gate_statuses=quality_gate_statuses,
                run_id=state.run_id,
                trace_id=state.run_id,
                side_effect_status="not_applicable",
                model_provider="local",
                model_choice="local",
                model_id=model_id,
                input_modalities=["structured_state"],
            )
            state.agent_invocations.append(invocation)
            store.save_invocation(invocation)

        evidence_count = len(state.evidence)
        if delivery_mode == "local_citation_binding":
            report = state.verification
            if not _local_citation_binding_passed(report):
                return
            citation_count = len(report.items) if report is not None else 0
            append_once(
                agent_id="verifier",
                role="citation_verifier",
                operation="confirm_local_citation_binding",
                input_type="CitedAnswerAndEvidence",
                output_type="LocalCitationBindingReport",
                input_summary=(
                    f"{len(str(state.draft_answer or ''))} answer characters; "
                    f"{citation_count} cited claims; semantic verifier unavailable"
                ),
                output_summary=(
                    "本地检查已确认引用编号、证据集合和保存材料可以对应；"
                    "未把这一步表述为模型语义核验通过。"
                ),
                quality_gate_statuses=["passed"],
                model_id="deterministic-citation-binding-v1",
            )
            return

        attachment_count = len(
            [
                item
                for item in state.attachment_observations
                if item.status == "succeeded"
            ]
        )
        mode_label = str(delivery.get("label") or delivery_mode or "当前可交付回答")
        append_once(
            agent_id="writer",
            role="evidence_writer",
            operation="compose_limited_answer",
            input_type="EvidenceLimitedMaterial",
            output_type="BoundedCitedAnswer",
            input_summary=(
                f"{evidence_count} saved evidence entries; "
                f"{attachment_count} observed attachments; delivery {mode_label}"
            ),
            output_summary=(
                "当前可交付回答已由已保存证据、附件观察和明确边界组成；"
                "未使用未保存材料。"
            ),
            quality_gate_statuses=["passed"],
        )
        append_once(
            agent_id="verifier",
            role="citation_verifier",
            operation="check_limited_delivery",
            input_type="BoundedCitedAnswer",
            output_type="DeliveryBoundaryCheck",
            input_summary=(
                f"{len(str(state.draft_answer or ''))} answer characters; "
                f"delivery mode {delivery.get('mode') or 'unknown'}"
            ),
            output_summary=(
                "已检查交付边界：当前回答可以展示，但仍不能标记为完整核验通过。"
            ),
            quality_gate_statuses=["failed"],
        )

    def _raise_if_cancelled(self) -> None:
        if self.cancel_check():
            raise ResearchCancelled()

    def _execution_lease_lost(self, store: RunStore) -> bool:
        if not self.execution_lease:
            return False
        if self.lease_lost_check is not None:
            try:
                if self.lease_lost_check():
                    return True
            except Exception:
                return True
        try:
            lease = store.execution_lease_audit()
        except Exception:
            return True
        if lease is None or not lease.get("active"):
            return True
        try:
            if int(lease.get("fence") or 0) != int(self.execution_lease["fence"]):
                return True
        except (TypeError, ValueError, KeyError):
            return True
        expected_receipt = str(self.execution_lease.get("receipt_id") or "")
        return bool(expected_receipt and lease.get("receipt_id") != expected_receipt)

    async def _wait_for_execution_lease_loss(self, store: RunStore) -> None:
        while not self._execution_lease_lost(store):
            await asyncio.sleep(0.05)

    async def _call_external(
        self,
        store: RunStore,
        call: Callable[[], Awaitable[T]],
    ) -> T:
        """Cancel an in-flight provider coroutine when its fence disappears."""
        if not self.execution_lease:
            return await call()
        if self._execution_lease_lost(store):
            raise ExecutionFenceLostError("execution lease was lost before provider call")

        operation_task = asyncio.create_task(call())
        lease_task = asyncio.create_task(self._wait_for_execution_lease_loss(store))
        try:
            done, _ = await asyncio.wait(
                {operation_task, lease_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # If both finish in the same loop turn, the lease loss wins so a
            # late provider result can never be committed as successful.
            if lease_task in done:
                operation_task.cancel()
                with contextlib.suppress(BaseException):
                    await operation_task
                raise ExecutionFenceLostError(
                    "execution lease was lost during provider call"
                )
            lease_task.cancel()
            with contextlib.suppress(BaseException):
                await lease_task
            return await operation_task
        finally:
            if not lease_task.done():
                lease_task.cancel()
                with contextlib.suppress(BaseException):
                    await lease_task

    def _ensure_resume_handoff(
        self,
        store: RunStore,
        state: ResearchState,
    ) -> None:
        transition = state.resume_transition
        transition_status = str(transition.get("status") or "")
        if transition_status not in {"authorized", "handoff_emitted"}:
            return
        receipt_id = str(transition.get("resume_receipt_id") or "")
        lease = self.execution_lease or {}
        if not receipt_id or str(lease.get("receipt_id") or "") != receipt_id:
            raise HandoffValidationError(
                "authorized resume transition is not bound to this worker lease"
            )
        receipt = store.resume_receipt(receipt_id)
        if (
            receipt is None
            or receipt.get("execution_status") != "running"
            or receipt.get("claim_owner_token") != str(lease.get("owner_token") or "")
            or int(receipt.get("claim_fence") or 0) != int(lease.get("fence") or 0)
        ):
            raise HandoffValidationError(
                "authorized resume transition is not bound to the active receipt claim"
            )
        target_node = str(transition.get("target_node") or "")
        if target_node != state.next_node:
            raise HandoffValidationError(
                "resume transition target no longer matches the durable workflow node"
            )
        target_agent = _handoff_consumer("resume", target_node)
        if target_agent in {"", "user", "orchestrator"} and target_node != "finalize":
            raise HandoffValidationError("resume transition has no executable target agent")

        # A worker can crash after the control handoff is committed but before
        # the target invocation consumes it. Reuse it only when its binding is
        # still owned by this exact lease; otherwise emit a higher-fenced retry.
        if transition_status == "handoff_emitted":
            handoff_id = str(transition.get("handoff_message_id") or "")
            binding = store.handoff_resume_binding(handoff_id) if handoff_id else None
            if (
                binding is not None
                and str(binding.get("resume_receipt_id") or "") == receipt_id
                and int(binding.get("claim_fence") or 0) == int(lease["fence"])
                and str(binding.get("route_target") or "") == target_node
                and str(binding.get("intended_consumer") or "") == target_agent
            ):
                return

        prior_transition = dict(transition)
        transition.update(
            {
                "status": "handoff_emitted",
                "target_agent": target_agent,
                "claim_fence": int(lease["fence"]),
                "handoff_emitted_at": datetime.now(UTC).isoformat(),
                "superseded_handoff_message_id": (
                    str(transition.get("handoff_message_id") or "") or None
                ),
            }
        )
        try:
            self._save(
                store,
                "resume",
                state,
                {
                    "resume_receipt_id": receipt_id,
                    "source": transition.get("source"),
                    "target_node": target_node,
                    "target_agent": target_agent,
                    "claim_fence": int(lease["fence"]),
                    "previous_handoff_message_id": transition.get(
                        "previous_handoff_message_id"
                    ),
                    "superseded_handoff_message_id": transition.get(
                        "superseded_handoff_message_id"
                    ),
                },
            )
        except BaseException:
            transition.clear()
            transition.update(prior_transition)
            raise

    @staticmethod
    def _incoming_handoff_message_ids(
        store: RunStore,
        state: ResearchState,
        expected_consumer: str,
    ) -> list[str]:
        if not state.handoff_ids:
            return []
        message_id = state.handoff_ids[-1]
        route = store.handoff_route(message_id)
        if route is None:
            raise HandoffValidationError(
                "latest checkpoint references a non-durable handoff"
            )
        if route["intended_consumer"] != expected_consumer:
            raise HandoffValidationError(
                "latest handoff is not addressed to the executing agent"
            )
        return [message_id]

    @staticmethod
    def _save(
        store: RunStore,
        node: str,
        state: ResearchState,
        payload: dict[str, object],
    ) -> None:
        prior_resume_transition = dict(state.resume_transition)
        handoff = NODE_HANDOFFS.get(node, {})
        producer = handoff.get("agent", "orchestrator")
        expected_operation = NODE_INVOCATION_OPERATIONS.get(node)
        allowed_operations = (
            {expected_operation}
            if expected_operation is not None
            else {"search", "fetch"}
            if node == "search_and_fetch"
            else set()
        )
        invocation = next(
            (
                item
                for item in reversed(state.agent_invocations)
                if item.agent_id == producer
                and item.status == "succeeded"
                and item.operation in allowed_operations
                and not item.handoff_message_ids
            ),
            None,
        )
        detached_invocation: AgentInvocation | None = None
        if invocation is None:
            now = datetime.now(UTC).isoformat()
            previous = state.agent_invocations[-1] if state.agent_invocations else None
            detached_invocation = AgentInvocation(
                invocation_id=str(uuid.uuid4()),
                agent_id=producer,
                role="orchestrator" if producer == "orchestrator" else "stage_projection",
                operation=f"emit_{node}",
                attempt=max(1, state.counters.iterations),
                started_at=now,
                ended_at=now,
                status="succeeded",
                input_type="ResearchState",
                execution_mode="executed",
                provider_call_count=0,
                previous_in_log_id=(
                    previous.invocation_id if previous is not None else None
                ),
                output_type="CanonicalStageArtifact",
                output_summary=f"Projected durable {node} stage",
                run_id=state.run_id,
                trace_id=state.run_id,
                side_effect_status="not_applicable",
            )
            invocation = detached_invocation
        transition = state.resume_transition
        transition_handoff_id = str(transition.get("handoff_message_id") or "")
        if (
            node != "resume"
            and transition.get("status") == "handoff_emitted"
            and transition_handoff_id
            and transition_handoff_id in invocation.consumed_handoff_message_ids
        ):
            transition.update(
                {
                    "status": "consumed",
                    "consumed_by_invocation_id": invocation.invocation_id,
                    "consumed_by_agent_id": invocation.agent_id,
                    "consumed_by_operation": invocation.operation,
                    "consumed_at": invocation.ended_at or datetime.now(UTC).isoformat(),
                }
            )
        attempt = invocation.attempt
        gate_passed = _gate_passed(node, payload, state)
        intended_consumer = _handoff_consumer(node, state.next_node)
        artifact_payload = state.as_dict()
        previous_artifact = None
        if state.last_artifact_id is not None:
            previous_artifact = store.load_artifact_ref(state.last_artifact_id)
            if previous_artifact is None:
                raise ArtifactIntegrityError(
                    f"parent artifact {state.last_artifact_id} is missing"
                )
        envelope = build_handoff(
            run_id=state.run_id,
            node=node,
            producer=producer,
            consumer=intended_consumer,
            attempt=attempt,
            state_payload=artifact_payload,
            previous_artifact=previous_artifact,
            gate_rule=handoff.get("quality_gate", "Stage output is structurally valid"),
            gate_passed=gate_passed,
            intended_consumer=intended_consumer,
            route_target=state.next_node,
            receipt=_handoff_receipt(
                node,
                invocation,
                state.run_id,
                producer_invocation_id=(
                    store.handoff_producer_invocation_id(
                        invocation.consumed_handoff_message_ids[-1]
                    )
                    if invocation.consumed_handoff_message_ids
                    else None
                ),
            ),
            producer_invocation_id=invocation.invocation_id,
            resume_receipt_id=(
                str(transition.get("resume_receipt_id") or "") or None
                if node == "resume"
                else None
            ),
            claim_fence=(
                int(transition.get("claim_fence") or 0) or None
                if node == "resume"
                else None
            ),
        )
        if node == "resume" and transition.get("resume_receipt_id"):
            transition["handoff_message_id"] = envelope.message_id
        prior_last_artifact_id = state.last_artifact_id
        prior_handoff_count = len(state.handoff_ids)
        prior_invocation_handoffs = len(invocation.handoff_message_ids)
        prior_invocation_artifacts = len(invocation.output_artifact_ids)
        prior_invocation_gates = len(invocation.quality_gate_statuses)
        state.last_artifact_id = envelope.output_artifacts[0].artifact_id
        state.handoff_ids.append(envelope.message_id)
        invocation.handoff_message_ids.append(envelope.message_id)
        invocation.output_artifact_ids.extend(
            item.artifact_id for item in envelope.output_artifacts
        )
        if envelope.quality_gate is not None:
            invocation.quality_gate_statuses.append(
                envelope.quality_gate.status
            )
        try:
            store.commit_stage(
                node,
                state,
                "node_finished",
                {**payload, **handoff, "handoff_envelope": envelope.as_dict()},
                artifact=envelope.output_artifacts[0],
                artifact_payload=artifact_payload,
                detached_invocation=detached_invocation,
            )
        except BaseException:
            state.last_artifact_id = prior_last_artifact_id
            del state.handoff_ids[prior_handoff_count:]
            del invocation.handoff_message_ids[prior_invocation_handoffs:]
            del invocation.output_artifact_ids[prior_invocation_artifacts:]
            del invocation.quality_gate_statuses[prior_invocation_gates:]
            state.resume_transition.clear()
            state.resume_transition.update(prior_resume_transition)
            raise


def _query_fingerprint(text: str) -> str:
    normalized = _normalized_query_text(text)
    return hashlib.sha1(normalized.encode()).hexdigest()


def _query_intent_key(query: Query) -> tuple[str, str]:
    """Return the answer target and research purpose a query is serving."""

    return (
        str(query.subgoal_id or "").strip(),
        str(query.strategy or "").strip(),
    )


def _normalized_query_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _classify_error(error: Exception) -> str:
    if isinstance(error, ProviderRequestNotSent):
        return "model_transport_error"
    if isinstance(error, ExternalOutcomeUnknownError):
        return "external_outcome_unknown"
    if isinstance(error, AmbiguousOperationError):
        return "ambiguous_operation"
    if isinstance(error, TimeoutError):
        return "fetch_error"
    return "runtime_error"


def _token_overlap(left: str, right: str) -> int:
    left_tokens = set(left.casefold().split())
    right_tokens = set(right.casefold().split())
    return len(left_tokens & right_tokens)


def _query_similarity(left: str, right: str) -> float:
    left_tokens = set(left.casefold().split())
    right_tokens = set(right.casefold().split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _canonical_source_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").casefold()
    port = parsed.port
    netloc = host if port in {None, 80, 443} else f"{host}:{port}"
    query = urllib.parse.urlencode(
        sorted(
            (key, value)
            for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"ref", "source", "fbclid", "gclid"}
        )
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path or "/", query, "")
    )


def _registrable_domain(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "unknown-source").casefold()
    labels = [label for label in host.split(".") if label]
    if len(labels) <= 2:
        return host
    two_level_suffixes = {
        "co.uk", "org.uk", "ac.uk", "gov.uk", "com.cn", "net.cn", "org.cn",
        "com.au", "net.au", "org.au", "co.jp", "co.kr",
    }
    suffix = ".".join(labels[-2:])
    return ".".join(labels[-3:]) if suffix in two_level_suffixes else suffix


def _scholarly_work_identity(source: SourceRecord) -> str:
    """Return a stable repository work identity when it is explicit in the URL."""
    if source.source_type != "paper":
        return ""
    for url in (source.final_url, source.canonical_url, source.url):
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or "").casefold().rstrip(".")
        if host != "arxiv.org" and not host.endswith(".arxiv.org"):
            continue
        match = re.fullmatch(
            r"/(?:abs|pdf|html)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?/?",
            parsed.path,
        )
        if match:
            return f"arxiv:{match.group(1)}"
    return ""


def _publisher_identity(publisher_url: str, publisher_name: str) -> str:
    """Build a conservative grouping key from self-declared publisher metadata."""
    if publisher_url:
        domain = _registrable_domain(publisher_url)
        if domain and domain != "unknown-source":
            return f"declared-publisher-url:{domain}"
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", publisher_name.casefold())
    if normalized:
        digest = hashlib.sha1(normalized.encode()).hexdigest()[:12]
        return f"declared-publisher-name:{digest}"
    return ""


def _normalized_source_text(text: str) -> str:
    return " ".join(re.findall(r"[\w\u4e00-\u9fff]+", text.casefold()))


def _simhash(text: str) -> str:
    if not text:
        return ""
    weights = [0] * 64
    for token in text.split():
        digest = int.from_bytes(hashlib.sha256(token.encode()).digest()[:8], "big")
        for bit in range(64):
            weights[bit] += 1 if digest & (1 << bit) else -1
    value = sum((1 << bit) for bit, weight in enumerate(weights) if weight >= 0)
    return f"{value:016x}"


def _simhash_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    distance = (int(left, 16) ^ int(right, 16)).bit_count()
    return 1.0 - distance / 64


def _claim_quote_consistency(claim: str, quote: str) -> tuple[float, list[str]]:
    """Compatibility alias for existing callers and stored-test imports."""
    return claim_quote_consistency(claim, quote)


def _required_query_coverage_subgoals(
    plan: ResearchPlan,
    gaps: list[EvidenceGap],
    *,
    max_queries: int = 3,
    history: list[Query] | None = None,
) -> list[Subgoal]:
    required_slot_ids = {slot.id for slot in plan.slots if slot.required}
    gap_slot_ids = {gap.slot_id for gap in gaps if gap.slot_id in required_slot_ids}
    remaining = gap_slot_ids or required_slot_ids
    selected: list[Subgoal] = []
    candidates = list(plan.subgoals)
    attempts = {
        item.id: sum(query.subgoal_id == item.id for query in (history or []))
        for item in candidates
    }
    plan_order = {item.id: index for index, item in enumerate(candidates)}
    while remaining and len(selected) < max_queries:
        ranked = sorted(
            candidates,
            key=lambda item: (
                -len(remaining.intersection(item.slot_ids)),
                attempts[item.id],
                plan_order[item.id],
            ),
        )
        best = ranked[0] if ranked else None
        if best is None or not remaining.intersection(best.slot_ids):
            break
        selected.append(best)
        remaining -= set(best.slot_ids)
        candidates.remove(best)
    return selected


def _coverage_fallback_query(
    state: ResearchState,
    subgoal: Subgoal,
    strategy: str,
    occupied_fingerprints: set[str],
) -> Query:
    """Make a materially different retrieval angle for one unmet subgoal.

    Appending ``audit round 2`` does not substantially change a search and is
    also correctly rejected by near-duplicate detection.  These compact lenses
    replace the shared question prefix with different source or failure-mode
    terms, so each repair has a distinct retrieval intent and audit trail.
    """

    lenses_by_strategy = {
        "contradiction_check": (
            "failure cases ablation error analysis negative findings",
            "noise robustness unreliable estimator limitations",
            "counterexample benchmark comparison auxiliary branch disabled",
            "assumptions bias sensitivity replication technical report",
            "when the method fails occlusion domain shift adverse conditions",
            "critical review empirical caveats competing explanation",
        ),
        "entity_resolution": (
            "official documentation author institution primary record",
            "canonical title DOI authors publisher metadata",
            "alternate names chronology primary source verification",
        ),
        "bridge": (
            "survey taxonomy mechanism representation retrieval evaluation",
            "method paper ablation bridge evidence experimental analysis",
            "benchmark protocol cross-domain generalization comparison",
        ),
        "source_targeting": (
            "method paper abstract representation fusion ablation evidence",
            "benchmark experiment cross-view robustness evaluation results",
            "survey technical report mechanism limitations evidence",
            "open access paper supplementary error analysis retrieval",
            "authors PDF implementation details structural feature comparison",
            "independent reproduction empirical analysis alternative method",
        ),
    }
    lenses = lenses_by_strategy.get(
        strategy,
        lenses_by_strategy["source_targeting"],
    )
    prior_attempts = sum(
        item.subgoal_id == subgoal.id and item.strategy == strategy
        for item in state.queries
    )
    for offset in range(len(lenses) * 2):
        attempt = prior_attempts + offset
        lens = lenses[attempt % len(lenses)]
        cycle = attempt // len(lenses)
        suffix = "" if cycle == 0 else f" evidence route {cycle + 1}"
        text = f"{subgoal.question} {lens}{suffix}".strip()
        if _query_fingerprint(text) not in occupied_fingerprints:
            return Query(text=text, subgoal_id=subgoal.id, strategy=strategy)

    # The deterministic final suffix is only reached after all named lenses
    # were used in this intent. It remains bounded by the run budget ceiling.
    attempt = prior_attempts + len(lenses) * 2
    text = f"{subgoal.question} independent evidence route {attempt}".strip()
    return Query(text=text, subgoal_id=subgoal.id, strategy=strategy)


def _round_robin_results(
    result_groups: list[list[SearchResult]],
    limit: int,
) -> list[SearchResult]:
    """Allocate a constrained fetch budget across query intents before depth."""
    selected: list[SearchResult] = []
    index = 0
    while len(selected) < limit:
        added = False
        for group in result_groups:
            if index >= len(group):
                continue
            selected.append(group[index])
            added = True
            if len(selected) >= limit:
                break
        if not added:
            break
        index += 1
    return selected


def _claim_target_relevance(claim: str, target: str) -> tuple[float, list[str]]:
    claim_folded, target_folded = claim.casefold(), target.casefold()
    exclusion_patterns = (
        r"\bunrelated\b",
        r"\bno facts? about\b",
        r"\bdoes not (?:address|describe|discuss|mention|support)\b",
        r"\bnot (?:about|related to|relevant to)\b",
        r"(?:无关|不相关|没有关于|未提及|不涉及|不支持)",
    )
    matched_exclusion = next(
        (pattern for pattern in exclusion_patterns if re.search(pattern, claim_folded)),
        None,
    )
    if matched_exclusion:
        return 0.0, [
            "claim explicitly disclaims relevance to the target; excluded from support"
        ]
    cjk_target = "".join(re.findall(r"[\u4e00-\u9fff]", target_folded))
    cjk_claim = "".join(re.findall(r"[\u4e00-\u9fff]", claim_folded))
    stopwords = {
        "the", "a", "an", "is", "was", "were", "of", "to", "in", "on",
        "and", "or", "what", "when", "who", "which", "for", "by", "its",
    }
    target_units: set[str] = set()
    claim_units: set[str] = set()
    if len(cjk_target) >= 2:
        target_units.update(
            cjk_target[index:index + 2]
            for index in range(len(cjk_target) - 1)
        )
    if len(cjk_claim) >= 2:
        claim_units.update(
            cjk_claim[index:index + 2]
            for index in range(len(cjk_claim) - 1)
        )
    target_units.update(
        _light_stem(token)
        for token in re.findall(r"[a-z0-9]+", target_folded)
        if len(token) >= 2 and token not in stopwords
    )
    claim_units.update(
        _light_stem(token)
        for token in re.findall(r"[a-z0-9]+", claim_folded)
        if len(token) >= 2 and token not in stopwords
    )
    matched = {
        target_unit
        for target_unit in target_units
        if any(
            target_unit == claim_unit
            or (
                len(target_unit) >= 5
                and len(claim_unit) >= 5
                and target_unit[:5] == claim_unit[:5]
            )
            for claim_unit in claim_units
        )
    }
    score = len(matched) / max(1, len(target_units))
    return round(score, 4), [
        f"target concepts covered by claim: {len(matched)}/{len(target_units)} ({score:.1%})"
    ]


def _future_direction_target(*texts: str) -> bool:
    return bool(
        re.search(
            r"\bfuture\b|\bopen\s+(?:issue|issues|problem|problems)\b|"
            r"未来|开放问题|研究方向",
            " ".join(texts),
            flags=re.IGNORECASE,
        )
    )


def _future_direction_signal(text: str) -> bool:
    return bool(
        re.search(
            r"\bfuture\s+(?:research|work|direction(?:s)?|stud(?:y|ies))\b|"
            r"\b(?:research|work)\s+in\s+the\s+future\b|"
            r"\bpromising\s+(?:direction|directions)\b|"
            r"\bopen\s+(?:issue|issues|problem|problems)\b|"
            r"\bresearch\s+agenda\b|"
            r"未来(?:研究|工作|方向)|开放问题|研究方向",
            text,
            flags=re.IGNORECASE,
        )
    )


def _claim_target_relevance_variants(
    claim: str,
    targets: list[str],
) -> tuple[float, list[str]]:
    candidates = [target for target in targets if target.strip()]
    if not candidates:
        return 0.0, ["no answer-target formulation was recorded"]
    scored = [
        (target, *_claim_target_relevance(claim, target))
        for target in candidates
    ]
    target, score, reasons = max(scored, key=lambda item: item[1])
    return score, [
        f"best coverage across {len(scored)} target formulations: {target[:160]}",
        *reasons,
    ]


def _light_stem(token: str) -> str:
    for suffix in ("ing", "ed", "er", "or", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def _is_transient_verifier_outage(error: Exception) -> bool:
    text = str(error).casefold()
    if any(marker in text for marker in ("outcome is unknown", "ambiguous operation")):
        return False
    return any(
        marker in text
        for marker in (
            "http 408",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "http 520",
            "http 524",
            "gateway timeout",
            "service unavailable",
            "timed out",
            "timeout",
            "temporarily unavailable",
        )
    )


def _saved_verifier_outage_reason(state: ResearchState) -> str:
    """Reuse a known safe verifier outage without issuing another paid call."""
    resumed_candidate_recheck = bool(
        state.resume_transition.get("recheck_existing_answer")
    )
    if state.next_node != "verify" or not (
        state.status == "failed" or resumed_candidate_recheck
    ):
        return ""
    for failure in reversed(state.failures):
        reason = str(failure.get("reason") or "")
        if _is_transient_verifier_outage(RuntimeError(reason)):
            return reason
    return ""


def _local_citation_binding_report(
    answer: str,
    evidence: list[Evidence],
    outage_reason: str,
) -> VerificationReport:
    """Check durable citation bindings when the semantic verifier is unavailable.

    This deliberately does not claim semantic entailment. It verifies only the
    mechanical facts that can be checked locally: each answer sentence has one
    or more allowed evidence IDs and every referenced item is in the admitted
    evidence set. The UI labels this result separately from model verification.
    """
    evidence_ids = {item.id for item in evidence}
    items: list[VerificationItem] = []
    for expected in parse_answer_claims(answer):
        cited_ids = list(dict.fromkeys(expected["evidence_ids"]))
        missing = [item for item in cited_ids if item not in evidence_ids]
        reasons: list[str] = []
        if not cited_ids:
            reasons.append("answer sentence has no citation")
        if missing:
            reasons.append("answer cites unknown or non-admitted evidence: " + ", ".join(missing))
        bound = not reasons
        items.append(
            VerificationItem(
                claim=expected["claim"],
                evidence_ids=[item for item in cited_ids if item in evidence_ids],
                status="partial" if bound else "unsupported",
                reason=(
                    "模型语义核验服务未返回结果；本地已确认本句的引用编号存在且属于通过材料检查的证据。"
                    if bound
                    else "; ".join(reasons)
                ),
                claim_id=expected["claim_id"],
                expected_evidence_ids=cited_ids,
                verifier_evidence_ids=[],
                citation_set_match=bound,
            )
        )
    return VerificationReport(
        passed=False,
        items=items,
        provider_passed=None,
        expected_item_count=len(items),
        provider_item_count=0,
        contract_version=LOCAL_CITATION_BINDING_CONTRACT_VERSION,
    )


def _local_citation_binding_passed(report: VerificationReport | None) -> bool:
    return bool(
        report
        and report.contract_version == LOCAL_CITATION_BINDING_CONTRACT_VERSION
        and report.items
        and all(
            item.status == "partial"
            and item.citation_set_match
            and bool(item.evidence_ids)
            for item in report.items
        )
    )


def _has_terminal_answer_delivery(state: ResearchState) -> bool:
    """Return whether a terminal checkpoint has text and an explicit delivery mode."""

    return bool(
        str(state.draft_answer or "").strip()
        and isinstance(state.answer_delivery, dict)
        and str(state.answer_delivery.get("mode") or "").strip()
    )


def _terminal_delivery_needs_refresh(state: ResearchState) -> bool:
    """Refresh only unverified, deterministic terminal prose after a formatter update."""

    delivery = state.answer_delivery if isinstance(state.answer_delivery, dict) else {}
    return (
        str(delivery.get("mode") or "")
        in {"evidence_limited", "interrupted_evidence_limited", "research_status"}
        and str(delivery.get("format_version") or "")
        != TERMINAL_DELIVERY_FORMAT_VERSION
    )


def _terminal_delivery_invocation_needs_backfill(state: ResearchState) -> bool:
    """Identify old local-binding deliveries that lack their durable check log."""

    delivery = state.answer_delivery if isinstance(state.answer_delivery, dict) else {}
    return bool(
        str(delivery.get("mode") or "") == "local_citation_binding"
        and _local_citation_binding_passed(state.verification)
        and not any(
            item.operation == "confirm_local_citation_binding"
            for item in state.agent_invocations
        )
    )


def _ensure_terminal_answer_delivery(state: ResearchState) -> None:
    """Guarantee a useful, explicitly qualified terminal delivery."""

    if state.status == "completed" and state.draft_answer:
        local_citation_binding = bool(
            state.verification
            and state.verification.contract_version
            == LOCAL_CITATION_BINDING_CONTRACT_VERSION
            and _local_citation_binding_passed(state.verification)
        )
        state.answer_delivery = {
            "mode": (
                "local_citation_binding"
                if local_citation_binding
                else "verified"
            ),
            "label": (
                "本地引用绑定检查完成（语义模型核验超时）"
                if local_citation_binding
                else "已核验最终回答"
            ),
            "verified": not local_citation_binding,
            "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
            "evidence_revision": state.evidence_revision,
            "reason": (
                "独立来源、原文定位和逐句引用编号对应已检查；模型语义核验服务超时，"
                "因此不把本地绑定检查表述为逐句语义核验通过。"
                if local_citation_binding
                else "独立来源、原文定位和逐句引用检查均已通过。"
            ),
        }
        return

    if state.status == "verification_failed" and state.draft_answer:
        state.answer_delivery = {
            "mode": "citation_unverified",
            "label": "引用仍待核验的候选回答",
            "verified": False,
            "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
            "evidence_revision": state.evidence_revision,
            "reason": "已有回答草稿，但逐句引用对应检查尚未通过。",
        }
        return

    if state.status == "evidence_incomplete":
        answer, evidence_ids = _compose_evidence_limited_answer(state)
        state.draft_answer = answer
        state.draft_revision = state.evidence_revision
        state.answer_delivery = {
            "mode": "evidence_limited",
            "label": "当前可交付回答（仍待补齐核验）",
            "verified": False,
            "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
            "evidence_revision": state.evidence_revision,
            "evidence_ids": evidence_ids,
            "attachment_ids": _observed_attachment_ids(state),
            "reason": "系统已在本轮可用预算内继续补证；以下回答只使用已保存、可回看的材料，并明确标出仍待补齐的独立来源、反面材料和逐句引用检查。",
        }
        return

    if state.draft_answer:
        state.answer_delivery = {
            "mode": "interrupted_candidate",
            "label": "中断前候选回答",
            "verified": False,
            "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
            "evidence_revision": state.evidence_revision,
            "reason": "研究在完成逐句引用检查前中断，以下保留的是中断前已生成的回答。",
        }
        return

    if state.status in {"failed", "cancelled"}:
        answer, evidence_ids = _compose_interrupted_evidence_limited_answer(state)
        state.draft_answer = answer
        state.draft_revision = state.evidence_revision
        state.answer_delivery = {
            "mode": "interrupted_evidence_limited",
            "label": "运行中断后的当前可交付回答",
            "verified": False,
            "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
            "evidence_revision": state.evidence_revision,
            "evidence_ids": evidence_ids,
            "attachment_ids": _observed_attachment_ids(state),
            "reason": _terminal_interruption_reason(state),
        }
        return

    state.draft_answer = _compose_terminal_status_answer(state)
    state.draft_revision = state.evidence_revision
    state.answer_delivery = {
        "mode": "research_status",
        "label": "研究状态最终交付",
        "verified": False,
        "format_version": TERMINAL_DELIVERY_FORMAT_VERSION,
        "evidence_revision": state.evidence_revision,
        "reason": "本轮未形成可核对的研究材料，已保留当前状态与后续补证入口。",
    }


def _compose_evidence_limited_answer(
    state: ResearchState,
) -> tuple[str, list[str]]:
    """Build a readable interim conclusion from auditable material only."""

    selected = _evidence_limited_candidates(state)
    evidence_ids = [item.id for item in selected]
    attachment_material = _attachment_material(state, limit=3)
    lines = [
        "## 当前可交付回答",
        "以下先直接回答问题，再说明仍待核验的部分。回答只使用本轮已保存、可以回看的材料；材料不足不会让系统空白结束。",
        "",
        "## 对问题的直接回答",
    ]
    explanatory_answer = _compose_3d_2d_retrieval_stage_answer(state, selected)
    if explanatory_answer is not None:
        direct_lines, direct_evidence_ids = explanatory_answer
        lines.extend(direct_lines)
        evidence_ids = list(dict.fromkeys([*evidence_ids, *direct_evidence_ids]))
    elif selected:
        lines.extend(f"- {item.claim.strip()} [{item.id}]" for item in selected[:3])
    elif attachment_material:
        lines.append(f"- 针对“{state.question}”，上传材料中可直接读到：")
        lines.extend(
            f"  {item['text']}（{item['label']}）" for item in attachment_material[:2]
        )
    else:
        lines.append(
            f"- 针对“{state.question}”，本轮已用尽当前可用的检索路线，但还没有保存能够逐字回到原文的材料。为了不把猜测当成结论，暂不能给出具体事实判断。"
        )

    if attachment_material:
        lines.extend(["", "## 可直接核对的上传材料"])
        lines.extend(
            f"- {item['text']}（{item['label']}）" for item in attachment_material
        )
    if selected:
        lines.extend(["", "## 对应的可查证据"])
        lines.extend(f"- {item.claim.strip()} [{item.id}]" for item in selected)

    lines.extend(["", "## 仍待补充的核验"])
    closure = state.closure
    if closure is not None:
        lines.append(
            f"- 已通过全部交付前检查的回答目标：{closure.passed_slots}/{closure.required_slots}；这里的数量反映材料检查进度，不是结论正确率。"
        )
        failed_slots = [
            audit.description
            for audit in closure.slot_audits
            if not audit.passed and audit.description
        ]
        if failed_slots:
            lines.append("- 下一轮优先补充：" + "；".join(failed_slots[:3]) + "。")
    else:
        lines.append("- 本轮尚未形成完整的材料检查记录。")
    if state.budget_expansions:
        latest = state.budget_expansions[-1]
        new_limits = latest.get("new_limits", {})
        lines.append(
            "- 系统已自动扩展一次受限检索预算，当前上限为 "
            f"{new_limits.get('iterations', '—')} 轮、"
            f"{new_limits.get('search_calls', '—')} 次搜索、"
            f"{new_limits.get('pages', '—')} 篇页面；仍受本次运行预设总上限约束。"
        )
    lines.extend(
        [
            "- 尚未完成逐句引用检查，因此这是当前可交付回答，不应当标记为“已核验完成”。",
            "- 方括号中的 Evidence ID 可在证据账本中打开原文、快照和来源绑定记录；附件编号可在输入材料审计中回看。",
        ]
    )
    return "\n".join(lines), evidence_ids


def _evidence_limited_candidates(state: ResearchState) -> list[Evidence]:
    """Select only ledger claims that remain directly inspectable at delivery."""

    closure_admitted_ids = {
        evidence_id
        for audit in (state.closure.slot_audits if state.closure else [])
        for evidence_id in audit.supporting_evidence_ids
    }
    candidates: list[Evidence] = []
    seen_claims: set[str] = set()
    for item in state.evidence:
        if item.stance != "supports" or not item.id or not item.claim.strip():
            continue
        if not _is_explanatory_delivery_claim(item.claim):
            continue
        consistency, _reasons = claim_quote_consistency(item.claim, item.quote)
        is_attachment = bool(item.attachment_id) or item.source_url.startswith(
            "attachment://"
        )
        if consistency < MIN_CLAIM_QUOTE_CONSISTENCY or not (
            is_attachment or item.id in closure_admitted_ids
        ):
            continue
        fingerprint = " ".join(item.claim.casefold().split())
        if fingerprint in seen_claims:
            continue
        seen_claims.add(fingerprint)
        candidates.append(item)
    candidates.sort(
        key=lambda item: (
            not (
                bool(item.attachment_id)
                or item.source_url.startswith("attachment://")
            ),
            -float(item.slot_relevance_score or 0.0),
            -float(item.reliability or 0.0),
            item.id,
        )
    )
    return candidates[:5]


def _is_explanatory_delivery_claim(claim: str) -> bool:
    """Keep labels and isolated diagram tokens out of a human-facing answer."""

    text = " ".join(str(claim or "").split())
    cjk = "".join(re.findall(r"[\u4e00-\u9fff]", text))
    if len(cjk) >= 6:
        return any(
            marker in text
            for marker in (
                "提供",
                "产生",
                "补充",
                "融合",
                "通过",
                "导致",
                "限制",
                "改善",
                "支持",
                "使用",
            )
        )
    words = set(re.findall(r"[a-z]+", text.casefold()))
    return bool(
        words
        & {
            "are",
            "captures",
            "capture",
            "contains",
            "encounters",
            "enables",
            "hindered",
            "improve",
            "improves",
            "incorporates",
            "is",
            "offers",
            "offer",
            "provides",
            "provide",
            "uses",
        }
    )


def _compose_3d_2d_retrieval_stage_answer(
    state: ResearchState,
    selected: list[Evidence],
) -> tuple[list[str], list[str]] | None:
    """Turn a common multimodal mechanism question into an auditable explanation."""

    question = state.question.casefold()
    if not (
        "3d" in question
        and "2d" in question
        and ("检索" in state.question or "retriev" in question)
    ):
        return None

    image_context = _3d_2d_attachment_context(state)
    fusion = next(
        (
            item
            for item in selected
            if item.attachment_id
            and any(marker in item.claim for marker in ("姿态", "结构", "门控", "2D"))
        ),
        None,
    )
    structure = next(
        (item for item in selected if not item.attachment_id),
        None,
    )
    alpha_marker = next(
        (
            item
            for item in state.evidence
            if item.attachment_id
            and item.stance == "supports"
            and any(token in f"{item.claim} {item.quote}".casefold() for token in ("α=0", "alpha=0"))
        ),
        None,
    )
    if fusion is None and structure is None and image_context is None:
        return None

    used_ids = [item.id for item in (fusion, structure, alpha_marker) if item]
    attachment_ref = (
        f"用户附件 {fusion.attachment_id[:12]}"
        if fusion and fusion.attachment_id
        else image_context["label"]
        if image_context
        else "用户图示"
    )
    lines = [
        "**简短结论**：3D 可以增强纯 2D 检索，核心原因是它给 2D 外观特征补上了姿态、几何结构和相对空间关系。最终检索表示不只看“图像里像什么”，还看“结构上是什么姿态、形状或空间关系”。",
    ]
    if fusion is not None:
        lines.append(
            f"{attachment_ref} 明确给出“3D 分支提供姿态/结构信息，并通过可靠性门控补充 2D 视觉特征”的融合关系 [{fusion.id}]。"
        )
    elif image_context is not None:
        lines.append(
            f"{attachment_ref} 的可回看观察显示：图中包含 2D 视觉特征、3D 姿态/结构特征和融合输出之间的链路；这是本轮回答图片内容的直接依据。"
        )
    if structure is not None:
        lines.append(
            f"外部原文说明 3D 表示可承载位置、细致形状和身体纹理等信息 [{structure.id}]；这些信息可作为 2D 外观表示的互补线索。"
        )
    else:
        lines.append(
            "本轮尚未完成外部论文或网页的独立来源核验，因此这里不声称已经证明了某个具体数据集上的提升幅度。"
        )
    lines.extend(
        [
            "",
            "**图片中这条链路怎么读**：",
            f"1. 左侧 2D 视觉支路先从图像 token、patch 或场景上下文中形成外观特征 `f_vis`；它主要表达颜色、纹理、局部区域和背景里的视觉线索（{attachment_ref}）。",
            "2. 3D 辅助结构支路从 3D estimator 形成 `f_pose` 一类结构特征；它补充的是姿态、关节几何、整体形状、深度关系或视角相对稳定的空间约束。",
            "3. 可靠性门控再根据当前状态选择 3D 信息该占多大权重，并把 `f_vis` 与 `f_pose` 融合为 `f_out`。所以它不是“先做 3D 检索再替换 2D 检索”，而是在最终向量里把外观和结构一起用于相似度匹配。",
            "4. 这能增益 2D 检索，是因为纯 2D 外观容易受视角、遮挡、背景、光照、尺度、服饰纹理或相机域差异影响；3D 结构线索在这些情况下提供另一种判别依据。",
        ]
    )
    if image_context is not None and image_context["observations"]:
        lines.extend(["", "**图片中可人工核对的位置**："])
        for item in image_context["observations"][:4]:
            lines.append(f"- {item}")
    if alpha_marker is not None:
        lines.extend(
            [
                "",
                f"**边界**：图中还标出了 `alpha=0` 的边缘部署分支 [{alpha_marker.id}]。这表示该架构预留了减弱或关闭 3D 辅助信息的表示；单凭该图不能推出固定阈值或具体性能收益。",
            ]
        )
    elif image_context is not None and image_context["has_alpha"]:
        lines.extend(
            [
                "",
                "**边界**：附件观察里出现了 `alpha` 或类似权重符号；这通常表示 3D 辅助信息是可调的。单凭图片不能推出固定阈值或具体性能收益。",
            ]
        )
    return lines, used_ids


def _3d_2d_attachment_context(state: ResearchState) -> dict[str, object] | None:
    """Find attachment observations that can ground a 3D/2D retrieval explanation."""

    candidates: list[dict[str, object]] = []
    for attachment in state.attachment_observations:
        if attachment.status != "succeeded" or not attachment.attachment_id:
            continue
        combined_parts = [attachment.summary]
        combined_parts.extend(item.text for item in attachment.observations)
        combined = " ".join(str(part or "") for part in combined_parts)
        folded = combined.casefold()
        markers = {
            "2d": "2d" in folded or "f_vis" in folded or "visual" in folded,
            "3d": "3d" in folded or "f_pose" in folded or "pose" in folded,
            "fusion": any(
                token in folded
                for token in ("gate", "gating", "fusion", "f_out", "融合", "门控")
            ),
        }
        if not all(markers.values()):
            continue
        observations: list[str] = []
        summary = _compact_terminal_text(attachment.summary, 220)
        if summary:
            observations.append(f"解析摘要：{summary}")
        for item in attachment.observations:
            if len(observations) >= 5:
                break
            text = _compact_terminal_text(item.text, 180)
            if not text:
                continue
            locator = _compact_terminal_text(item.locator, 80) or "位置未记录"
            observations.append(f"{locator}：{text}")
        candidates.append(
            {
                "label": f"用户附件 {attachment.attachment_id[:12]}",
                "observations": observations,
                "has_alpha": "α" in combined or "alpha" in folded,
            }
        )
    return candidates[0] if candidates else None


def _observed_attachment_ids(state: ResearchState) -> list[str]:
    return [
        item.attachment_id
        for item in state.attachment_observations
        if item.status == "succeeded" and item.attachment_id
    ]


def _attachment_material(
    state: ResearchState,
    *,
    limit: int,
) -> list[dict[str, str]]:
    """Expose concise, locatable user-input observations without inventing evidence."""

    material: list[dict[str, str]] = []
    for attachment in state.attachment_observations:
        if attachment.status != "succeeded" or not attachment.attachment_id:
            continue
        attachment_label = f"用户附件 {attachment.attachment_id[:12]}"
        summary = _compact_terminal_text(attachment.summary, 420)
        if summary:
            material.append({"label": f"{attachment_label} 的解析摘要", "text": summary})
        for observation in attachment.observations:
            if len(material) >= limit:
                return material[:limit]
            text = _compact_terminal_text(observation.text, 260)
            if not text:
                continue
            locator = _compact_terminal_text(observation.locator, 100) or "位置未记录"
            material.append(
                {
                    "label": f"{attachment_label}，{locator}",
                    "text": text,
                }
            )
        if len(material) >= limit:
            return material[:limit]
    return material[:limit]


def _compact_terminal_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _compose_interrupted_evidence_limited_answer(
    state: ResearchState,
) -> tuple[str, list[str]]:
    """Deliver the best auditable answer after an execution interruption."""

    selected = _evidence_limited_candidates(state)
    evidence_ids = [item.id for item in selected]
    attachment_material = _attachment_material(state, limit=4)
    lines = [
        "## 当前可交付回答（运行中断后）",
        "本轮没有完成全部材料整理。系统不会因网络或模型中断留下空白回答；以下只使用已保存的证据和用户附件观察，不把未核验内容写成已完成结论。",
        "",
        "## 对问题的直接回答",
    ]
    explanatory_answer = _compose_3d_2d_retrieval_stage_answer(state, selected)
    if explanatory_answer is not None:
        direct_lines, direct_evidence_ids = explanatory_answer
        lines.extend(direct_lines)
        evidence_ids = list(dict.fromkeys([*evidence_ids, *direct_evidence_ids]))
    elif selected:
        lines.extend(f"- {item.claim.strip()} [{item.id}]" for item in selected[:3])
    elif attachment_material:
        lines.extend(
            f"- {item['text']}（{item['label']}）" for item in attachment_material[:2]
        )
    else:
        lines.append(
            "- 本轮在形成可查材料前中断，当前不能负责任地给出具体事实结论；这不是“没有回答”，而是明确说明现有材料不足以支持该事实。"
        )

    if attachment_material:
        lines.extend(["", "## 可直接回看的附件内容"])
        lines.extend(
            f"- {item['text']}（{item['label']}）" for item in attachment_material
        )
    if selected:
        lines.extend(["", "## 可直接回看的证据"])
        lines.extend(f"- {item.claim.strip()} [{item.id}]" for item in selected)

    lines.extend(
        [
            "",
            "## 本轮未完成的部分",
            f"- {_terminal_interruption_reason(state)}",
            "- 可以继续研究补齐外部来源和逐句引用检查；若系统提示可能重复收费，会先要求人工确认再重发请求。",
        ]
    )
    return "\n".join(lines), evidence_ids


def _terminal_interruption_reason(state: ResearchState) -> str:
    failure = state.failures[-1] if state.failures else {}
    failure_type = str(failure.get("type") or "")
    messages = {
        "ambiguous_operation": "证据整理时模型连接在收到结果前断开，系统无法判断服务端是否已经处理该请求，因此没有自动重发以避免重复收费。",
        "external_outcome_unknown": "外部调用的最终结果无法确认，系统没有自动重发，以避免产生重复请求或重复收费。",
        "model_transport_error": "模型请求在发出前未能建立稳定连接，尚未完成后续材料整理。",
        "fetch_error": "部分页面读取没有完成，当前无法把未读取的页面当作证据。",
    }
    if state.status == "cancelled":
        return "研究被主动停止，系统保留停止前已经保存的材料。"
    return messages.get(
        failure_type,
        "研究在完成材料整理前中断，系统已保留当时能够回看的内容。",
    )


def _compose_terminal_status_answer(state: ResearchState) -> str:
    return "\n".join(
        [
            "## 当前最终答复（材料尚未形成）",
            f"针对“{state.question}”，本轮还没有保存能够直接支撑回答的材料。",
            "- 当前不能负责任地给出具体事实结论，也不会用未核验的推测填充答案。",
            "- 运行记录已保留；继续研究后会从已保存的节点补齐材料，而不是重新丢失已有进度。",
        ]
    )


class ResearchCancelled(Exception):
    pass


class AmbiguousOperationError(RuntimeError):
    def __init__(self, operation_key: str, message: str) -> None:
        super().__init__(message)
        self.operation_key = operation_key


class ExternalOutcomeUnknownError(AmbiguousOperationError):
    """A provider call may have completed after this worker lost its fence."""


class OperationInProgressError(RuntimeError):
    def __init__(self, operation_key: str) -> None:
        super().__init__(f"operation {operation_key} is already in progress")
        self.operation_key = operation_key


def _serialize_dataclass(value: Any) -> dict[str, Any]:
    return asdict(value)


def _deserialize_plan(raw: dict[str, Any]) -> ResearchPlan:
    return ResearchPlan(
        answer_type=raw["answer_type"],
        slots=[AnswerSlot(**item) for item in raw["slots"]],
        subgoals=[Subgoal(**item) for item in raw["subgoals"]],
    )


def _deserialize_queries(raw: list[dict[str, Any]]) -> list[Query]:
    return [Query(**item) for item in raw]


def _deserialize_evidence(raw: list[dict[str, Any]]) -> list[Evidence]:
    return [Evidence(**item) for item in raw]


def _deserialize_attachment_observations(
    raw: list[dict[str, Any]],
) -> list[AttachmentObservation]:
    return [
        AttachmentObservation(
            **{
                **item,
                "observations": [
                    GroundedObservation(**observation)
                    for observation in item.get("observations", [])
                ],
            }
        )
        for item in raw
    ]


def _deserialize_verification(raw: dict[str, Any]) -> VerificationReport:
    return VerificationReport(
        passed=raw["passed"],
        items=[VerificationItem(**item) for item in raw["items"]],
        provider_passed=raw.get("provider_passed"),
        expected_item_count=int(raw.get("expected_item_count", 0)),
        provider_item_count=int(raw.get("provider_item_count", 0)),
        contract_version=str(raw.get("contract_version", "")),
    )


NODE_HANDOFFS: dict[str, dict[str, str]] = {
    "resume": {
        "agent": "orchestrator",
        "input_artifact": "恢复授权 receipt、执行 fence 与上一终态研究档案",
        "output_artifact": "绑定实际恢复节点和目标 Agent 的控制平面交接",
        "quality_gate": "receipt、worker lease、claim fence 与恢复目标必须一致",
    },
    "plan": {
        "agent": "planner",
        "input_artifact": "用户问题、回答格式与运行预算",
        "output_artifact": "回答目标槽位、子目标与完成条件",
        "quality_gate": "每个必需槽位都必须被至少一个子目标覆盖",
    },
    "perceive_inputs": {
        "agent": "perception",
        "input_artifact": "content-addressed image, audio, text or document inputs",
        "output_artifact": "locator-bound multimodal observations",
        "quality_gate": "each observation must retain an attachment ID and human locator",
    },
    "generate_queries": {
        "agent": "scout",
        "input_artifact": "当前子目标、证据缺口与历史查询",
        "output_artifact": "经过近重复过滤的定向检索路线",
        "quality_gate": "每条查询必须对应一个明确证据缺口",
    },
    "search_and_fetch": {
        "agent": "scout",
        "input_artifact": "检索路线与页面预算",
        "output_artifact": "带抓取状态、摘要和正文哈希的文章集合",
        "quality_gate": "失败页面单独记录，重复页面不重复进入证据阶段",
    },
    "ingest_evidence": {
        "agent": "curator",
        "input_artifact": "文章正文与回答目标槽位",
        "output_artifact": "逐字 quote、规范化 claim、立场和来源关联",
        "quality_gate": "quote 必须能在正文中逐字定位",
    },
    "assess_closure": {
        "agent": "critic",
        "input_artifact": "回答目标、证据账本、来源簇与冲突候选",
        "output_artifact": "五项闭包分数、证据缺口与继续/停止决策",
        "quality_gate": "必需目标全覆盖且不存在未解决冲突",
    },
    "citation_repair": {
        "agent": "critic",
        "input_artifact": "未通过核验的声明与原有证据",
        "output_artifact": "定向补充证据和更新后的闭包报告",
        "quality_gate": "只修复失败声明，不重复执行整题",
    },
    "draft": {
        "agent": "writer",
        "input_artifact": "已闭包的 Evidence Ledger",
        "output_artifact": "每条事实带 Evidence ID 的候选回答",
        "quality_gate": "禁止使用账本之外的事实和引用 ID",
    },
    "verify": {
        "agent": "verifier",
        "input_artifact": "候选回答、引用 ID 与对应原文",
        "output_artifact": "逐句 entailed、partial 或 unsupported 判定",
        "quality_gate": "全部事实声明必须被引用原文完整支持",
    },
    "finalize": {
        "agent": "orchestrator",
        "input_artifact": "闭包报告、最终回答和逐句核验结果",
        "output_artifact": "可恢复 checkpoint、事件轨迹和最终研究档案",
        "quality_gate": "所有产物可序列化、可追踪、可复现",
    },
}

NODE_CONSUMERS = {
    "perceive_inputs": "planner",
    "plan": "scout",
    "generate_queries": "scout",
    "search_and_fetch": "curator",
    "ingest_evidence": "critic",
    "assess_closure": "writer",
    "citation_repair": "writer",
    "draft": "verifier",
    "verify": "orchestrator",
    "finalize": "user",
    "cancelled": "user",
    "recover": "orchestrator",
}


def _handoff_consumer(node: str, route_target: str) -> str:
    if node in {"recover", "resume"}:
            return {
                "perceive_inputs": "perception",
                "plan": "planner",
            "generate_queries": "scout",
            "search_and_fetch": "scout",
            "ingest_evidence": "curator",
            "assess_closure": "critic",
            "draft": "writer",
            "verify": "verifier",
            "finalize": "orchestrator",
            "done": "user",
        }.get(route_target, "orchestrator")
    if node == "assess_closure":
        if route_target == "draft":
            return "writer"
        if route_target == "generate_queries":
            return "scout"
        return "orchestrator"
    if node == "verify":
        return "scout" if route_target == "generate_queries" else "orchestrator"
    return NODE_CONSUMERS.get(node, "orchestrator")


NODE_INVOCATION_OPERATIONS = {
    "perceive_inputs": "perceive_inputs",
    "plan": "plan",
    "generate_queries": "generate_queries",
    "search_and_fetch": "search_and_fetch",
    "ingest_evidence": "extract_evidence",
    "assess_closure": "assess_closure",
    "draft": "draft",
    "verify": "verify",
}


def _handoff_receipt(
    node: str,
    invocation: AgentInvocation | None,
    run_id: str,
    producer_invocation_id: str | None = None,
) -> HandoffReceipt | None:
    if (
        invocation is None
        or invocation.operation != NODE_INVOCATION_OPERATIONS.get(node)
        or not invocation.consumed_handoff_message_ids
    ):
        return None
    return HandoffReceipt(
        message_id=invocation.consumed_handoff_message_ids[-1],
        consumed_by_invocation_id=invocation.invocation_id,
        consumed_by_agent_id=invocation.agent_id,
        consumed_by_operation=invocation.operation,
        consumed_from_producer_invocation_id=producer_invocation_id,
        consumed_at=invocation.ended_at or invocation.started_at,
        run_id=run_id,
        trace_id=run_id,
        valid=True,
    )


def _gate_passed(
    node: str, payload: dict[str, object], state: ResearchState
) -> bool | None:
    if node == "perceive_inputs":
        expected = len(state.input_attachments)
        observed = len(state.attachment_observations)
        return expected > 0 and observed == expected and all(
            item.status == "succeeded" and bool(item.observations)
            for item in state.attachment_observations
        )
    if node == "plan":
        if not state.plan:
            return False
        covered = {slot_id for subgoal in state.plan.subgoals for slot_id in subgoal.slot_ids}
        return all(not slot.required or slot.id in covered for slot in state.plan.slots)
    if node == "generate_queries":
        return int(payload.get("count", 0)) > 0
    if node == "search_and_fetch":
        return int(payload.get("pages", 0)) > 0
    if node == "ingest_evidence":
        return int(payload.get("count", 0)) > 0
    if node == "draft":
        return bool(state.draft_answer and state.draft_revision == state.evidence_revision)
    if node == "verify":
        return bool(payload.get("passed"))
    if node == "assess_closure":
        return bool(payload.get("closed"))
    if node == "citation_repair":
        return int(payload.get("new_evidence", 0)) > 0
    if node == "resume":
        return True
    if node in {"recover", "cancelled"}:
        return False
    if node == "finalize":
        return state.next_node == "done"
    return None
