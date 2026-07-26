from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .schemas import (
    ClosureReport,
    ContradictionAudit,
    AttachmentObservation,
    Evidence,
    EvidenceGap,
    Page,
    Query,
    ResearchPlan,
    SourceRecord,
    VerificationReport,
    InputAttachment,
)
from .contracts import AgentInvocation


@dataclass(slots=True)
class Counters:
    iterations: int = 0
    search_calls: int = 0
    search_operations: int = 0
    pages_fetched: int = 0
    pages_selected: int = 0
    fetch_attempts: int = 0
    duplicate_queries: int = 0
    verification_repairs: int = 0
    model_calls: int = 0
    model_cache_hits: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


@dataclass(slots=True)
class ResearchState:
    run_id: str
    question: str
    status: str = "initialized"
    next_node: str = "plan"
    plan: ResearchPlan | None = None
    queries: list[Query] = field(default_factory=list)
    pending_queries: list[Query] = field(default_factory=list)
    pending_pages: list[Page] = field(default_factory=list)
    pending_gaps: list[EvidenceGap] = field(default_factory=list)
    input_attachments: list[InputAttachment] = field(default_factory=list)
    attachment_observations: list[AttachmentObservation] = field(default_factory=list)
    attachment_pages: list[Page] = field(default_factory=list)
    attachments_ingested: bool = False
    evidence: list[Evidence] = field(default_factory=list)
    sources: list[SourceRecord] = field(default_factory=list)
    contradiction_checked_slots: list[str] = field(default_factory=list)
    contradiction_checks: list[ContradictionAudit] = field(default_factory=list)
    last_artifact_id: str | None = None
    handoff_ids: list[str] = field(default_factory=list)
    agent_invocations: list[AgentInvocation] = field(default_factory=list)
    closure: ClosureReport | None = None
    draft_answer: str | None = None
    # Describes whether draft_answer is fully verified or an explicit,
    # deterministic evidence-limited delivery. Kept alongside the text so
    # clients cannot infer verification solely from the presence of an answer.
    answer_delivery: dict[str, Any] = field(default_factory=dict)
    verification: VerificationReport | None = None
    evidence_revision: int = 0
    closure_revision: int = -1
    draft_revision: int = -1
    verification_revision: int = -1
    methodology: dict[str, Any] = field(default_factory=dict)
    operation_replays: list[str] = field(default_factory=list)
    operation_replay_details: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    suspension: dict[str, Any] = field(default_factory=dict)
    resume_transition: dict[str, Any] = field(default_factory=dict)
    budget_limits: dict[str, int] = field(default_factory=dict)
    budget_ceilings: dict[str, int] = field(default_factory=dict)
    # Bounded automatic extensions are persisted so people can see exactly
    # when the engine spent beyond its initial working budget.
    budget_expansions: list[dict[str, Any]] = field(default_factory=list)
    counters: Counters = field(default_factory=Counters)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
