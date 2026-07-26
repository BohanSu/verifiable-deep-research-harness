from __future__ import annotations

from dataclasses import asdict, dataclass, field
import math
from typing import Any, Literal


Stance = Literal["supports", "contradicts", "context"]
InputModality = Literal["text", "document", "image", "audio"]


@dataclass(slots=True)
class InputAttachment:
    """A content-addressed user input; raw bytes never live in a checkpoint."""

    id: str
    name: str
    media_type: str
    modality: InputModality
    sha256: str
    byte_length: int
    content_uri: str
    created_at: str
    status: str = "stored"
    parser_version: str = ""
    error: str | None = None


@dataclass(slots=True)
class GroundedObservation:
    """One model observation with a human-locatable page/region/time anchor."""

    locator: str
    text: str
    kind: str = "description"
    confidence: float = 0.0
    page: int | None = None
    region: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass(slots=True)
class AttachmentObservation:
    attachment_id: str
    modality: InputModality
    summary: str
    observations: list[GroundedObservation] = field(default_factory=list)
    model_choice: str = ""
    model_id: str = ""
    parser_version: str = ""
    status: str = "succeeded"
    error: str | None = None


@dataclass(slots=True)
class AnswerSlot:
    id: str
    description: str
    required: bool = True
    value: str | None = None
    supporting_evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0


@dataclass(slots=True)
class Subgoal:
    id: str
    question: str
    slot_ids: list[str]
    done_when: str


@dataclass(slots=True)
class ResearchPlan:
    answer_type: str
    slots: list[AnswerSlot]
    subgoals: list[Subgoal]


@dataclass(slots=True)
class Query:
    text: str
    subgoal_id: str
    strategy: str


@dataclass(slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    source_type: str = "web"


@dataclass(slots=True)
class Page:
    url: str
    title: str
    text: str
    source_type: str = "web"
    content_hash: str = ""
    fetched_at: str = ""
    http_status: int | None = None
    content_type: str = ""
    parser_version: str = ""
    bytes_read: int = 0
    cache_hit: bool = False
    canonical_url: str = ""
    publisher_name: str = ""
    publisher_url: str = ""
    author_names: list[str] = field(default_factory=list)
    site_name: str = ""
    upstream_urls: list[str] = field(default_factory=list)
    provenance_signals: list[str] = field(default_factory=list)
    # A page is transient, but these fields preserve the exact durable fetch
    # and snapshot that produced the text handed to the curator.
    fetch_record_id: str = ""
    snapshot_sha256: str = ""
    snapshot_available: bool = False
    fetch_binding_status: str = "unbound"
    fetch_binding_valid: bool | None = None
    content_hash_scope: str = "unknown"
    attachment_id: str = ""
    modality: str = ""
    source_locator: str = ""
    perception_model: str = ""
    grounding_confidence: float = 0.0
    # Routing context is derived from the durable discovery ledger. It is not
    # source content and is only used to help the curator choose the relevant
    # answer slot for an otherwise grounded quote.
    retrieval_query_texts: list[str] = field(default_factory=list)
    retrieval_subgoal_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SourceRecord:
    id: str
    url: str
    title: str
    source_type: str
    snippet: str
    query_texts: list[str] = field(default_factory=list)
    status: Literal["discovered", "fetched", "failed"] = "discovered"
    iteration: int = 0
    content_hash: str = ""
    error: str | None = None
    discovered_at: str = ""
    fetched_at: str = ""
    final_url: str = ""
    http_status: int | None = None
    content_type: str = ""
    parser_version: str = ""
    bytes_read: int = 0
    cache_hit: bool = False
    canonical_url: str = ""
    registrable_domain: str = ""
    normalized_content_hash: str = ""
    simhash: str = ""
    near_duplicate_of_source_id: str | None = None
    near_duplicate_similarity: float = 0.0
    origin_cluster_id: str = ""
    independence_status: str = "unknown"
    independence_reason: str = ""
    source_role: str = "unknown"
    authority_scope: str = ""
    publisher_name: str = ""
    publisher_url: str = ""
    publisher_id: str = ""
    author_names: list[str] = field(default_factory=list)
    site_name: str = ""
    upstream_urls: list[str] = field(default_factory=list)
    provenance_signals: list[str] = field(default_factory=list)
    snapshot_available: bool = False
    snapshot_sha256: str = ""
    discovery_invocation_ids: list[str] = field(default_factory=list)
    discovery_operation_keys: list[str] = field(default_factory=list)
    fetch_invocation_id: str = ""
    fetch_result_invocation_id: str = ""
    fetch_operation_key: str = ""
    fetch_execution_mode: str = ""
    fetch_provider: str = ""
    fetch_mode: str = "unknown"
    fetch_binding_status: str = "unbound"
    fetch_binding_valid: bool | None = None
    fetch_record_id: str = ""
    content_hash_scope: str = "unknown"
    attachment_id: str = ""
    modality: str = ""
    perception_model: str = ""


@dataclass(slots=True)
class Evidence:
    id: str
    subgoal_id: str
    slot_id: str
    claim: str
    quote: str
    source_url: str
    source_title: str
    stance: Stance
    reliability: float
    extraction_confidence: float
    content_hash: str
    source_cluster_id: str
    source_id: str = ""
    origin_cluster_id: str = ""
    independence_status: str = "unknown"
    independence_basis: str = ""
    source_role: str = "unknown"
    authority_scope: str = ""
    claim_quote_consistency: float = 0.0
    claim_quote_check_reasons: list[str] = field(default_factory=list)
    slot_relevance_score: float = 0.0
    slot_relevance_reasons: list[str] = field(default_factory=list)
    # Exact provenance is intentionally separate from source_id.  A source
    # can have multiple fetch attempts and a truncated saved snapshot.
    fetch_record_id: str = ""
    snapshot_sha256: str = ""
    snapshot_available: bool = False
    fetch_binding_status: str = "unbound"
    fetch_binding_valid: bool | None = None
    content_hash_scope: str = "unknown"
    attachment_id: str = ""
    modality: str = ""
    source_locator: str = ""
    perception_model: str = ""
    grounding_confidence: float = 0.0

    def __post_init__(self) -> None:
        for field_name in (
            "reliability",
            "extraction_confidence",
            "claim_quote_consistency",
            "slot_relevance_score",
            "grounding_confidence",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Evidence.{field_name} must be a finite number in [0, 1]")
            numeric = float(value)
            if not math.isfinite(numeric) or not 0.0 <= numeric <= 1.0:
                raise ValueError(f"Evidence.{field_name} must be a finite number in [0, 1]")
            setattr(self, field_name, numeric)


@dataclass(slots=True)
class EvidenceGap:
    type: str
    slot_id: str
    description: str
    preferred_source: str = "independent_source"


@dataclass(slots=True)
class ContradictionAudit:
    slot_id: str
    query_text: str
    status: str
    executed_at: str
    result_count: int = 0
    pages_inspected: int = 0
    counterevidence_found: bool = False
    error: str | None = None
    inspected_source_ids: list[str] = field(default_factory=list)
    relevant_pages_inspected: int = 0
    relevant_source_ids: list[str] = field(default_factory=list)
    irrelevant_source_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SlotGateAudit:
    slot_id: str
    description: str
    passed: bool
    supporting_evidence_ids: list[str] = field(default_factory=list)
    contradicting_evidence_ids: list[str] = field(default_factory=list)
    source_clusters: list[str] = field(default_factory=list)
    required_source_count: int = 0
    effective_source_count: int = 0
    source_gate_passed: bool = False
    authoritative_exception_used: bool = False
    exact_quote_gate_passed: bool = False
    contradiction_checked: bool = False
    conflict_gate_passed: bool = False
    candidate_values: list[str] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)
    origin_clusters: list[str] = field(default_factory=list)
    weak_provenance_evidence_ids: list[str] = field(default_factory=list)
    dependent_evidence_ids: list[str] = field(default_factory=list)
    consensus_excluded_evidence_ids: list[str] = field(default_factory=list)
    source_counting_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ClosureReport:
    closed: bool
    score: float | None
    slot_coverage: float | None
    source_independence: float | None
    evidence_entailment: float | None
    source_reliability: float | None
    conflict_resolution: float | None
    score_status: Literal["observed", "invalid"] = "observed"
    gaps: list[EvidenceGap] = field(default_factory=list)
    required_slots: int = 0
    passed_slots: int = 0
    hard_gate_passed: bool = False
    gate_failures: list[str] = field(default_factory=list)
    slot_audits: list[SlotGateAudit] = field(default_factory=list)


@dataclass(slots=True)
class VerificationItem:
    claim: str
    evidence_ids: list[str]
    status: Literal["entailed", "partial", "unsupported"]
    reason: str
    claim_id: str = ""
    expected_evidence_ids: list[str] = field(default_factory=list)
    verifier_evidence_ids: list[str] = field(default_factory=list)
    citation_set_match: bool = False


@dataclass(slots=True)
class VerificationReport:
    passed: bool
    items: list[VerificationItem]
    provider_passed: bool | None = None
    expected_item_count: int = 0
    provider_item_count: int = 0
    contract_version: str = ""


def to_dict(value: Any) -> Any:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return value
