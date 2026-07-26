from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


@dataclass(slots=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    revision: int
    checksum: str
    producer: str
    content_uri: str | None = None
    byte_length: int | None = None
    media_type: str = "application/json"
    canonicalization: str = "json-sort-keys-utf8-v1"
    producer_invocation_id: str | None = None
    handoff_message_id: str | None = None
    parent_artifact_id: str | None = None
    metadata_hash: str = ""


@dataclass(slots=True)
class QualityGateResult:
    status: Literal["passed", "failed", "unknown"]
    rule: str
    reason: str


@dataclass(slots=True)
class HandoffReceipt:
    """Proof that a later invocation explicitly consumed one handoff message."""

    message_id: str
    consumed_by_invocation_id: str
    consumed_by_agent_id: str
    consumed_at: str
    run_id: str | None = None
    trace_id: str | None = None
    consumed_by_operation: str | None = None
    consumed_from_producer_invocation_id: str | None = None
    valid: bool = True
    validation_error: str | None = None


@dataclass(slots=True)
class HandoffEnvelope:
    """A routed handoff, not a delivery acknowledgement.

    consumer is the legacy alias for intended_consumer. route_target names the
    workflow node selected by the orchestrator. Only receipt proves consumption.
    """

    schema_version: str
    message_id: str
    trace_id: str
    run_id: str
    producer: str
    consumer: str
    attempt: int
    idempotency_key: str
    created_at: str
    input_artifacts: list[ArtifactRef] = field(default_factory=list)
    output_artifacts: list[ArtifactRef] = field(default_factory=list)
    quality_gate: QualityGateResult | None = None
    intended_consumer: str | None = None
    route_target: str | None = None
    receipt: HandoffReceipt | None = None
    producer_invocation_id: str | None = None
    receipt_validation: Literal["not_present", "valid", "invalid"] = "not_present"
    receipt_validation_error: str | None = None
    resume_receipt_id: str | None = None
    claim_fence: int | None = None

    def __post_init__(self) -> None:
        # consumer is a legacy routing alias, not proof that delivery occurred.
        if self.intended_consumer is None:
            self.intended_consumer = self.consumer
        if self.route_target is None:
            self.route_target = self.intended_consumer
        if self.receipt is not None:
            self.receipt_validation = "valid" if self.receipt.valid else "invalid"
            self.receipt_validation_error = self.receipt.validation_error

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class AgentInvocation:
    """One chronological agent log entry with explicit execution provenance."""

    invocation_id: str
    agent_id: str
    role: str
    operation: str
    attempt: int
    started_at: str
    ended_at: str | None
    status: Literal["running", "succeeded", "failed", "cancelled"]
    input_type: str
    execution_mode: Literal["executed", "replayed"] = "executed"
    provider_call_count: int | None = None
    previous_in_log_id: str | None = None
    output_type: str | None = None
    error: str | None = None
    parent_invocation_id: str | None = None
    input_summary: str = ""
    output_summary: str = ""
    consumed_handoff_message_ids: list[str] = field(default_factory=list)
    handoff_message_ids: list[str] = field(default_factory=list)
    output_artifact_ids: list[str] = field(default_factory=list)
    quality_gate_statuses: list[str] = field(default_factory=list)
    run_id: str = ""
    trace_id: str = ""
    operation_key: str | None = None
    replay_of_invocation_id: str | None = None
    side_effect_status: Literal[
        "not_applicable",
        "unknown",
        "committed",
        "not_committed",
        "not_reexecuted",
    ] = "not_applicable"
    provenance_status: Literal[
        "store_consistent",
        "legacy_backfilled",
        "identity_mismatch",
        "unverified",
    ] = "unverified"
    provenance_reason: str = ""
    model_provider: str = ""
    model_choice: str = ""
    model_id: str = ""
    input_modalities: list[str] = field(default_factory=list)


def build_handoff(
    *,
    run_id: str,
    node: str,
    producer: str,
    consumer: str,
    attempt: int,
    state_payload: dict[str, Any],
    previous_artifact: ArtifactRef | None,
    gate_rule: str,
    gate_passed: bool | None,
    intended_consumer: str | None = None,
    route_target: str | None = None,
    receipt: HandoffReceipt | None = None,
    producer_invocation_id: str | None = None,
    resume_receipt_id: str | None = None,
    claim_fence: int | None = None,
) -> HandoffEnvelope:
    serialized = canonical_artifact_bytes(state_payload)
    checksum = hashlib.sha256(serialized).hexdigest()
    message_id = str(uuid.uuid4())
    artifact_id = "A" + hashlib.sha1(
        f"{run_id}|{node}|{attempt}|{checksum}".encode()
    ).hexdigest()[:12]
    output = ArtifactRef(
        artifact_id=artifact_id,
        kind=f"research/{node}",
        revision=attempt,
        checksum=checksum,
        producer=producer,
        content_uri=f"artifacts/{artifact_id}.json",
        byte_length=len(serialized),
        producer_invocation_id=producer_invocation_id,
        handoff_message_id=message_id,
        parent_artifact_id=(
            previous_artifact.artifact_id if previous_artifact is not None else None
        ),
    )
    output.metadata_hash = artifact_metadata_hash(output)
    inputs = [previous_artifact] if previous_artifact is not None else []
    gate_status: Literal["passed", "failed", "unknown"] = (
        "unknown" if gate_passed is None else "passed" if gate_passed else "failed"
    )
    gate_reason = {
        "passed": "Stage output passed the declared quality gate.",
        "failed": "Stage executed, but its output failed the declared quality gate; downstream delivery is blocked or rerouted.",
        "unknown": "The stage did not emit a machine-decidable quality-gate result.",
    }[gate_status]
    idempotency_key = hashlib.sha256(
        f"{run_id}|{node}|{attempt}|{checksum}".encode()
    ).hexdigest()
    return HandoffEnvelope(
        schema_version="deep-research-handoff/1.1",
        message_id=message_id,
        trace_id=run_id,
        run_id=run_id,
        producer=producer,
        producer_invocation_id=producer_invocation_id,
        consumer=consumer,
        intended_consumer=intended_consumer or consumer,
        route_target=route_target or intended_consumer or consumer,
        receipt=receipt,
        attempt=attempt,
        idempotency_key=idempotency_key,
        created_at=datetime.now(UTC).isoformat(),
        resume_receipt_id=resume_receipt_id,
        claim_fence=claim_fence,
        input_artifacts=inputs,
        output_artifacts=[output],
        quality_gate=QualityGateResult(
            status=gate_status,
            rule=gate_rule,
            reason=gate_reason,
        ),
    )


def canonical_artifact_bytes(payload: dict[str, Any]) -> bytes:
    """Return the exact byte representation covered by ArtifactRef.checksum."""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_artifact_metadata_bytes(artifact: ArtifactRef) -> bytes:
    """Canonical bytes covered by ArtifactRef.metadata_hash.

    The digest field itself is excluded so the manifest is not self-referential.
    Producer, handoff, and parent bindings are all covered by this digest.
    """
    payload = asdict(artifact)
    payload.pop("metadata_hash", None)
    return canonical_artifact_bytes(payload)


def artifact_metadata_hash(artifact: ArtifactRef) -> str:
    return hashlib.sha256(canonical_artifact_metadata_bytes(artifact)).hexdigest()
