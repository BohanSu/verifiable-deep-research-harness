from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import fcntl
import time
import urllib.parse
import uuid
from contextlib import closing, contextmanager
from dataclasses import asdict, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schemas import (
    AnswerSlot,
    AttachmentObservation,
    ClosureReport,
    ContradictionAudit,
    Evidence,
    EvidenceGap,
    GroundedObservation,
    Page,
    Query,
    ResearchPlan,
    Subgoal,
    SourceRecord,
    SlotGateAudit,
    VerificationItem,
    VerificationReport,
    InputAttachment,
)
from .contracts import (
    AgentInvocation,
    ArtifactRef,
    artifact_metadata_hash,
    canonical_artifact_bytes,
)
from .state import Counters, ResearchState


class ExecutionFenceLostError(RuntimeError):
    pass


class HandoffValidationError(ValueError):
    pass


class ArtifactIntegrityError(RuntimeError):
    pass


@contextmanager
def agui_interrupt_index_lock(runs_dir: Path):
    """Serialize the cross-run interrupt snapshot with interrupt creation.

    The lock is deliberately outside any run database. A thread-wide resume
    therefore cannot observe a directory snapshot and then race a producer
    that publishes another open interrupt in a different run store.
    """

    root = runs_dir.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / ".agui-interrupt-index.lock"
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


INVOCATION_IDENTITY_VERSION = "agent-invocation-identity/v1"
INVOCATION_IDENTITY_BACKFILL_VERSION = "agent-invocation-identity/backfilled-v1"
SOURCE_FETCH_BINDING_VERSION = "source-fetch-binding/v2"
_HANDOFF_PENDING_BINDING_STATUSES = frozenset(
    {"pending_receipt", "retry_pending_receipt", "replay_pending_receipt"}
)
_USAGE_STATUSES = frozenset(
    {"complete", "partial", "not_applicable", "unavailable"}
)
_PRICING_STATUSES = frozenset(
    {"complete", "partial", "not_applicable", "unavailable"}
)
_RESUME_EXECUTION_STATUSES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "failed",
        "startup_failed",
        "not_required",
        "legacy_unverified",
    }
)
_NON_RETRYABLE_OPERATION_PREFIXES = ("resource_limit_exceeded:",)
_INVOCATION_IDENTITY_FIELDS = (
    "run_id",
    "trace_id",
    "operation_key",
    "agent_id",
    "role",
    "operation",
    "attempt",
    "started_at",
    "input_type",
    "execution_mode",
    "replay_of_invocation_id",
    "parent_invocation_id",
    "previous_in_log_id",
    "consumed_handoff_message_ids",
)
_INVOCATION_OUTCOME_FIELDS = (
    "status",
    "ended_at",
    "provider_call_count",
    "output_type",
    "error",
    "input_summary",
    "output_summary",
    "handoff_message_ids",
    "output_artifact_ids",
    "quality_gate_statuses",
    "side_effect_status",
    "model_provider",
    "model_choice",
    "model_id",
    "input_modalities",
)
_INVOCATION_DERIVED_LINK_FIELDS = frozenset(
    {"handoff_message_ids", "output_artifact_ids", "quality_gate_statuses"}
)
_ROUTE_TARGET_OPERATIONS = {
    "plan": "plan",
    "generate_queries": "generate_queries",
    "search_and_fetch": "search_and_fetch",
    "ingest_evidence": "extract_evidence",
    "assess_closure": "assess_closure",
    "draft": "draft",
    "verify": "verify",
}
_AGENT_INVOCATION_FIELD_NAMES = {
    item.name for item in fields(AgentInvocation)
}


def _owner_token_fingerprint(owner_token: str | None) -> str | None:
    if not owner_token:
        return None
    return hashlib.sha256(owner_token.encode("utf-8")).hexdigest()[:16]


def _invocation_identity(payload: dict[str, Any]) -> dict[str, Any]:
    identity = {field: payload.get(field) for field in _INVOCATION_IDENTITY_FIELDS}
    identity["attempt"] = int(identity.get("attempt") or 0)
    identity["consumed_handoff_message_ids"] = [
        str(item)
        for item in identity.get("consumed_handoff_message_ids") or []
    ]
    return identity


def _invocation_identity_hash(identity: dict[str, Any]) -> str:
    content = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _invocation_outcome(payload: dict[str, Any]) -> dict[str, Any]:
    outcome = {
        field: payload.get(field) for field in _INVOCATION_OUTCOME_FIELDS
    }
    for field in (
        "handoff_message_ids",
        "output_artifact_ids",
        "quality_gate_statuses",
        "input_modalities",
    ):
        outcome[field] = [str(item) for item in outcome.get(field) or []]
    for field in ("model_provider", "model_choice", "model_id"):
        outcome[field] = str(outcome.get(field) or "")
    if outcome["provider_call_count"] is not None:
        outcome["provider_call_count"] = int(outcome["provider_call_count"])
    return outcome


def _invocation_outcome_hash(outcome: dict[str, Any]) -> str:
    content = json.dumps(
        outcome,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _canonical_source_fetch_url(url: str) -> str:
    """Canonicalize a fetch target using the retrieval operation's URL contract."""

    try:
        parsed = urllib.parse.urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("source fetch requested URL is invalid") from error
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("source fetch requested URL must be an absolute HTTP(S) URL")
    host = parsed.hostname.casefold()
    netloc = host if port in {None, 80, 443} else f"{host}:{port}"
    query = urllib.parse.urlencode(
        sorted(
            (key, value)
            for key, value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if not key.casefold().startswith("utm_")
            and key.casefold() not in {"ref", "source", "fbclid", "gclid"}
        )
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), netloc, parsed.path or "/", query, "")
    )


def _source_fetch_binding_digest(binding: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(binding).encode("utf-8")).hexdigest()


def _source_fetch_record_id(
    run_id: str,
    source_id: str,
    operation_key: str,
    invocation_id: str,
    status: str,
    attempt: int,
) -> str:
    material = _canonical_json(
        {
            "run_id": run_id,
            "source_id": source_id,
            "operation_key": operation_key,
            "invocation_id": invocation_id,
            "status": status,
            "attempt": int(attempt),
        }
    )
    return "F" + hashlib.sha256(material.encode()).hexdigest()[:20]


def _aggregate_evidence_status(statuses: list[str]) -> str:
    if not statuses:
        return "unavailable"
    applicable = [status for status in statuses if status != "not_applicable"]
    if not applicable:
        return "not_applicable"
    if all(status == "complete" for status in applicable):
        return "complete"
    if any(status in {"complete", "partial"} for status in applicable):
        return "partial"
    return "unavailable"


def _usage_evidence(
    usage: dict[str, Any] | None,
    *,
    operation_kind: str,
) -> dict[str, Any]:
    """Classify usage evidence without treating zero as measured usage."""

    if usage is None:
        return {
            "model_calls": 0,
            "model_cache_hits": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost_usd": 0.0,
            "usage_status": (
                "unavailable" if operation_kind == "model" else "not_applicable"
            ),
            "usage_reason": (
                "The model provider did not expose a usage snapshot."
                if operation_kind == "model"
                else "This operation does not carry model usage evidence."
            ),
            "provider": "unknown",
            "pricing_status": (
                "unavailable" if operation_kind == "model" else "not_applicable"
            ),
            "pricing_reason": "No token usage was available for pricing.",
        }

    provider = str(usage.get("provider") or "unknown")
    model_calls = int(usage.get("model_calls", 0) or 0)
    model_cache_hits = int(usage.get("model_cache_hits", 0) or 0)
    input_present = "input_tokens" in usage or "prompt_tokens" in usage
    output_present = "output_tokens" in usage or "completion_tokens" in usage
    input_tokens = int(
        usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    )
    output_tokens = int(
        usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    )
    estimated_cost = float(usage.get("estimated_cost_usd", 0.0) or 0.0)
    snapshot_available = usage.get("usage_snapshot_available", True) is not False
    usage_applicability = str(
        usage.get("usage_applicability") or "applicable"
    ).casefold()
    mock_provider = provider.casefold().endswith("mockmodelprovider")

    if operation_kind != "model":
        usage_status = "not_applicable"
        usage_reason = "This operation is not a model operation."
    elif usage_applicability == "not_applicable" or mock_provider:
        usage_status = "not_applicable"
        usage_reason = "The configured mock provider has no billable token usage."
    elif not snapshot_available:
        usage_status = "unavailable"
        usage_reason = "The model provider did not expose a usage snapshot."
    elif model_calls <= 0 and model_cache_hits <= 0:
        usage_status = "unavailable"
        usage_reason = "No measurable provider usage delta was recorded."
    elif not input_present or not output_present:
        usage_status = "partial"
        usage_reason = "Provider usage did not include both prompt and completion token counts."
    else:
        usage_status = "complete"
        usage_reason = "Prompt and completion token counts were returned by the provider."

    explicit_pricing = str(usage.get("pricing_status") or "")
    pricing_configured = usage.get("pricing_configured") is True
    if usage_status == "not_applicable":
        pricing_status = "not_applicable"
        pricing_reason = "No measured token usage requires a price calculation."
    elif usage_status == "unavailable":
        pricing_status = "unavailable"
        pricing_reason = "Pricing cannot be established without usage evidence."
    elif usage_status == "partial":
        pricing_status = "partial"
        pricing_reason = "Incomplete token evidence prevents an exact cost calculation."
    elif explicit_pricing in _PRICING_STATUSES:
        pricing_status = explicit_pricing
        pricing_reason = str(
            usage.get("pricing_reason")
            or "Pricing status supplied by the provider integration."
        )
    elif not input_present or not output_present:
        pricing_status = "unavailable"
        pricing_reason = "Pricing cannot be established without both token counters."
    elif estimated_cost > 0 or pricing_configured:
        pricing_status = "complete"
        pricing_reason = "Configured token pricing produced the recorded estimate."
    else:
        pricing_status = "unavailable"
        pricing_reason = "Token pricing was not configured; zero is not an exact cost."

    return {
        "model_calls": model_calls,
        "model_cache_hits": model_cache_hits,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": estimated_cost,
        "usage_status": usage_status,
        "usage_reason": usage_reason,
        "provider": provider,
        "pricing_status": pricing_status,
        "pricing_reason": pricing_reason,
    }


def _outcome_is_immutable_compatible(
    stored: dict[str, Any], incoming: dict[str, Any]
) -> bool:
    """Allow only append-only derived links after a terminal invocation."""

    for field in _INVOCATION_OUTCOME_FIELDS:
        old_value = stored.get(field)
        new_value = incoming.get(field)
        if field not in _INVOCATION_DERIVED_LINK_FIELDS:
            if old_value != new_value:
                return False
            continue
        old_links = [str(item) for item in old_value or []]
        new_links = [str(item) for item in new_value or []]
        if new_links[: len(old_links)] != old_links:
            return False
    return True


def _project_invocation_row(
    row: dict[str, Any],
) -> tuple[AgentInvocation, dict[str, Any]]:
    """Rebuild an invocation from normalized columns, never JSON identity fields."""
    issues: list[str] = []
    try:
        raw = json.loads(str(row.get("invocation_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        raw = {}
        issues.append("invocation_json is not valid JSON")
    if not isinstance(raw, dict):
        raw = {}
        issues.append("invocation_json is not an object")

    try:
        consumed = json.loads(str(row.get("consumed_handoff_ids_json") or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        consumed = []
        issues.append("normalized consumption list is invalid")
    if not isinstance(consumed, list) or any(
        not isinstance(item, str) for item in consumed
    ):
        consumed = []
        issues.append("normalized consumption list is not a string array")

    normalized_identity = _invocation_identity(
        {
            "run_id": row.get("run_id"),
            "trace_id": row.get("trace_id"),
            "operation_key": row.get("operation_key"),
            "agent_id": row.get("agent_id"),
            "role": row.get("role"),
            "operation": row.get("operation"),
            "attempt": row.get("attempt"),
            "started_at": row.get("started_at"),
            "input_type": row.get("input_type"),
            "execution_mode": row.get("execution_mode"),
            "replay_of_invocation_id": row.get("replay_of_invocation_id"),
            "parent_invocation_id": row.get("parent_invocation_id"),
            "previous_in_log_id": row.get("previous_in_log_id"),
            "consumed_handoff_message_ids": consumed,
        }
    )
    try:
        json_identity = _invocation_identity(raw)
    except (TypeError, ValueError):
        json_identity = {}
        issues.append("invocation_json identity has invalid scalar types")
    if json_identity != normalized_identity:
        issues.append("invocation_json identity disagrees with normalized columns")
    if str(raw.get("invocation_id") or "") != str(row.get("invocation_id") or ""):
        issues.append("invocation_json id disagrees with the durable row key")

    try:
        stored_outcome = json.loads(str(row.get("outcome_json") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        stored_outcome = {}
        issues.append("normalized invocation outcome is not valid JSON")
    if not isinstance(stored_outcome, dict):
        stored_outcome = {}
        issues.append("normalized invocation outcome is not an object")
    normalized_outcome = _invocation_outcome(stored_outcome)
    json_outcome = _invocation_outcome(raw)
    if json_outcome != normalized_outcome:
        issues.append("invocation_json outcome disagrees with normalized columns")
    stored_outcome_hash = str(row.get("outcome_hash") or "")
    recomputed_outcome_hash = _invocation_outcome_hash(normalized_outcome)
    if stored_outcome_hash != recomputed_outcome_hash:
        issues.append("normalized invocation outcome hash does not verify")

    recomputed_hash = _invocation_identity_hash(normalized_identity)
    stored_hash = str(row.get("identity_hash") or "")
    if stored_hash != recomputed_hash:
        issues.append("normalized identity hash does not verify")
    identity_version = str(row.get("identity_version") or "")
    if identity_version not in {
        INVOCATION_IDENTITY_VERSION,
        INVOCATION_IDENTITY_BACKFILL_VERSION,
    }:
        issues.append("identity version is missing or unsupported")

    # Identity and outcome columns are the durable source of truth.  The JSON
    # blob remains a projection that can be audited, but never controls reads.
    payload = (
        dict(normalized_outcome)
        if stored_outcome_hash == recomputed_outcome_hash
        else {}
    )
    payload.update(normalized_identity)
    payload.update(
        {
            "invocation_id": str(row.get("invocation_id") or ""),
            "status": str(row.get("status") or "failed"),
            "side_effect_status": str(
                row.get("side_effect_status") or "unknown"
            ),
        }
    )
    if not payload.get("status"):
        payload["status"] = str(row.get("status") or "failed")
    payload.setdefault("ended_at", None)
    if issues:
        provenance_status = "identity_mismatch"
        provenance_reason = "; ".join(issues)
    elif identity_version == INVOCATION_IDENTITY_BACKFILL_VERSION:
        provenance_status = "legacy_backfilled"
        provenance_reason = (
            "Identity was frozen during migration from a pre-v1 invocation record."
        )
    else:
        provenance_status = "store_consistent"
        provenance_reason = (
            "Normalized identity columns, identity hash, and JSON projection agree."
        )
    payload["provenance_status"] = provenance_status
    payload["provenance_reason"] = provenance_reason
    invocation = AgentInvocation(**payload)
    return invocation, {
        "status": provenance_status,
        "reason": provenance_reason,
        "issues": issues,
        "identity_version": identity_version or "unrecorded",
        "stored_identity_hash": stored_hash,
        "recomputed_identity_hash": recomputed_hash,
        "stored_outcome_hash": stored_outcome_hash,
        "recomputed_outcome_hash": recomputed_outcome_hash,
    }


def _parse_utc_timestamp(value: object, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as error:
        raise HandoffValidationError(f"{label} timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise HandoffValidationError(f"{label} timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _is_non_retryable_operation_error(value: object) -> bool:
    return str(value or "").startswith(_NON_RETRYABLE_OPERATION_PREFIXES)


def _attachment_from_row(row: Any) -> InputAttachment:
    values = list(row)
    return InputAttachment(
        id=str(values[0]),
        name=str(values[1]),
        media_type=str(values[2]),
        modality=str(values[3]),  # type: ignore[arg-type]
        sha256=str(values[4]),
        byte_length=int(values[5]),
        content_uri=str(values[6]),
        created_at=str(values[7]),
        status=str(values[8]),
        parser_version=str(values[9] or ""),
        error=str(values[10]) if values[10] is not None else None,
    )


class RunStore:
    def __init__(self, runs_dir: Path, run_id: str) -> None:
        self.run_id = validate_run_id(run_id)
        self._execution_owner_token: str | None = None
        self._execution_fence: int | None = None
        self._acquired_execution_leases: dict[str, tuple[str, int, int]] = {}
        self._operation_owner_token = uuid.uuid4().hex
        root = runs_dir.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.run_dir = (root / self.run_id).resolve()
        if self.run_dir.parent != root:
            raise ValueError("run_id escapes the configured runs directory")
        self.run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.run_dir, 0o700)
        self.events_path = self.run_dir / "events.jsonl"
        self.events_lock_path = self.run_dir / ".events.lock"
        self.database_path = self.run_dir / "checkpoints.sqlite"
        self._initialize()

    def _initialize(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    node TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    published_at TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_key TEXT PRIMARY KEY,
                    node TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'model',
                    idempotent INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    semantic_input_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    result_json TEXT,
                    error TEXT
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(operations)")
            }
            for name, definition in (
                ("kind", "TEXT NOT NULL DEFAULT 'model'"),
                ("idempotent", "INTEGER NOT NULL DEFAULT 0"),
                ("attempt_count", "INTEGER NOT NULL DEFAULT 1"),
                ("owner_token", "TEXT"),
                ("owner_fence", "INTEGER"),
                ("lease_expires_at_ms", "INTEGER"),
                ("original_invocation_id", "TEXT"),
                ("result_invocation_id", "TEXT"),
                ("last_invocation_id", "TEXT"),
                ("side_effect_status", "TEXT NOT NULL DEFAULT 'unknown'"),
            ):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE operations ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agent_invocations (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    invocation_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    operation_key TEXT,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    input_type TEXT NOT NULL,
                    parent_invocation_id TEXT,
                    previous_in_log_id TEXT,
                    consumed_handoff_ids_json TEXT NOT NULL,
                    identity_version TEXT NOT NULL,
                    identity_hash TEXT NOT NULL,
                    execution_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    replay_of_invocation_id TEXT,
                    side_effect_status TEXT NOT NULL,
                    outcome_json TEXT NOT NULL DEFAULT '{}',
                    outcome_hash TEXT NOT NULL DEFAULT '',
                    invocation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(operation_key) REFERENCES operations(operation_key),
                    FOREIGN KEY(replay_of_invocation_id)
                        REFERENCES agent_invocations(invocation_id)
                )
                """
            )
            invocation_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(agent_invocations)")
            }
            for name, definition in (
                ("role", "TEXT"),
                ("operation", "TEXT"),
                ("attempt", "INTEGER"),
                ("started_at", "TEXT"),
                ("input_type", "TEXT"),
                ("parent_invocation_id", "TEXT"),
                ("previous_in_log_id", "TEXT"),
                ("consumed_handoff_ids_json", "TEXT"),
                ("identity_version", "TEXT"),
                ("identity_hash", "TEXT"),
                ("outcome_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("outcome_hash", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in invocation_columns:
                    connection.execute(
                        f"ALTER TABLE agent_invocations ADD COLUMN {name} {definition}"
                    )
            self._backfill_invocation_identity(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS agent_invocations_operation_idx
                ON agent_invocations(operation_key, sequence)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_messages (
                    message_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    producer TEXT NOT NULL,
                    producer_invocation_id TEXT NOT NULL,
                    intended_consumer TEXT NOT NULL,
                    route_target TEXT NOT NULL,
                    envelope_json TEXT NOT NULL,
                    checkpoint_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(producer_invocation_id)
                        REFERENCES agent_invocations(invocation_id),
                    FOREIGN KEY(checkpoint_id) REFERENCES checkpoints(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_consumptions (
                    message_id TEXT NOT NULL,
                    consumer_invocation_id TEXT NOT NULL,
                    consumer_agent_id TEXT NOT NULL,
                    consumer_operation TEXT NOT NULL,
                    source_producer_invocation_id TEXT NOT NULL,
                    binding_status TEXT NOT NULL,
                    consumer_attempt INTEGER NOT NULL DEFAULT 0,
                    consumption_fence INTEGER NOT NULL DEFAULT 0,
                    superseded_by_invocation_id TEXT,
                    superseded_at TEXT,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY(message_id, consumer_invocation_id),
                    FOREIGN KEY(message_id) REFERENCES handoff_messages(message_id),
                    FOREIGN KEY(consumer_invocation_id)
                        REFERENCES agent_invocations(invocation_id),
                    FOREIGN KEY(source_producer_invocation_id)
                        REFERENCES agent_invocations(invocation_id),
                    FOREIGN KEY(superseded_by_invocation_id)
                        REFERENCES agent_invocations(invocation_id)
                )
                """
            )
            consumption_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(handoff_consumptions)"
                )
            }
            for name, definition in (
                ("consumer_attempt", "INTEGER NOT NULL DEFAULT 0"),
                ("consumption_fence", "INTEGER NOT NULL DEFAULT 0"),
                ("superseded_by_invocation_id", "TEXT"),
                ("superseded_at", "TEXT"),
            ):
                if name not in consumption_columns:
                    connection.execute(
                        f"ALTER TABLE handoff_consumptions ADD COLUMN {name} {definition}"
                    )
            self._backfill_handoff_consumptions(connection)
            self._migrate_handoff_consumption_fences(connection)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS handoff_active_consumption_idx
                ON handoff_consumptions(message_id)
                WHERE superseded_by_invocation_id IS NULL
                  AND binding_status IN (
                      'pending_receipt', 'retry_pending_receipt',
                      'replay_pending_receipt', 'server_validated'
                  )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_receipts (
                    message_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    consumed_by_invocation_id TEXT NOT NULL,
                    consumed_by_agent_id TEXT NOT NULL,
                    consumed_by_operation TEXT,
                    consumed_from_producer_invocation_id TEXT,
                    receipt_json TEXT NOT NULL,
                    consumed_at TEXT NOT NULL,
                    validation_status TEXT NOT NULL DEFAULT 'server_validated',
                    validation_json TEXT NOT NULL DEFAULT '{}',
                    validated_at TEXT,
                    FOREIGN KEY(message_id) REFERENCES handoff_messages(message_id),
                    FOREIGN KEY(consumed_by_invocation_id)
                        REFERENCES agent_invocations(invocation_id)
                )
                """
            )
            receipt_audit_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(handoff_receipts)")
            }
            for name, definition in (
                ("consumed_by_operation", "TEXT"),
                ("consumed_from_producer_invocation_id", "TEXT"),
                ("validation_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
                ("validation_json", "TEXT NOT NULL DEFAULT '{}'"),
                ("validated_at", "TEXT"),
            ):
                if name not in receipt_audit_columns:
                    connection.execute(
                        f"ALTER TABLE handoff_receipts ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS handoff_receipt_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id TEXT,
                    run_id TEXT,
                    trace_id TEXT,
                    consumed_by_invocation_id TEXT,
                    consumed_by_agent_id TEXT,
                    reason TEXT NOT NULL,
                    receipt_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS artifact_manifests (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    metadata_hash TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    producer_invocation_id TEXT,
                    handoff_message_id TEXT,
                    parent_artifact_id TEXT,
                    checkpoint_id INTEGER,
                    manifest_valid INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(producer_invocation_id)
                        REFERENCES agent_invocations(invocation_id),
                    FOREIGN KEY(parent_artifact_id)
                        REFERENCES artifact_manifests(artifact_id),
                    FOREIGN KEY(checkpoint_id) REFERENCES checkpoints(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_fetches (
                    fetch_record_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    requested_url TEXT NOT NULL,
                    canonical_requested_url TEXT NOT NULL DEFAULT '',
                    final_url TEXT,
                    operation_key TEXT NOT NULL,
                    invocation_id TEXT NOT NULL,
                    result_invocation_id TEXT,
                    execution_mode TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    fetch_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    content_hash TEXT,
                    content_hash_scope TEXT NOT NULL DEFAULT 'unknown',
                    snapshot_sha256 TEXT,
                    error TEXT,
                    fetched_at TEXT,
                    binding_version TEXT NOT NULL DEFAULT '',
                    binding_json TEXT NOT NULL DEFAULT '',
                    binding_digest TEXT NOT NULL DEFAULT '',
                    binding_status TEXT NOT NULL DEFAULT 'legacy_unverified',
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(operation_key) REFERENCES operations(operation_key),
                    FOREIGN KEY(invocation_id)
                        REFERENCES agent_invocations(invocation_id),
                    FOREIGN KEY(result_invocation_id)
                        REFERENCES agent_invocations(invocation_id)
                )
                """
            )
            source_fetch_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(source_fetches)")
            }
            for name, definition in (
                ("canonical_requested_url", "TEXT NOT NULL DEFAULT ''"),
                ("content_hash_scope", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("binding_version", "TEXT NOT NULL DEFAULT ''"),
                ("binding_json", "TEXT NOT NULL DEFAULT ''"),
                ("binding_digest", "TEXT NOT NULL DEFAULT ''"),
                (
                    "binding_status",
                    "TEXT NOT NULL DEFAULT 'legacy_unverified'",
                ),
            ):
                if name not in source_fetch_columns:
                    connection.execute(
                        f"ALTER TABLE source_fetches ADD COLUMN {name} {definition}"
                    )
            # A pre-binding database may contain rows that need a one-time
            # migration marker.  Recreate the append-only trigger after that
            # marker is applied; new writes can never update a fetch record.
            connection.execute("DROP TRIGGER IF EXISTS source_fetch_binding_immutable")
            self._mark_legacy_source_fetch_bindings(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS source_fetches_source_idx
                ON source_fetches(source_id, recorded_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS input_attachments (
                    attachment_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    media_type TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    byte_length INTEGER NOT NULL,
                    content_uri TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    parser_version TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'stored',
                    error TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS input_attachments_run_idx
                ON input_attachments(run_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS source_fetch_binding_immutable
                BEFORE UPDATE ON source_fetches
                BEGIN
                    SELECT RAISE(ABORT, 'source fetch records are immutable');
                END
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_ledger (
                    operation_key TEXT PRIMARY KEY,
                    model_calls INTEGER NOT NULL,
                    model_cache_hits INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    usage_status TEXT NOT NULL DEFAULT 'unavailable',
                    reason TEXT NOT NULL DEFAULT '',
                    usage_reason TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    pricing_status TEXT NOT NULL DEFAULT 'unavailable',
                    pricing_reason TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(operation_key) REFERENCES operations(operation_key)
                )
                """
            )
            # A model response can contain billable usage before the enclosing
            # operation has finished serialization and checkpointing.  Keep
            # that settlement append-only and keyed by operation attempt so a
            # confirmed retry cannot overwrite an earlier paid response.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_settlements (
                    operation_key TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    model_calls INTEGER NOT NULL,
                    model_cache_hits INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    usage_status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    pricing_status TEXT NOT NULL DEFAULT 'unavailable',
                    pricing_reason TEXT NOT NULL DEFAULT '',
                    settled_at TEXT NOT NULL,
                    PRIMARY KEY(operation_key, attempt_count),
                    FOREIGN KEY(operation_key) REFERENCES operations(operation_key)
                )
                """
            )
            # A single agent operation can make more than one model request
            # (for example, one perception request per uploaded attachment).
            # Keep each returned provider usage record so the live UI does not
            # wait for the whole operation to finish before showing its cost.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_settlement_events (
                    operation_key TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    settlement_index INTEGER NOT NULL,
                    model_calls INTEGER NOT NULL,
                    model_cache_hits INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    estimated_cost_usd REAL NOT NULL,
                    usage_status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT 'unknown',
                    pricing_status TEXT NOT NULL DEFAULT 'unavailable',
                    pricing_reason TEXT NOT NULL DEFAULT '',
                    settled_at TEXT NOT NULL,
                    PRIMARY KEY(operation_key, attempt_count, settlement_index),
                    FOREIGN KEY(operation_key) REFERENCES operations(operation_key)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS usage_settlements_settled_at_idx
                ON usage_settlements(settled_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS usage_settlement_events_settled_at_idx
                ON usage_settlement_events(settled_at)
                """
            )
            usage_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(usage_ledger)")
            }
            for name, definition in (
                ("usage_status", "TEXT NOT NULL DEFAULT 'partial'"),
                (
                    "reason",
                    "TEXT NOT NULL DEFAULT 'Legacy usage row lacks per-operation evidence.'",
                ),
                (
                    "usage_reason",
                    "TEXT NOT NULL DEFAULT 'Legacy usage row lacks per-operation evidence.'",
                ),
                ("provider", "TEXT NOT NULL DEFAULT 'legacy_unknown'"),
                ("pricing_status", "TEXT NOT NULL DEFAULT 'unavailable'"),
                (
                    "pricing_reason",
                    "TEXT NOT NULL DEFAULT 'Legacy usage row has no pricing evidence.'",
                ),
            ):
                if name not in usage_columns:
                    connection.execute(
                        f"ALTER TABLE usage_ledger ADD COLUMN {name} {definition}"
                    )
            if "reason" not in usage_columns:
                connection.execute(
                    """
                    UPDATE usage_ledger
                    SET reason = usage_reason
                    WHERE usage_reason IS NOT NULL AND usage_reason != ''
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_interrupts (
                    interrupt_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    protocol_run_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    response_schema_json TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT,
                    resume_receipt_id TEXT
                )
                """
            )
            interrupt_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(agui_interrupts)")
            }
            if "response_schema_json" not in interrupt_columns:
                connection.execute(
                    "ALTER TABLE agui_interrupts ADD COLUMN response_schema_json TEXT"
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resume_receipts (
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
                    durable_run_status TEXT,
                    execution_started_at TEXT,
                    execution_completed_at TEXT,
                    execution_error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS agui_message_snapshots (
                    thread_id TEXT PRIMARY KEY,
                    messages_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS resume_execution_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    transition_key TEXT,
                    transition_kind TEXT NOT NULL DEFAULT 'execution',
                    idempotency_key TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    owner_fence INTEGER,
                    owner_token_fingerprint TEXT,
                    handoff_message_id TEXT,
                    agent_invocation_id TEXT,
                    agent_id TEXT,
                    operation TEXT,
                    superseded_handoff_message_id TEXT,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(idempotency_key)
                        REFERENCES resume_receipts(idempotency_key)
                )
                """
            )
            transition_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(resume_execution_transitions)"
                )
            }
            for name, definition in (
                ("transition_key", "TEXT"),
                ("transition_kind", "TEXT NOT NULL DEFAULT 'execution'"),
                ("handoff_message_id", "TEXT"),
                ("agent_invocation_id", "TEXT"),
                ("agent_id", "TEXT"),
                ("operation", "TEXT"),
                ("superseded_handoff_message_id", "TEXT"),
            ):
                if name not in transition_columns:
                    connection.execute(
                        f"ALTER TABLE resume_execution_transitions "
                        f"ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS resume_transition_key_idx
                ON resume_execution_transitions(transition_key)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_leases (
                    run_id TEXT PRIMARY KEY,
                    owner_token TEXT NOT NULL,
                    receipt_id TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    acquired_at_ms INTEGER NOT NULL,
                    heartbeat_at_ms INTEGER NOT NULL,
                    expires_at_ms INTEGER NOT NULL
                )
                """
            )
            receipt_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(resume_receipts)")
            }
            if "parent_protocol_run_id" not in receipt_columns:
                connection.execute(
                    "ALTER TABLE resume_receipts ADD COLUMN parent_protocol_run_id TEXT"
                )
            for name, definition in (
                ("claim_owner_token", "TEXT"),
                ("claim_fence", "INTEGER NOT NULL DEFAULT 0"),
                ("claim_expires_at_ms", "INTEGER"),
                ("execution_status", "TEXT NOT NULL DEFAULT 'legacy_unverified'"),
                ("durable_run_status", "TEXT"),
                ("execution_started_at", "TEXT"),
                ("execution_completed_at", "TEXT"),
                ("execution_error", "TEXT"),
            ):
                if name not in receipt_columns:
                    connection.execute(
                        f"ALTER TABLE resume_receipts ADD COLUMN {name} {definition}"
                    )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS resume_claim_metadata_required
                BEFORE UPDATE OF execution_claimed, claim_owner_token,
                                 claim_fence, claim_expires_at_ms
                ON resume_receipts
                WHEN
                    (NEW.execution_claimed = 1 AND (
                        NEW.claim_owner_token IS NULL OR
                        NEW.claim_owner_token = '' OR
                        NEW.claim_fence <= 0 OR
                        NEW.claim_expires_at_ms IS NULL
                    )) OR
                    (NEW.execution_claimed = 0 AND (
                        NEW.claim_owner_token IS NOT NULL OR
                        NEW.claim_fence != 0 OR
                        NEW.claim_expires_at_ms IS NOT NULL
                    ))
                BEGIN
                    SELECT RAISE(ABORT, 'resume execution claim metadata is inconsistent');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS agent_invocation_identity_immutable
                BEFORE UPDATE OF
                    run_id, trace_id, operation_key, agent_id, role, operation,
                    attempt, started_at, input_type, parent_invocation_id,
                    previous_in_log_id, consumed_handoff_ids_json,
                    identity_version, identity_hash, execution_mode,
                    replay_of_invocation_id, created_at
                ON agent_invocations
                WHEN
                    OLD.run_id IS NOT NEW.run_id OR
                    OLD.trace_id IS NOT NEW.trace_id OR
                    OLD.operation_key IS NOT NEW.operation_key OR
                    OLD.agent_id IS NOT NEW.agent_id OR
                    OLD.role IS NOT NEW.role OR
                    OLD.operation IS NOT NEW.operation OR
                    OLD.attempt IS NOT NEW.attempt OR
                    OLD.started_at IS NOT NEW.started_at OR
                    OLD.input_type IS NOT NEW.input_type OR
                    OLD.parent_invocation_id IS NOT NEW.parent_invocation_id OR
                    OLD.previous_in_log_id IS NOT NEW.previous_in_log_id OR
                    OLD.consumed_handoff_ids_json IS NOT NEW.consumed_handoff_ids_json OR
                    OLD.identity_version IS NOT NEW.identity_version OR
                    OLD.identity_hash IS NOT NEW.identity_hash OR
                    OLD.execution_mode IS NOT NEW.execution_mode OR
                    OLD.replay_of_invocation_id IS NOT NEW.replay_of_invocation_id OR
                    OLD.created_at IS NOT NEW.created_at
                BEGIN
                    SELECT RAISE(ABORT, 'agent invocation identity is immutable');
                END
                """
            )
            connection.commit()
        os.chmod(self.database_path, 0o600)
        self._register_legacy_artifacts()
        self._flush_outbox()

    def _backfill_invocation_identity(self, connection: sqlite3.Connection) -> None:
        """Freeze provenance columns for databases created before v1 identity storage."""
        rows = connection.execute(
            """
            SELECT invocation_id, run_id, trace_id, operation_key, agent_id,
                   execution_mode, replay_of_invocation_id, invocation_json,
                   created_at, role, operation, attempt, started_at, input_type,
                   parent_invocation_id, previous_in_log_id,
                   consumed_handoff_ids_json, identity_version, identity_hash
            FROM agent_invocations ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            try:
                raw = json.loads(str(row[7]))
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            consumed = raw.get("consumed_handoff_message_ids") or []
            if not isinstance(consumed, list):
                consumed = []
            identity = _invocation_identity(
                {
                    "run_id": row[1] or raw.get("run_id") or self.run_id,
                    "trace_id": row[2] or raw.get("trace_id") or self.run_id,
                    "operation_key": row[3] if row[3] is not None else raw.get("operation_key"),
                    "agent_id": row[4] or raw.get("agent_id") or "unknown",
                    "role": row[9] or raw.get("role") or "unknown",
                    "operation": row[10] or raw.get("operation") or "unknown",
                    "attempt": row[11] if row[11] is not None else raw.get("attempt", 0),
                    "started_at": row[12] or row[8] or raw.get("started_at") or "",
                    "input_type": row[13] or raw.get("input_type") or "",
                    "execution_mode": row[5] or raw.get("execution_mode") or "executed",
                    "replay_of_invocation_id": (
                        row[6]
                        if row[6] is not None
                        else raw.get("replay_of_invocation_id")
                    ),
                    "parent_invocation_id": (
                        row[14]
                        if row[14] is not None
                        else raw.get("parent_invocation_id")
                    ),
                    "previous_in_log_id": (
                        row[15]
                        if row[15] is not None
                        else raw.get("previous_in_log_id")
                    ),
                    "consumed_handoff_message_ids": consumed,
                }
            )
            consumed_json = json.dumps(
                identity["consumed_handoff_message_ids"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            outcome = _invocation_outcome(raw)
            if not outcome.get("status"):
                outcome["status"] = str(raw.get("status") or "failed")
            outcome_json = json.dumps(
                outcome,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            # Do not rewrite an already frozen identity. Existing pre-v1 rows
            # receive a one-time backfill and are explicitly labelled in reads.
            if row[17] and row[18] and all(
                value is not None
                for value in (row[9], row[10], row[11], row[12], row[13], row[16])
            ):
                connection.execute(
                    """
                    UPDATE agent_invocations
                    SET outcome_json = ?, outcome_hash = ?
                    WHERE invocation_id = ? AND (outcome_hash IS NULL OR outcome_hash = '')
                    """,
                    (outcome_json, _invocation_outcome_hash(outcome), row[0]),
                )
                continue
            connection.execute(
                """
                UPDATE agent_invocations
                SET role = ?, operation = ?, attempt = ?, started_at = ?,
                    input_type = ?, parent_invocation_id = ?,
                    previous_in_log_id = ?, consumed_handoff_ids_json = ?,
                    identity_version = ?, identity_hash = ?
                WHERE invocation_id = ?
                """,
                (
                    identity["role"],
                    identity["operation"],
                    identity["attempt"],
                    identity["started_at"],
                    identity["input_type"],
                    identity["parent_invocation_id"],
                    identity["previous_in_log_id"],
                    consumed_json,
                    INVOCATION_IDENTITY_BACKFILL_VERSION,
                    _invocation_identity_hash(identity),
                    row[0],
                ),
            )
            connection.execute(
                """
                UPDATE agent_invocations
                SET outcome_json = ?, outcome_hash = ?
                WHERE invocation_id = ? AND (outcome_hash IS NULL OR outcome_hash = '')
                """,
                (outcome_json, _invocation_outcome_hash(outcome), row[0]),
            )

    def _backfill_handoff_consumptions(self, connection: sqlite3.Connection) -> None:
        """Recover explicit consumption edges without treating JSON as future truth."""
        rows = connection.execute(
            """
            SELECT invocation_id, run_id, agent_id, operation,
                   consumed_handoff_ids_json, created_at
            FROM agent_invocations
            WHERE consumed_handoff_ids_json IS NOT NULL
              AND consumed_handoff_ids_json != '[]'
            """
        ).fetchall()
        for invocation_id, run_id, agent_id, operation, raw_ids, recorded_at in rows:
            try:
                message_ids = json.loads(str(raw_ids))
            except (TypeError, ValueError, json.JSONDecodeError):
                message_ids = []
            if not isinstance(message_ids, list):
                continue
            for message_id in message_ids:
                source = connection.execute(
                    """
                    SELECT producer_invocation_id
                    FROM handoff_messages
                    WHERE message_id = ? AND run_id = ?
                    """,
                    (str(message_id), str(run_id)),
                ).fetchone()
                if source is None:
                    continue
                connection.execute(
                    """
                    INSERT OR IGNORE INTO handoff_consumptions(
                        message_id, consumer_invocation_id, consumer_agent_id,
                        consumer_operation, source_producer_invocation_id,
                        binding_status, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, 'legacy_backfilled', ?)
                    """,
                    (
                        str(message_id),
                        str(invocation_id),
                        str(agent_id),
                        str(operation),
                        str(source[0]),
                        str(recorded_at),
                    ),
                  )

    def _migrate_handoff_consumption_fences(
        self, connection: sqlite3.Connection
    ) -> None:
        """Assign durable retry fences without promoting legacy provenance."""

        rows = connection.execute(
            """
            SELECT hc.message_id, hc.consumer_invocation_id, hc.binding_status,
                   hc.consumer_attempt, hc.consumption_fence,
                   hc.superseded_by_invocation_id, hc.recorded_at,
                   COALESCE(ai.attempt, 0)
            FROM handoff_consumptions AS hc
            LEFT JOIN agent_invocations AS ai
              ON ai.invocation_id = hc.consumer_invocation_id
            ORDER BY hc.message_id, hc.recorded_at, hc.consumer_invocation_id
            """
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row | tuple[Any, ...]]] = {}
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)

        for message_id, candidates in grouped.items():
            modern = [row for row in candidates if str(row[2]) != "legacy_backfilled"]
            used_fences = {
                int(row[4]) for row in modern if int(row[4] or 0) > 0
            }
            next_fence = max(used_fences, default=0)
            assigned_fences: set[int] = set()
            for row in candidates:
                attempt = int(row[7] or row[3] or 0)
                if str(row[2]) == "legacy_backfilled":
                    if int(row[3] or 0) != attempt:
                        connection.execute(
                            """
                            UPDATE handoff_consumptions
                            SET consumer_attempt = ?
                            WHERE message_id = ? AND consumer_invocation_id = ?
                            """,
                            (attempt, message_id, str(row[1])),
                        )
                    continue
                fence = int(row[4] or 0)
                if fence <= 0 or fence in assigned_fences:
                    next_fence += 1
                    fence = next_fence
                assigned_fences.add(fence)
                connection.execute(
                    """
                    UPDATE handoff_consumptions
                    SET consumer_attempt = ?, consumption_fence = ?
                    WHERE message_id = ? AND consumer_invocation_id = ?
                    """,
                    (attempt, fence, message_id, str(row[1])),
                )

            active = [
                row
                for row in candidates
                if str(row[2]) in _HANDOFF_PENDING_BINDING_STATUSES
                or str(row[2]) == "server_validated"
            ]
            active = [row for row in active if not str(row[5] or "")]
            if len(active) <= 1:
                continue
            latest = max(
                active,
                key=lambda row: (
                    int(row[7] or row[3] or 0),
                    int(row[4] or 0),
                    str(row[6] or ""),
                    str(row[1]),
                ),
            )
            latest_id = str(latest[1])
            now = datetime.now(UTC).isoformat()
            for row in active:
                if str(row[1]) == latest_id:
                    continue
                # A legacy row remains legacy even when a modern retry makes
                # it obsolete.  It is never rewritten as server_validated.
                new_status = (
                    "superseded"
                    if str(row[2]) in _HANDOFF_PENDING_BINDING_STATUSES
                    or str(row[2]) == "server_validated"
                    else str(row[2])
                )
                connection.execute(
                    """
                    UPDATE handoff_consumptions
                    SET binding_status = ?, superseded_by_invocation_id = ?,
                        superseded_at = ?
                    WHERE message_id = ? AND consumer_invocation_id = ?
                    """,
                    (new_status, latest_id, now, message_id, str(row[1])),
                )

    @staticmethod
    def _source_fetch_binding_is_proven(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> bool:
        """Revalidate a stored source binding before retaining server trust."""
        if (
            str(row["binding_status"] or "") != "server_bound"
            or str(row["binding_version"] or "") != SOURCE_FETCH_BINDING_VERSION
        ):
            return False
        try:
            binding = json.loads(str(row["binding_json"] or ""))
            if not isinstance(binding, dict):
                return False
            if str(row["binding_digest"] or "") != _source_fetch_binding_digest(
                binding
            ):
                return False
            invocation_row = connection.execute(
                "SELECT * FROM agent_invocations WHERE invocation_id = ?",
                (str(row["invocation_id"]),),
            ).fetchone()
            operation = connection.execute(
                """
                SELECT operation_key, kind, node, semantic_input_hash,
                       status, result_invocation_id
                FROM operations WHERE operation_key = ?
                """,
                (str(row["operation_key"]),),
            ).fetchone()
            if invocation_row is None or operation is None:
                return False
            invocation, validation = _project_invocation_row(dict(invocation_row))
            if (
                validation["stored_identity_hash"]
                != validation["recomputed_identity_hash"]
                or validation["stored_outcome_hash"]
                != validation["recomputed_outcome_hash"]
                or validation["identity_version"] != INVOCATION_IDENTITY_VERSION
            ):
                return False
            if (
                invocation.run_id != str(row["run_id"])
                or invocation.trace_id != str(row["run_id"])
                or invocation.operation_key != str(row["operation_key"])
                or invocation.operation != "fetch"
                or invocation.execution_mode != str(row["execution_mode"])
                or int(invocation.attempt) != int(row["attempt"])
            ):
                return False
            result_invocation_id = str(operation["result_invocation_id"] or "") or None
            stored_result_id = str(row["result_invocation_id"] or "") or None
            if stored_result_id != result_invocation_id:
                return False
            semantic_invocation = invocation
            if invocation.execution_mode == "replayed":
                if not invocation.replay_of_invocation_id or str(
                    invocation.replay_of_invocation_id
                ) != str(result_invocation_id or ""):
                    return False
                semantic_row = connection.execute(
                    "SELECT * FROM agent_invocations WHERE invocation_id = ?",
                    (str(result_invocation_id or ""),),
                ).fetchone()
                if semantic_row is None:
                    return False
                semantic_invocation, semantic_validation = _project_invocation_row(
                    dict(semantic_row)
                )
                if (
                    semantic_validation["stored_identity_hash"]
                    != semantic_validation["recomputed_identity_hash"]
                    or semantic_validation["stored_outcome_hash"]
                    != semantic_validation["recomputed_outcome_hash"]
                    or semantic_validation["identity_version"]
                    != INVOCATION_IDENTITY_VERSION
                    or semantic_invocation.execution_mode != "executed"
                ):
                    return False
            parsed_input = json.loads(semantic_invocation.input_summary)
            if not isinstance(parsed_input, dict):
                return False
            if hashlib.sha256(
                _canonical_json(parsed_input).encode("utf-8")
            ).hexdigest() != str(operation["semantic_input_hash"]):
                return False
            if any(
                field not in parsed_input
                or parsed_input[field] is None
                or parsed_input[field] == ""
                for field in ("source_id", "requested_url", "provider")
            ):
                return False
            canonical_url = _canonical_source_fetch_url(
                str(parsed_input["requested_url"])
            )
            expected = {
                "run_id": str(row["run_id"]),
                "source_id": str(row["source_id"]),
                "canonical_requested_url": str(row["canonical_requested_url"]),
                "operation_key": str(row["operation_key"]),
                "invocation_id": str(row["invocation_id"]),
                "semantic_invocation_id": semantic_invocation.invocation_id,
                "result_invocation_id": result_invocation_id,
                "execution_mode": str(row["execution_mode"]),
                "provider": str(row["provider"]),
                "fetch_mode": str(row["fetch_mode"]),
                "status": str(row["status"]),
                "attempt": int(row["attempt"]),
                "content_hash": str(row["content_hash"] or "") or None,
                "content_hash_scope": str(row["content_hash_scope"] or "unknown"),
                "snapshot_sha256": str(row["snapshot_sha256"] or "") or None,
            }
            if any(
                str(binding.get(key)) != str(value)
                for key, value in expected.items()
            ):
                return False
            if (
                str(parsed_input["source_id"]) != str(row["source_id"])
                or canonical_url != str(row["canonical_requested_url"])
                or str(parsed_input["provider"]) != str(row["provider"])
            ):
                return False
            return not binding.get("missing_semantic_fields")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return False

    def _mark_legacy_source_fetch_bindings(
        self, connection: sqlite3.Connection
    ) -> None:
        """Label old fetch rows; missing invocation input cannot be inferred safely."""

        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT * FROM source_fetches").fetchall()
        for row in rows:
            if self._source_fetch_binding_is_proven(connection, row):
                continue
            canonical = str(row["canonical_requested_url"] or "")
            if not canonical:
                try:
                    canonical = _canonical_source_fetch_url(str(row["requested_url"]))
                except ValueError:
                    canonical = str(row["requested_url"])
            if (
                str(row["canonical_requested_url"] or "") != canonical
                or str(row["binding_status"] or "") != "legacy_unverified"
            ):
                connection.execute(
                    """
                    UPDATE source_fetches
                    SET canonical_requested_url = ?, binding_status = 'legacy_unverified'
                    WHERE fetch_record_id = ?
                    """,
                    (canonical, str(row["fetch_record_id"])),
                )

    def _register_legacy_artifacts(self) -> None:
        """Backfill verified pre-manifest files without blessing new crash orphans."""
        directory = self.run_dir / "artifacts"
        if not directory.exists():
            return
        candidates: list[tuple[ArtifactRef, str]] = []
        for metadata_path in sorted(directory.glob("A*.meta.json")):
            artifact_id = metadata_path.name.removesuffix(".meta.json")
            path = directory / f"{artifact_id}.json"
            if not path.exists():
                continue
            try:
                raw = json.loads(metadata_path.read_text(encoding="utf-8"))
                if any(
                    raw.get(key)
                    for key in (
                        "metadata_hash",
                        "producer_invocation_id",
                        "handoff_message_id",
                        "parent_artifact_id",
                    )
                ):
                    continue
                artifact = ArtifactRef(**raw)
                content = path.read_bytes()
                parsed = json.loads(content.decode("utf-8"))
                if (
                    artifact.artifact_id != artifact_id
                    or artifact.content_uri != f"artifacts/{artifact_id}.json"
                    or artifact.media_type != "application/json"
                    or artifact.canonicalization != "json-sort-keys-utf8-v1"
                    or hashlib.sha256(content).hexdigest() != artifact.checksum
                    or (
                        artifact.byte_length is not None
                        and artifact.byte_length != len(content)
                    )
                    or not isinstance(parsed, dict)
                    or canonical_artifact_bytes(parsed) != content
                ):
                    continue
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            candidates.append(
                (
                    artifact,
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            )
        if not candidates:
            return
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for artifact, metadata_json in candidates:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO artifact_manifests(
                        artifact_id, run_id, checksum, metadata_hash,
                        metadata_json, producer_invocation_id,
                        handoff_message_id, parent_artifact_id,
                        checkpoint_id, manifest_valid, status, created_at
                    ) VALUES (?, ?, ?, '', ?, NULL, NULL, NULL,
                              NULL, 0, 'legacy_verified', ?)
                    """,
                    (
                        artifact.artifact_id,
                        self.run_id,
                        artifact.checksum,
                        metadata_json,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            connection.commit()

    def bind_execution_fence(self, owner_token: str, fence: int) -> None:
        self._execution_owner_token = owner_token
        self._execution_fence = int(fence)

    def save_invocation(
        self,
        invocation: AgentInvocation,
        *,
        operation_key: str | None = None,
    ) -> None:
        """Insert or advance one durable invocation without changing its identity."""
        if invocation.run_id and invocation.run_id != self.run_id:
            raise ValueError("invocation belongs to a different run")
        if invocation.trace_id and invocation.trace_id != self.run_id:
            raise ValueError("invocation trace does not match the run")
        invocation.run_id = self.run_id
        invocation.trace_id = self.run_id
        if operation_key is not None:
            if invocation.operation_key not in {None, operation_key}:
                raise ValueError("invocation operation key cannot be changed")
            invocation.operation_key = operation_key
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            self._save_invocation(connection, invocation)
            connection.commit()

    # Compatibility alias used by integrations that name this boundary explicitly.
    persist_invocation = save_invocation

    def _save_invocation(
        self,
        connection: sqlite3.Connection,
        invocation: AgentInvocation,
    ) -> None:
        existing = connection.execute(
            """
            SELECT run_id, trace_id, operation_key, execution_mode,
                   replay_of_invocation_id, status, agent_id, role, operation,
                   attempt, started_at, input_type, parent_invocation_id,
                   previous_in_log_id, consumed_handoff_ids_json,
                   identity_version, identity_hash, created_at,
                   outcome_json, outcome_hash
            FROM agent_invocations WHERE invocation_id = ?
            """,
            (invocation.invocation_id,),
        ).fetchone()
        identity = _invocation_identity(asdict(invocation))
        identity_hash = _invocation_identity_hash(identity)
        outcome = _invocation_outcome(asdict(invocation))
        outcome_json = json.dumps(
            outcome,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        outcome_hash = _invocation_outcome_hash(outcome)
        consumed_json = json.dumps(
            identity["consumed_handoff_message_ids"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if existing is not None:
            try:
                stored_consumed = json.loads(existing[14] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("stored invocation consumption identity is invalid") from error
            stored_identity = {
                "run_id": existing[0],
                "trace_id": existing[1],
                "operation_key": existing[2],
                "agent_id": existing[6],
                "role": existing[7],
                "operation": existing[8],
                "attempt": int(existing[9] or 0),
                "started_at": existing[10],
                "input_type": existing[11],
                "execution_mode": existing[3],
                "replay_of_invocation_id": existing[4],
                "parent_invocation_id": existing[12],
                "previous_in_log_id": existing[13],
                "consumed_handoff_message_ids": stored_consumed,
            }
            if stored_identity != identity:
                raise ValueError("durable invocation identity fields cannot be changed")
            if existing[16] and str(existing[16]) != identity_hash:
                raise ValueError("durable invocation identity hash is inconsistent")
            if existing[15] and str(existing[15]) not in {
                INVOCATION_IDENTITY_VERSION,
                INVOCATION_IDENTITY_BACKFILL_VERSION,
            }:
                raise ValueError("durable invocation identity version is unsupported")
            try:
                stored_outcome = json.loads(str(existing[18] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("stored invocation outcome is invalid") from error
            if not isinstance(stored_outcome, dict):
                raise ValueError("stored invocation outcome is not an object")
            stored_outcome = _invocation_outcome(stored_outcome)
            if existing[19] and str(existing[19]) != _invocation_outcome_hash(
                stored_outcome
            ):
                raise ValueError("durable invocation outcome hash is inconsistent")
            if existing[5] in {"succeeded", "failed", "cancelled"}:
                if invocation.status != existing[5]:
                    raise ValueError("terminal invocation outcome is immutable")
                if not _outcome_is_immutable_compatible(stored_outcome, outcome):
                    raise ValueError("terminal invocation outcome is immutable")
        if existing is not None and existing[5] in {
            "succeeded",
            "failed",
            "cancelled",
        } and invocation.status == "running":
            raise ValueError("terminal invocation cannot return to running")

        invocation.provenance_status = (
            "legacy_backfilled"
            if existing is not None
            and existing[15] == INVOCATION_IDENTITY_BACKFILL_VERSION
            else "store_consistent"
        )
        invocation.provenance_reason = (
            "Identity was backfilled from a pre-v1 invocation record."
            if invocation.provenance_status == "legacy_backfilled"
            else "Normalized identity columns and the JSON projection agree."
        )
        now = datetime.now(UTC).isoformat()
        serialized = json.dumps(asdict(invocation), ensure_ascii=False, sort_keys=True)
        if existing is None:
            connection.execute(
                """
                INSERT INTO agent_invocations(
                    invocation_id, run_id, trace_id, operation_key, agent_id,
                    role, operation, attempt, started_at, input_type,
                    parent_invocation_id,
                    previous_in_log_id, consumed_handoff_ids_json,
                    identity_version, identity_hash, execution_mode, status,
                    replay_of_invocation_id, side_effect_status, outcome_json,
                    outcome_hash, invocation_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    invocation.invocation_id,
                    invocation.run_id,
                    invocation.trace_id,
                    invocation.operation_key,
                    invocation.agent_id,
                    invocation.role,
                    invocation.operation,
                    invocation.attempt,
                    invocation.started_at,
                    invocation.input_type,
                    invocation.parent_invocation_id,
                    invocation.previous_in_log_id,
                    consumed_json,
                    INVOCATION_IDENTITY_VERSION,
                    identity_hash,
                    invocation.execution_mode,
                    invocation.status,
                    invocation.replay_of_invocation_id,
                    invocation.side_effect_status,
                    outcome_json,
                    outcome_hash,
                    serialized,
                    invocation.started_at,
                    now,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE agent_invocations
                SET status = ?, side_effect_status = ?, invocation_json = ?,
                    outcome_json = ?, outcome_hash = ?, updated_at = ?
                WHERE invocation_id = ?
                """,
                (
                    invocation.status,
                    invocation.side_effect_status,
                    serialized,
                    outcome_json,
                    outcome_hash,
                    now,
                    invocation.invocation_id,
                ),
            )

        self._record_invocation_consumptions(connection, invocation)

        if invocation.operation_key:
            operation = connection.execute(
                "SELECT original_invocation_id FROM operations WHERE operation_key = ?",
                (invocation.operation_key,),
            ).fetchone()
            if operation is None:
                raise ValueError("invocation references a ghost operation")
            if existing is None and invocation.execution_mode == "executed":
                connection.execute(
                    """
                    UPDATE operations
                    SET original_invocation_id = COALESCE(original_invocation_id, ?),
                        last_invocation_id = ?
                    WHERE operation_key = ?
                    """,
                    (
                        invocation.invocation_id,
                        invocation.invocation_id,
                        invocation.operation_key,
                    ),
                )
            elif existing is None:
                connection.execute(
                    """
                    UPDATE operations SET last_invocation_id = ?
                    WHERE operation_key = ?
                    """,
                    (invocation.invocation_id, invocation.operation_key),
                )

    def _record_invocation_consumptions(
        self,
        connection: sqlite3.Connection,
        invocation: AgentInvocation,
    ) -> None:
        """Persist explicit handoff edges without treating JSON as proof."""
        if not invocation.consumed_handoff_message_ids:
            return
        consumer_started = _parse_utc_timestamp(
            invocation.started_at, "consumer invocation"
        )
        for message_id in invocation.consumed_handoff_message_ids:
            source = connection.execute(
                """
                SELECT run_id, trace_id, producer_invocation_id,
                       intended_consumer, route_target, created_at,
                       envelope_json
                FROM handoff_messages WHERE message_id = ?
                """,
                (str(message_id),),
            ).fetchone()
            if source is None:
                raise HandoffValidationError(
                    f"invocation references a ghost handoff message: {message_id}"
                )
            if str(source[0]) != self.run_id or str(source[1]) != self.run_id:
                raise HandoffValidationError("consumed handoff belongs to another run")
            try:
                source_envelope = json.loads(str(source[6] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise HandoffValidationError(
                    "consumed handoff envelope is not valid JSON"
                ) from error
            if not isinstance(source_envelope, dict):
                raise HandoffValidationError("consumed handoff envelope is not an object")
            if str(source[3]) != invocation.agent_id:
                raise HandoffValidationError(
                    "invocation is not the intended handoff consumer"
                )
            source_created = _parse_utc_timestamp(source[5], "handoff")
            if source_created > consumer_started:
                raise HandoffValidationError(
                    "handoff was created after its declared consumer started"
                )
            expected_operation = _ROUTE_TARGET_OPERATIONS.get(str(source[4]))
            if expected_operation and invocation.operation != expected_operation:
                raise HandoffValidationError(
                    "consumer operation does not match the handoff route target"
                )
            same_consumption = connection.execute(
                """
                SELECT consumer_agent_id, consumer_operation,
                       source_producer_invocation_id, binding_status,
                       consumer_attempt, consumption_fence,
                       superseded_by_invocation_id
                FROM handoff_consumptions
                WHERE message_id = ? AND consumer_invocation_id = ?
                """,
                (str(message_id), invocation.invocation_id),
            ).fetchone()
            if same_consumption is not None:
                if (
                    str(same_consumption[0]) != invocation.agent_id
                    or str(same_consumption[1]) != invocation.operation
                    or str(same_consumption[2]) != str(source[2])
                    or int(same_consumption[4] or 0) != int(invocation.attempt)
                ):
                    raise HandoffValidationError(
                        "durable handoff consumption binding is inconsistent"
                    )
                binding_status = str(same_consumption[3])
                consumption_fence = int(same_consumption[5] or 0)
                superseded_by = str(same_consumption[6] or "")
                if binding_status == "legacy_backfilled":
                    # Re-saving a migrated invocation must not promote its
                    # unverified JSON-derived consumption edge.
                    continue
                if consumption_fence <= 0 or (
                    bool(superseded_by) != (binding_status == "superseded")
                ):
                    raise HandoffValidationError(
                        "durable handoff consumption binding is inconsistent"
                    )
                if binding_status not in {
                    *_HANDOFF_PENDING_BINDING_STATUSES,
                    "server_validated",
                    "superseded",
                }:
                    raise HandoffValidationError(
                        "durable handoff consumption binding has an invalid status"
                    )
                # A crash replay can persist an older invocation again after
                # a higher-fenced retry superseded it. This is an idempotent
                # historical save, not a request to reactivate the old edge.
                # In particular, an already-recorded resume handoff belongs
                # to its original lease, so it must not be checked against a
                # later worker fence while a new resume checkpoint is saved.
                continue
            # A newly recorded consumer must prove that its resume control
            # handoff belongs to the worker that is active right now. The
            # check intentionally happens after the historical-edge branch
            # above so replaying a durable invocation cannot invalidate its
            # original fence binding.
            self._validate_resume_handoff_binding(
                connection,
                source_envelope,
                str(message_id),
            )
            if connection.execute(
                "SELECT 1 FROM handoff_receipts WHERE message_id = ?",
                (str(message_id),),
            ).fetchone():
                raise HandoffValidationError(
                    "handoff message already has a server-validated receipt"
                )
            existing_consumers = connection.execute(
                """
                SELECT hc.consumer_invocation_id, hc.consumer_agent_id,
                       hc.consumer_operation, hc.source_producer_invocation_id,
                       hc.consumer_attempt, hc.consumption_fence,
                       hc.binding_status, hc.superseded_by_invocation_id,
                       ai.execution_mode
                FROM handoff_consumptions AS hc
                JOIN agent_invocations AS ai
                  ON ai.invocation_id = hc.consumer_invocation_id
                WHERE hc.message_id = ?
                ORDER BY hc.consumer_attempt DESC, hc.consumption_fence DESC
                """,
                (str(message_id),),
            ).fetchall()
            if existing_consumers:
                latest = existing_consumers[0]
                if any(
                    str(item[1]) != invocation.agent_id
                    or str(item[2]) != invocation.operation
                    or str(item[3]) != str(source[2])
                    for item in existing_consumers
                ):
                    raise HandoffValidationError(
                        "handoff retry has a different consumer or producer binding"
                    )
                if int(invocation.attempt) <= int(latest[4]):
                    raise HandoffValidationError(
                        "handoff retry attempt is not greater than the prior attempt"
                    )
            next_fence = max(
                (int(item[5] or 0) for item in existing_consumers),
                default=0,
            ) + 1
            now = datetime.now(UTC).isoformat()
            active_consumers = [
                item
                for item in existing_consumers
                if not str(item[7] or "")
                and str(item[6]) in _HANDOFF_PENDING_BINDING_STATUSES
            ]
            if any(
                str(item[6]) == "server_validated" for item in existing_consumers
            ):
                raise HandoffValidationError(
                    "handoff message already has a server-validated consumer"
                )
            if active_consumers:
                placeholders = ",".join("?" for _ in active_consumers)
                cursor = connection.execute(
                    f"""
                    UPDATE handoff_consumptions
                    SET binding_status = 'superseded',
                        superseded_by_invocation_id = ?, superseded_at = ?
                    WHERE message_id = ?
                      AND consumer_invocation_id IN ({placeholders})
                      AND superseded_by_invocation_id IS NULL
                      AND binding_status IN (
                          'pending_receipt', 'retry_pending_receipt',
                          'replay_pending_receipt'
                      )
                    """,
                    (
                        invocation.invocation_id,
                        now,
                        str(message_id),
                        *(str(item[0]) for item in active_consumers),
                    ),
                )
                if cursor.rowcount != len(active_consumers):
                    raise HandoffValidationError(
                        "handoff retry state changed during supersession"
                    )
            binding_status = (
                "replay_pending_receipt"
                if invocation.execution_mode == "replayed"
                else "retry_pending_receipt"
                if existing_consumers
                else "pending_receipt"
            )
            cursor = connection.execute(
                """
                INSERT INTO handoff_consumptions(
                    message_id, consumer_invocation_id, consumer_agent_id,
                    consumer_operation, source_producer_invocation_id,
                    binding_status, consumer_attempt, consumption_fence,
                    superseded_by_invocation_id, superseded_at, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    str(message_id),
                    invocation.invocation_id,
                    invocation.agent_id,
                    invocation.operation,
                    str(source[2]),
                    binding_status,
                    int(invocation.attempt),
                    next_fence,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffValidationError(
                    "handoff consumption binding was not inserted"
                )

    def _validate_resume_handoff_binding(
        self,
        connection: sqlite3.Connection,
        envelope: dict[str, Any],
        message_id: str,
    ) -> None:
        """Reject a resume handoff emitted by a stale worker fence."""
        receipt_id = str(envelope.get("resume_receipt_id") or "")
        if not receipt_id:
            if envelope.get("claim_fence") is not None:
                raise HandoffValidationError(
                    f"handoff {message_id} carries a claim fence without a resume receipt"
                )
            return
        try:
            handoff_fence = int(envelope.get("claim_fence") or 0)
        except (TypeError, ValueError) as error:
            raise HandoffValidationError(
                f"resume handoff {message_id} has an invalid claim fence"
            ) from error
        if handoff_fence <= 0:
            raise HandoffValidationError(
                f"resume handoff {message_id} is missing its claim fence"
            )
        if self._execution_owner_token is None or self._execution_fence is None:
            raise HandoffValidationError(
                f"resume handoff {message_id} was consumed without an execution fence"
            )
        now = int(time.time() * 1000)
        lease = connection.execute(
            """
            SELECT receipt_id, owner_token, fence, expires_at_ms
            FROM execution_leases WHERE run_id = ?
            """,
            (self.run_id,),
        ).fetchone()
        if (
            lease is None
            or str(lease[0]) != receipt_id
            or str(lease[1]) != self._execution_owner_token
            or int(lease[2]) != int(self._execution_fence)
            or int(lease[3]) <= now
            or handoff_fence != int(self._execution_fence)
        ):
            raise HandoffValidationError(
                f"resume handoff {message_id} is bound to a stale execution fence"
            )
        receipt = connection.execute(
            """
            SELECT execution_status, claim_owner_token, claim_fence,
                   claim_expires_at_ms
            FROM resume_receipts WHERE idempotency_key = ?
            """,
            (receipt_id,),
        ).fetchone()
        if (
            receipt is None
            or str(receipt[0]) != "running"
            or str(receipt[1] or "") != self._execution_owner_token
            or int(receipt[2] or 0) != int(self._execution_fence)
            or int(receipt[3] or 0) <= now
        ):
            raise HandoffValidationError(
                f"resume handoff {message_id} does not match the active resume receipt claim"
            )

    def store_input_attachment(
        self,
        *,
        name: str,
        media_type: str,
        modality: str,
        data: bytes,
    ) -> InputAttachment:
        from .multimodal import (
            MAX_ATTACHMENT_COUNT,
            MAX_TOTAL_ATTACHMENT_BYTES,
            attachment_digest,
            validate_attachment,
        )

        clean_name, detected_media_type, detected_modality = validate_attachment(
            name,
            media_type,
            data,
        )
        if detected_modality != modality:
            raise ValueError("attachment modality does not match its validated media type")
        digest = attachment_digest(data)
        attachment_id = "I" + digest
        existing_attachments = self.load_input_attachments()
        existing_by_id = {item.id: item for item in existing_attachments}
        existing_manifest = existing_by_id.get(attachment_id)
        if existing_manifest is not None:
            if existing_manifest.sha256 != digest:
                raise ArtifactIntegrityError("attachment identifier collision")
            return self.read_input_attachment(attachment_id)[0]
        if len(existing_attachments) >= MAX_ATTACHMENT_COUNT:
            raise ValueError(f"at most {MAX_ATTACHMENT_COUNT} attachments are allowed")
        if sum(item.byte_length for item in existing_attachments) + len(data) > MAX_TOTAL_ATTACHMENT_BYTES:
            raise ValueError("attachments exceed the 24 MB total limit")
        relative = Path("inputs") / digest[:2] / digest
        target = (self.run_dir / relative).resolve()
        if self.run_dir not in target.parents:
            raise ValueError("attachment content path escapes the run directory")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        if target.exists():
            existing = target.read_bytes()
            if attachment_digest(existing) != digest:
                raise ArtifactIntegrityError(
                    "content-addressed attachment path contains different bytes"
                )
        else:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            os.chmod(target, 0o600)
        created_at = datetime.now(UTC).isoformat()
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR IGNORE INTO input_attachments(
                    attachment_id, run_id, name, media_type, modality, sha256,
                    byte_length, content_uri, created_at, parser_version,
                    status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'upload-content-addressed-v1',
                          'stored', NULL)
                """,
                (
                    attachment_id,
                    self.run_id,
                    clean_name,
                    detected_media_type,
                    detected_modality,
                    digest,
                    len(data),
                    relative.as_posix(),
                    created_at,
                ),
            )
            row = connection.execute(
                """
                SELECT attachment_id, name, media_type, modality, sha256,
                       byte_length, content_uri, created_at, status,
                       parser_version, error
                FROM input_attachments WHERE attachment_id = ? AND run_id = ?
                """,
                (attachment_id, self.run_id),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("attachment manifest was not persisted")
        return _attachment_from_row(row)

    def load_input_attachments(self) -> list[InputAttachment]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT attachment_id, name, media_type, modality, sha256,
                       byte_length, content_uri, created_at, status,
                       parser_version, error
                FROM input_attachments
                WHERE run_id = ? ORDER BY created_at, attachment_id
                """,
                (self.run_id,),
            ).fetchall()
        return [_attachment_from_row(row) for row in rows]

    def read_input_attachment(self, attachment_id: str) -> tuple[InputAttachment, bytes]:
        records = {
            item.id: item for item in self.load_input_attachments()
        }
        attachment = records.get(str(attachment_id))
        if attachment is None:
            raise FileNotFoundError("attachment was not found")
        path = (self.run_dir / attachment.content_uri).resolve()
        if self.run_dir not in path.parents or not path.is_file():
            raise ArtifactIntegrityError("attachment content file is missing or unsafe")
        data = path.read_bytes()
        if len(data) != attachment.byte_length:
            raise ArtifactIntegrityError("attachment byte length does not match its manifest")
        if hashlib.sha256(data).hexdigest() != attachment.sha256:
            raise ArtifactIntegrityError("attachment SHA-256 does not match its manifest")
        return attachment, data

    def input_attachment_audit(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for attachment in self.load_input_attachments():
            valid = False
            error: str | None = None
            try:
                self.read_input_attachment(attachment.id)
                valid = True
            except (OSError, ArtifactIntegrityError) as caught:
                error = str(caught)
            result.append(
                {
                    **asdict(attachment),
                    "manifest_valid": valid,
                    "validation_error": error,
                }
            )
        return result

    def load_invocations(self) -> list[AgentInvocation]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM agent_invocations
                ORDER BY sequence
                """
            ).fetchall()
        return [_project_invocation_row(dict(row))[0] for row in rows]

    def invocation_rows(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM agent_invocations ORDER BY sequence"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            invocation, validation = _project_invocation_row(item)
            item.pop("invocation_json", None)
            item["invocation"] = asdict(invocation)
            item["identity_validation"] = validation
            result.append(item)
        return result

    @staticmethod
    def _audit_limit(limit: int) -> int:
        return max(1, min(100, int(limit)))

    @staticmethod
    def _audit_after(after: object, key: str = "rowid") -> int:
        """Decode a keyset cursor while retaining compatibility with integers."""
        if after in (None, "", 0):
            return 0
        value: object = after
        if isinstance(after, dict):
            value = after.get(key, 0)
        elif isinstance(after, str) and after.lstrip().startswith("{"):
            try:
                parsed = json.loads(after)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("invalid audit cursor") from error
            if not isinstance(parsed, dict):
                raise ValueError("invalid audit cursor")
            value = parsed.get(key, 0)
        try:
            parsed_value = int(value or 0)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid audit cursor") from error
        if parsed_value < 0:
            raise ValueError("invalid audit cursor")
        return parsed_value

    @staticmethod
    def _audit_cursor(after: object) -> dict[str, object]:
        if after in (None, "", 0):
            return {}
        if isinstance(after, dict):
            return dict(after)
        if isinstance(after, str) and after.lstrip().startswith("{"):
            try:
                parsed = json.loads(after)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("invalid audit cursor") from error
            if isinstance(parsed, dict):
                return parsed
        try:
            return {"rowid": max(0, int(after))}
        except (TypeError, ValueError) as error:
            raise ValueError("invalid audit cursor") from error

    def invocation_rows_page(
        self,
        *,
        limit: int = 50,
        after: int = 0,
    ) -> dict[str, Any]:
        """Read invocation history with a sequence keyset, never an offset."""
        limit = self._audit_limit(limit)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM agent_invocations
                WHERE sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (max(0, int(after)), limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in visible:
            item = dict(row)
            invocation, validation = _project_invocation_row(item)
            item.pop("invocation_json", None)
            item["invocation"] = asdict(invocation)
            item["identity_validation"] = validation
            items.append(item)
        return {
            "items": items,
            "has_more": has_more,
            "next_cursor": str(visible[-1]["sequence"]) if has_more and visible else None,
        }

    def invocation(self, invocation_id: str) -> AgentInvocation | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT * FROM agent_invocations
                WHERE invocation_id = ?
                """,
                (invocation_id,),
            ).fetchone()
        return _project_invocation_row(dict(row))[0] if row else None

    def _assert_execution_fence(self, connection: sqlite3.Connection) -> None:
        now = int(time.time() * 1000)
        if self._execution_owner_token is None or self._execution_fence is None:
            active = connection.execute(
                """
                SELECT 1 FROM execution_leases
                WHERE run_id = ? AND expires_at_ms > ?
                """,
                (self.run_id, now),
            ).fetchone()
            if active is not None:
                raise ExecutionFenceLostError(
                    f"run {self.run_id} has an active execution lease; writer is not fence-bound"
                )
            return
        row = connection.execute(
            """
            SELECT owner_token, fence, expires_at_ms
            FROM execution_leases WHERE run_id = ?
            """,
            (self.run_id,),
        ).fetchone()
        if (
            row is None
            or str(row[0]) != self._execution_owner_token
            or int(row[1]) != self._execution_fence
            or int(row[2]) <= now
        ):
            raise ExecutionFenceLostError(
                f"execution fence {self._execution_fence} is no longer active for run {self.run_id}"
            )

    def event(self, event_type: str, node: str, payload: dict[str, Any]) -> None:
        record = self._event_record(event_type, node, payload)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.execute(
                "INSERT INTO outbox(event_id, created_at, event_json) VALUES (?, ?, ?)",
                (record["event_id"], record["created_at"], json.dumps(record, ensure_ascii=False)),
            )
            connection.commit()
        self._flush_outbox()

    def save_agui_messages(
        self,
        thread_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        serialized = json.dumps(messages, ensure_ascii=False)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT INTO agui_message_snapshots(thread_id, messages_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    messages_json = excluded.messages_json,
                    updated_at = excluded.updated_at
                """,
                (thread_id, serialized, datetime.now(UTC).isoformat()),
            )
            connection.commit()

    def load_agui_messages(self, thread_id: str) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT messages_json FROM agui_message_snapshots
                WHERE thread_id = ?
                """,
                (thread_id,),
            ).fetchone()
        if not row:
            return []
        value = json.loads(row[0])
        return [dict(item) for item in value if isinstance(item, dict)]

    def checkpoint(self, node: str, state: ResearchState) -> None:
        state_json = json.dumps(state.as_dict(), ensure_ascii=False)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.execute(
                "INSERT INTO checkpoints(created_at, node, state_json) VALUES (?, ?, ?)",
                (datetime.now(UTC).isoformat(), node, state_json),
            )
            connection.commit()

    def commit_stage(
        self,
        node: str,
        state: ResearchState,
        event_type: str,
        payload: dict[str, Any],
        *,
        artifact: ArtifactRef | None = None,
        artifact_payload: dict[str, Any] | None = None,
        detached_invocation: AgentInvocation | None = None,
    ) -> None:
        record = self._event_record(event_type, node, payload)
        envelope = payload.get("handoff_envelope")
        prepared_artifact: dict[str, Any] | None = None
        receipt_validation: dict[str, Any] | None = None
        try:
            with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 10000")
                connection.execute("BEGIN IMMEDIATE")
                self._assert_execution_fence(connection)
                for invocation in state.agent_invocations:
                    if invocation.run_id not in {"", self.run_id}:
                        raise ValueError("checkpoint contains an invocation from another run")
                    if invocation.trace_id not in {"", self.run_id}:
                        raise ValueError("checkpoint contains an invocation from another trace")
                    invocation.run_id = self.run_id
                    invocation.trace_id = self.run_id
                    self._save_invocation(connection, invocation)
                if detached_invocation is not None:
                    if detached_invocation.run_id not in {"", self.run_id}:
                        raise ValueError(
                            "detached invocation belongs to a different run"
                        )
                    if detached_invocation.trace_id not in {"", self.run_id}:
                        raise ValueError(
                            "detached invocation belongs to a different trace"
                        )
                    detached_invocation.run_id = self.run_id
                    detached_invocation.trace_id = self.run_id
                    self._save_invocation(connection, detached_invocation)

                if artifact is not None:
                    if artifact_payload is None:
                        raise ValueError("artifact payload is required for stage commit")
                    prepared_artifact = self._prepare_artifact(
                        connection,
                        artifact,
                        artifact_payload,
                        require_manifest=True,
                        allow_orphan_recovery=True,
                    )
                elif artifact_payload is not None:
                    raise ValueError("artifact metadata is required for stage commit")

                if envelope is not None:
                    if not isinstance(envelope, dict):
                        raise HandoffValidationError("handoff envelope must be an object")
                    receipt_validation = self._validate_handoff(
                        connection, envelope, artifact
                    )

                self._annotate_usage_provider(
                    connection,
                    str(state.methodology.get("model_provider") or ""),
                )
                self._synchronize_source_binding_status(connection, state)

                state_json = json.dumps(state.as_dict(), ensure_ascii=False)
                checkpoint = connection.execute(
                    "INSERT INTO checkpoints(created_at, node, state_json) VALUES (?, ?, ?)",
                    (record["created_at"], node, state_json),
                )
                checkpoint_id = int(checkpoint.lastrowid)
                if prepared_artifact is not None:
                    self._insert_artifact_manifest(
                        connection,
                        artifact,
                        prepared_artifact,
                        checkpoint_id=checkpoint_id,
                    )
                if envelope is not None:
                    self._insert_handoff(
                        connection,
                        envelope,
                        checkpoint_id,
                        receipt_validation=receipt_validation,
                    )
                    self._record_resume_handoff_transition(
                        connection,
                        node,
                        state,
                        envelope,
                        record["created_at"],
                    )
                connection.execute(
                    "INSERT INTO outbox(event_id, created_at, event_json) VALUES (?, ?, ?)",
                    (
                        record["event_id"],
                        record["created_at"],
                        json.dumps(record, ensure_ascii=False),
                    ),
                )
                if prepared_artifact is not None:
                    self._write_artifact_files(artifact, prepared_artifact)
                connection.commit()
        except HandoffValidationError as error:
            if isinstance(envelope, dict):
                self._record_rejected_receipt(envelope, str(error))
            raise
        self._flush_outbox()

    def _record_resume_handoff_transition(
        self,
        connection: sqlite3.Connection,
        node: str,
        state: ResearchState,
        envelope: dict[str, Any],
        committed_at: str,
    ) -> None:
        """Bind resume handoff phases to the same transaction as their checkpoint."""
        transition = state.resume_transition
        receipt_id = str(transition.get("resume_receipt_id") or "")
        if not receipt_id:
            return

        status = str(transition.get("status") or "")
        if node == "resume":
            if status != "handoff_emitted":
                raise HandoffValidationError(
                    "resume checkpoint did not declare its handoff emission"
                )
            handoff_message_id = str(envelope.get("message_id") or "")
            if str(envelope.get("resume_receipt_id") or "") != receipt_id:
                raise HandoffValidationError(
                    "resume transition receipt disagrees with its handoff envelope"
                )
            from_status = "running"
            to_status = "handoff_emitted"
            agent_invocation_id = str(
                envelope.get("producer_invocation_id") or ""
            )
            agent_id = str(envelope.get("producer") or "")
            operation = f"emit_{node}"
            superseded_handoff_message_id = str(
                transition.get("superseded_handoff_message_id") or ""
            ) or None
            reason = (
                "higher-fenced resume control handoff superseded the prior handoff"
                if superseded_handoff_message_id
                else "resume control handoff durably emitted"
            )
            created_at = str(
                transition.get("handoff_emitted_at") or committed_at
            )
        elif status == "consumed":
            receipt = envelope.get("receipt")
            handoff_message_id = str(transition.get("handoff_message_id") or "")
            if (
                not isinstance(receipt, dict)
                or str(receipt.get("message_id") or "") != handoff_message_id
            ):
                return
            from_status = "handoff_emitted"
            to_status = "consumed"
            agent_invocation_id = str(
                transition.get("consumed_by_invocation_id") or ""
            )
            agent_id = str(transition.get("consumed_by_agent_id") or "")
            operation = str(transition.get("consumed_by_operation") or "")
            superseded_handoff_message_id = None
            reason = "resume control handoff durably consumed by target invocation"
            created_at = str(transition.get("consumed_at") or committed_at)
            if (
                str(receipt.get("consumed_by_invocation_id") or "")
                != agent_invocation_id
                or str(receipt.get("consumed_by_agent_id") or "") != agent_id
                or str(receipt.get("consumed_by_operation") or "") != operation
            ):
                raise HandoffValidationError(
                    "resume transition consumer disagrees with its durable receipt"
                )
        else:
            return

        try:
            owner_fence = int(transition.get("claim_fence") or 0)
        except (TypeError, ValueError) as error:
            raise HandoffValidationError(
                "resume handoff transition has an invalid claim fence"
            ) from error
        if owner_fence <= 0 or not handoff_message_id or not agent_invocation_id:
            raise HandoffValidationError(
                "resume handoff transition is missing its durable binding"
            )
        if (
            self._execution_owner_token is None
            or self._execution_fence is None
            or int(self._execution_fence) != owner_fence
        ):
            raise HandoffValidationError(
                "resume handoff transition is not owned by the active execution fence"
            )

        source = connection.execute(
            """
            SELECT envelope_json FROM handoff_messages
            WHERE message_id = ? AND run_id = ? AND trace_id = ?
            """,
            (handoff_message_id, self.run_id, self.run_id),
        ).fetchone()
        if source is None:
            raise HandoffValidationError(
                "resume transition references a non-durable handoff"
            )
        try:
            source_envelope = json.loads(str(source[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise HandoffValidationError(
                "resume transition references an invalid handoff envelope"
            ) from error
        if (
            not isinstance(source_envelope, dict)
            or str(source_envelope.get("resume_receipt_id") or "") != receipt_id
            or int(source_envelope.get("claim_fence") or 0) != owner_fence
        ):
            raise HandoffValidationError(
                "resume transition disagrees with the durable handoff fence binding"
            )

        receipt = connection.execute(
            """
            SELECT execution_status, claim_owner_token, claim_fence
            FROM resume_receipts WHERE idempotency_key = ?
            """,
            (receipt_id,),
        ).fetchone()
        if (
            receipt is None
            or str(receipt[0]) != "running"
            or str(receipt[1] or "") != self._execution_owner_token
            or int(receipt[2] or 0) != owner_fence
        ):
            raise HandoffValidationError(
                "resume handoff transition does not match the active receipt claim"
            )

        if to_status == "consumed":
            durable_receipt = connection.execute(
                """
                SELECT consumed_by_invocation_id, consumed_by_agent_id,
                       consumed_by_operation, validation_status
                FROM handoff_receipts WHERE message_id = ?
                """,
                (handoff_message_id,),
            ).fetchone()
            if (
                durable_receipt is None
                or str(durable_receipt[0]) != agent_invocation_id
                or str(durable_receipt[1]) != agent_id
                or str(durable_receipt[2] or "") != operation
                or str(durable_receipt[3]) != "server_validated"
            ):
                raise HandoffValidationError(
                    "resume consumption transition lacks a server-validated receipt"
                )

        transition_key = ":".join(
            (
                "resume-handoff",
                receipt_id,
                to_status,
                str(owner_fence),
                handoff_message_id,
                agent_invocation_id,
            )
        )
        values = (
            transition_key,
            "handoff",
            receipt_id,
            from_status,
            to_status,
            owner_fence,
            _owner_token_fingerprint(self._execution_owner_token),
            handoff_message_id,
            agent_invocation_id,
            agent_id,
            operation,
            superseded_handoff_message_id,
            reason,
            created_at,
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO resume_execution_transitions(
                transition_key, transition_kind, idempotency_key,
                from_status, to_status, owner_fence,
                owner_token_fingerprint, handoff_message_id,
                agent_invocation_id, agent_id, operation,
                superseded_handoff_message_id, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        persisted = connection.execute(
            """
            SELECT transition_key, transition_kind, idempotency_key,
                   from_status, to_status, owner_fence,
                   owner_token_fingerprint, handoff_message_id,
                   agent_invocation_id, agent_id, operation,
                   superseded_handoff_message_id, reason, created_at
            FROM resume_execution_transitions WHERE transition_key = ?
            """,
            (transition_key,),
        ).fetchone()
        if persisted is None or tuple(persisted) != values:
            raise HandoffValidationError(
                "resume handoff transition idempotency key collided with different data"
            )

    @staticmethod
    def _annotate_usage_provider(
        connection: sqlite3.Connection,
        provider: str,
    ) -> None:
        """Attach run methodology to rows created before the first checkpoint."""
        if not provider:
            return
        connection.execute(
            """
            UPDATE usage_ledger
            SET provider = ?
            WHERE provider = 'unknown'
            """,
            (provider,),
        )

    @staticmethod
    def _synchronize_source_binding_status(
        connection: sqlite3.Connection,
        state: ResearchState,
    ) -> None:
        """Project the durable binding verdict into the checkpoint state."""
        for source in state.sources:
            if not source.fetch_operation_key or not source.fetch_invocation_id:
                continue
            row = connection.execute(
                """
                SELECT binding_status
                FROM source_fetches
                WHERE source_id = ? AND operation_key = ? AND invocation_id = ?
                ORDER BY recorded_at DESC, fetch_record_id DESC
                LIMIT 1
                """,
                (
                    str(source.id),
                    str(source.fetch_operation_key),
                    str(source.fetch_invocation_id),
                ),
            ).fetchone()
            if row is not None:
                source.fetch_binding_status = str(row[0])

    def commit_resume(
        self,
        state: ResearchState,
        *,
        expected_checkpoint_id: int,
        idempotency_key: str,
        command_hash: str,
        source: str,
        thread_id: str | None,
        protocol_run_id: str | None,
        parent_protocol_run_id: str | None = None,
        payload: dict[str, Any],
        confirmed_operation_keys: list[str],
        interrupt_responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """CAS, authorize and receipt a resume in one SQLite transaction."""
        keys = list(dict.fromkeys(confirmed_operation_keys))
        state_json = json.dumps(state.as_dict(), ensure_ascii=False)
        record = self._event_record("run_resumed", "resume", payload)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                """
                SELECT command_hash, response_json, execution_claimed,
                       execution_status
                FROM resume_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if receipt:
                connection.commit()
                return {
                    "status": "replayed" if receipt[0] == command_hash else "conflict",
                    "reason": "idempotency key was reused with a different command"
                    if receipt[0] != command_hash
                    else "",
                    "response": json.loads(receipt[1]) if receipt[0] == command_hash else {},
                    "execution_claimed": bool(receipt[2]),
                    "execution_status": str(receipt[3] or "legacy_unverified"),
                }

            latest = connection.execute(
                "SELECT id FROM checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not latest or int(latest[0]) != expected_checkpoint_id:
                connection.rollback()
                return {
                    "status": "conflict",
                    "reason": "durable checkpoint changed before resume commit",
                }

            current_ambiguous = {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT operation_key FROM operations
                    WHERE (
                        status = 'started' AND idempotent = 0
                    ) OR status = 'external_outcome_unknown'
                    """
                ).fetchall()
            }
            if current_ambiguous != set(keys):
                connection.rollback()
                return {
                    "status": "conflict",
                    "reason": "confirmed ambiguous operation set is not current and complete",
                    "ambiguous_operation_keys": sorted(current_ambiguous),
                }
            if keys:
                placeholders = ",".join("?" for _ in keys)
                cursor = connection.execute(
                    f"""
                    UPDATE operations
                    SET status = 'retry_authorized', completed_at = ?,
                        error = 'manual retry authorized by user'
                    WHERE operation_key IN ({placeholders})
                      AND (
                          (status = 'started' AND idempotent = 0)
                          OR status = 'external_outcome_unknown'
                      )
                    """,
                    (datetime.now(UTC).isoformat(), *keys),
                )
                if cursor.rowcount != len(keys):
                    connection.rollback()
                    return {
                        "status": "conflict",
                        "reason": "ambiguous operations changed during authorization",
                    }

            if source == "agui":
                response_ids = {
                    str(item["interrupt_id"]) for item in interrupt_responses
                }
                open_rows = connection.execute(
                    """
                    SELECT interrupt_id, thread_id FROM agui_interrupts
                    WHERE status = 'open'
                    """
                ).fetchall()
                open_ids = {str(row[0]) for row in open_rows}
                if response_ids != open_ids or any(
                    str(row[1]) != str(thread_id) for row in open_rows
                ):
                    connection.rollback()
                    return {
                        "status": "conflict",
                        "reason": "resume must resolve the complete open interrupt set for the same thread",
                        "open_interrupt_ids": sorted(open_ids),
                    }

            checkpoint = connection.execute(
                "INSERT INTO checkpoints(created_at, node, state_json) VALUES (?, ?, ?)",
                (record["created_at"], "resume", state_json),
            )
            checkpoint_after = int(checkpoint.lastrowid)
            response = {
                **payload,
                "resume_receipt_id": idempotency_key,
                "checkpoint_id_before": expected_checkpoint_id,
                "checkpoint_id_after": checkpoint_after,
                "worker_required": True,
                "execution_status": "pending",
                "replayed": False,
            }
            if source == "agui":
                now = datetime.now(UTC).isoformat()
                for item in interrupt_responses:
                    cursor = connection.execute(
                        """
                        UPDATE agui_interrupts
                        SET status = ?, consumed_at = ?, resume_receipt_id = ?
                        WHERE interrupt_id = ? AND status = 'open'
                        """,
                        (
                            str(item["status"]),
                            now,
                            idempotency_key,
                            str(item["interrupt_id"]),
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        return {
                            "status": "conflict",
                            "reason": "interrupt changed during resume commit",
                        }
            connection.execute(
                "INSERT INTO outbox(event_id, created_at, event_json) VALUES (?, ?, ?)",
                (
                    record["event_id"],
                    record["created_at"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO resume_receipts(
                    idempotency_key, command_hash, source, thread_id,
                    protocol_run_id, parent_protocol_run_id,
                    checkpoint_id_before, checkpoint_id_after,
                    response_json, execution_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    idempotency_key,
                    command_hash,
                    source,
                    thread_id,
                    protocol_run_id,
                    parent_protocol_run_id,
                    expected_checkpoint_id,
                    checkpoint_after,
                    json.dumps(response, ensure_ascii=False),
                    record["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO resume_execution_transitions(
                    idempotency_key, from_status, to_status, reason, created_at
                ) VALUES (?, 'created', 'pending', 'resume command committed', ?)
                """,
                (idempotency_key, record["created_at"]),
            )
            connection.commit()
        self._flush_outbox()
        return {
            "status": "committed",
            "response": response,
            "execution_claimed": False,
            "execution_status": "pending",
        }

    def commit_interrupt_cancellation(
        self,
        *,
        expected_checkpoint_id: int,
        idempotency_key: str,
        command_hash: str,
        thread_id: str,
        protocol_run_id: str,
        parent_protocol_run_id: str | None,
        interrupt_responses: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Atomically consume a complete interrupt set without resuming execution."""
        payload = {
            "run_id": self.run_id,
            "status": "interrupts_cancelled",
            "source": "agui",
            "thread_id": thread_id,
            "protocol_run_id": protocol_run_id,
            "parent_protocol_run_id": parent_protocol_run_id,
            "interrupt_ids": [
                str(item["interrupt_id"]) for item in interrupt_responses
            ],
        }
        record = self._event_record(
            "agui_interrupts_cancelled",
            "resume",
            payload,
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            receipt = connection.execute(
                """
                SELECT command_hash, response_json, execution_claimed,
                       execution_status
                FROM resume_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if receipt:
                connection.commit()
                return {
                    "status": "replayed" if receipt[0] == command_hash else "conflict",
                    "reason": "idempotency key was reused with a different command"
                    if receipt[0] != command_hash
                    else "",
                    "response": json.loads(receipt[1]) if receipt[0] == command_hash else {},
                    "execution_claimed": bool(receipt[2]),
                    "execution_status": str(receipt[3] or "legacy_unverified"),
                }
            latest = connection.execute(
                "SELECT id FROM checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not latest or int(latest[0]) != expected_checkpoint_id:
                connection.rollback()
                return {
                    "status": "conflict",
                    "reason": "durable checkpoint changed before interrupt cancellation",
                }
            response_ids = {
                str(item["interrupt_id"]) for item in interrupt_responses
            }
            open_rows = connection.execute(
                """
                SELECT interrupt_id, thread_id FROM agui_interrupts
                WHERE status = 'open'
                """
            ).fetchall()
            open_ids = {str(row[0]) for row in open_rows}
            if response_ids != open_ids or any(
                str(row[1]) != thread_id for row in open_rows
            ):
                connection.rollback()
                return {
                    "status": "conflict",
                    "reason": "cancellation must cover the complete open interrupt set",
                    "open_interrupt_ids": sorted(open_ids),
                }
            if any(item.get("status") != "cancelled" for item in interrupt_responses):
                connection.rollback()
                return {
                    "status": "conflict",
                    "reason": "non-cancelled response cannot use cancellation commit",
                }
            now = datetime.now(UTC).isoformat()
            for interrupt_id in sorted(response_ids):
                cursor = connection.execute(
                    """
                    UPDATE agui_interrupts
                    SET status = 'cancelled', consumed_at = ?, resume_receipt_id = ?
                    WHERE interrupt_id = ? AND status = 'open'
                    """,
                    (now, idempotency_key, interrupt_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    return {
                        "status": "conflict",
                        "reason": "interrupt changed during cancellation",
                    }
            response = {
                **payload,
                "resume_receipt_id": idempotency_key,
                "checkpoint_id_before": expected_checkpoint_id,
                "checkpoint_id_after": expected_checkpoint_id,
                "worker_required": False,
                "execution_status": "not_required",
                "replayed": False,
            }
            connection.execute(
                "INSERT INTO outbox(event_id, created_at, event_json) VALUES (?, ?, ?)",
                (
                    record["event_id"],
                    record["created_at"],
                    json.dumps(record, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO resume_receipts(
                    idempotency_key, command_hash, source, thread_id,
                    protocol_run_id, parent_protocol_run_id,
                    checkpoint_id_before, checkpoint_id_after,
                    response_json, execution_status, created_at
                ) VALUES (?, ?, 'agui', ?, ?, ?, ?, ?, ?, 'not_required', ?)
                """,
                (
                    idempotency_key,
                    command_hash,
                    thread_id,
                    protocol_run_id,
                    parent_protocol_run_id,
                    expected_checkpoint_id,
                    expected_checkpoint_id,
                    json.dumps(response, ensure_ascii=False),
                    record["created_at"],
                ),
            )
            connection.execute(
                """
                INSERT INTO resume_execution_transitions(
                    idempotency_key, from_status, to_status, reason, created_at
                ) VALUES (
                    ?, 'created', 'not_required',
                    'all AG-UI interrupts were cancelled', ?
                )
                """,
                (idempotency_key, record["created_at"]),
            )
            connection.commit()
        self._flush_outbox()
        return {
            "status": "committed",
            "response": response,
            "execution_claimed": False,
            "execution_status": "not_required",
        }

    def claim_resume_execution(
        self,
        idempotency_key: str,
        *,
        allow_reclaim: bool = False,
        owner_token: str | None = None,
        fence: int | None = None,
    ) -> bool:
        credentials = self._acquired_execution_leases.get(idempotency_key)
        if owner_token is None and credentials is not None:
            owner_token = credentials[0]
        if fence is None and credentials is not None:
            fence = credentials[1]
        if not owner_token or fence is None or int(fence) <= 0:
            return False

        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT owner_token, fence, expires_at_ms
                FROM execution_leases
                WHERE run_id = ? AND receipt_id = ?
                """,
                (self.run_id, idempotency_key),
            ).fetchone()
            if (
                lease is None
                or str(lease[0]) != owner_token
                or int(lease[1]) != int(fence)
                or int(lease[2]) <= now
            ):
                connection.rollback()
                return False

            receipt = connection.execute(
                """
                SELECT execution_claimed, claim_owner_token, claim_fence,
                       claim_expires_at_ms, execution_status
                FROM resume_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
            if receipt is None:
                connection.rollback()
                return False

            claimed = bool(receipt[0])
            prior_owner = str(receipt[1] or "")
            prior_fence = int(receipt[2] or 0)
            prior_expiry = (
                int(receipt[3]) if receipt[3] is not None else None
            )
            prior_status = str(receipt[4] or "legacy_unverified")
            if (
                prior_status not in _RESUME_EXECUTION_STATUSES
                or prior_status
                in {"completed", "not_required", "legacy_unverified"}
                or (claimed and prior_status != "running")
                or (
                    not claimed
                    and prior_status not in {"pending", "startup_failed", "failed"}
                )
            ):
                connection.rollback()
                return False
            if claimed and prior_owner == owner_token and prior_fence == int(fence):
                # A reclaim is a takeover by a strictly newer execution
                # fence. Even the current owner must not use allow_reclaim to
                # turn an active claim into a second worker claim.
                connection.rollback()
                return False

            if claimed:
                if not allow_reclaim or int(fence) <= prior_fence:
                    connection.rollback()
                    return False
                prior_active = connection.execute(
                    """
                    SELECT 1 FROM execution_leases
                    WHERE run_id = ? AND owner_token = ? AND fence = ?
                      AND expires_at_ms > ?
                    """,
                    (self.run_id, prior_owner, prior_fence, now),
                ).fetchone()
                if prior_active is not None or (
                    prior_expiry is not None and prior_expiry > now
                ):
                    connection.rollback()
                    return False

            if claimed:
                cursor = connection.execute(
                    """
                    UPDATE resume_receipts
                    SET execution_claimed = 1, claim_owner_token = ?,
                        claim_fence = ?, claim_expires_at_ms = ?,
                        execution_status = 'running',
                        execution_started_at = COALESCE(execution_started_at, ?),
                        execution_completed_at = NULL, execution_error = NULL,
                        durable_run_status = NULL
                    WHERE idempotency_key = ? AND execution_claimed = 1
                      AND execution_status = 'running'
                      AND claim_owner_token IS ? AND claim_fence = ?
                      AND claim_expires_at_ms IS ?
                    """,
                    (
                        owner_token,
                        int(fence),
                        int(lease[2]),
                        datetime.now(UTC).isoformat(),
                        idempotency_key,
                        receipt[1],
                        prior_fence,
                        prior_expiry,
                    ),
                )
            else:
                if receipt[1] is not None or prior_fence != 0 or prior_expiry is not None:
                    connection.rollback()
                    return False
                cursor = connection.execute(
                    """
                    UPDATE resume_receipts
                    SET execution_claimed = 1, claim_owner_token = ?,
                        claim_fence = ?, claim_expires_at_ms = ?,
                        execution_status = 'running', execution_started_at = ?,
                        execution_completed_at = NULL, execution_error = NULL,
                        durable_run_status = NULL
                    WHERE idempotency_key = ? AND execution_claimed = 0
                      AND execution_status = ?
                      AND claim_owner_token IS NULL AND claim_fence = 0
                      AND claim_expires_at_ms IS NULL
                    """,
                    (
                        owner_token,
                        int(fence),
                        int(lease[2]),
                        datetime.now(UTC).isoformat(),
                        idempotency_key,
                        prior_status,
                    ),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO resume_execution_transitions(
                    idempotency_key, from_status, to_status, owner_fence,
                    owner_token_fingerprint, reason, created_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    prior_status,
                    int(fence),
                    _owner_token_fingerprint(owner_token),
                    "stale worker claim reclaimed" if claimed else "worker claim acquired",
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
            return True

    def release_resume_execution_claim(
        self,
        idempotency_key: str,
        *,
        owner_token: str | None = None,
        fence: int | None = None,
        error: str = "worker thread failed before startup",
    ) -> bool:
        """Record a retryable startup failure and clear the active claim."""
        credentials = self._acquired_execution_leases.get(idempotency_key)
        if owner_token is None and credentials is not None:
            owner_token = credentials[0]
        if fence is None and credentials is not None:
            fence = credentials[1]
        if not owner_token or fence is None or int(fence) <= 0:
            return False

        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT claim_expires_at_ms
                FROM resume_receipts
                WHERE idempotency_key = ? AND execution_claimed = 1
                  AND execution_status = 'running'
                  AND claim_owner_token = ? AND claim_fence = ?
                """,
                (idempotency_key, owner_token, int(fence)),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE resume_receipts
                SET execution_claimed = 0, claim_owner_token = NULL,
                    claim_fence = 0, claim_expires_at_ms = NULL,
                    execution_status = 'startup_failed',
                    execution_completed_at = NULL, execution_error = ?
                WHERE idempotency_key = ? AND execution_claimed = 1
                  AND execution_status = 'running'
                  AND claim_owner_token = ? AND claim_fence = ?
                  AND claim_expires_at_ms IS ?
                """,
                (
                    str(error)[:2000],
                    idempotency_key,
                    owner_token,
                    int(fence),
                    row[0],
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO resume_execution_transitions(
                    idempotency_key, from_status, to_status, owner_fence,
                    owner_token_fingerprint, reason, created_at
                ) VALUES (?, 'running', 'startup_failed', ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    int(fence),
                    _owner_token_fingerprint(owner_token),
                    str(error)[:500],
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        self._acquired_execution_leases.pop(idempotency_key, None)
        return True

    def finish_resume_execution(
        self,
        idempotency_key: str,
        *,
        owner_token: str,
        fence: int,
        status: str,
        durable_run_status: str | None = None,
        error: str | None = None,
    ) -> bool:
        """Terminally fence one claimed resume so its receipt cannot restart."""
        if status not in {"completed", "failed"}:
            raise ValueError("resume execution terminal status is invalid")
        if not owner_token or int(fence) <= 0:
            return False
        now = datetime.now(UTC).isoformat()
        reason = (
            f"worker finished with durable run status {durable_run_status}"
            if status == "completed"
            else str(error or "resume worker failed")[:500]
        )
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT 1 FROM execution_leases
                WHERE run_id = ? AND receipt_id = ?
                  AND owner_token = ? AND fence = ? AND expires_at_ms > ?
                """,
                (
                    self.run_id,
                    idempotency_key,
                    owner_token,
                    int(fence),
                    int(time.time() * 1000),
                ),
            ).fetchone()
            if lease is None:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE resume_receipts
                SET execution_claimed = 0, claim_owner_token = NULL,
                    claim_fence = 0, claim_expires_at_ms = NULL,
                    execution_status = ?, execution_completed_at = ?,
                    execution_error = ?, durable_run_status = ?
                WHERE idempotency_key = ? AND execution_claimed = 1
                  AND execution_status = 'running'
                  AND claim_owner_token = ? AND claim_fence = ?
                """,
                (
                    status,
                    now,
                    str(error)[:2000] if error else None,
                    str(durable_run_status)[:120] if durable_run_status else None,
                    idempotency_key,
                    owner_token,
                    int(fence),
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            connection.execute(
                """
                INSERT INTO resume_execution_transitions(
                    idempotency_key, from_status, to_status, owner_fence,
                    owner_token_fingerprint, reason, created_at
                ) VALUES (?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    status,
                    int(fence),
                    _owner_token_fingerprint(owner_token),
                    reason,
                    now,
                ),
            )
            connection.commit()
        return True

    def acquire_execution_lease(
        self,
        receipt_id: str,
        *,
        ttl_ms: int = 15_000,
    ) -> dict[str, Any] | None:
        if int(ttl_ms) <= 0:
            raise ValueError("execution lease TTL must be positive")
        now = int(time.time() * 1000)
        owner_token = uuid.uuid4().hex
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT owner_token, receipt_id, fence, expires_at_ms
                FROM execution_leases WHERE run_id = ?
                """,
                (self.run_id,),
            ).fetchone()
            if row and int(row[3]) > now:
                connection.rollback()
                return None
            fence = int(row[2]) + 1 if row else 1
            expires_at_ms = now + int(ttl_ms)
            if row is None:
                cursor = connection.execute(
                    """
                    INSERT INTO execution_leases(
                        run_id, owner_token, receipt_id, fence,
                        acquired_at_ms, heartbeat_at_ms, expires_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.run_id,
                        owner_token,
                        receipt_id,
                        fence,
                        now,
                        now,
                        expires_at_ms,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE execution_leases
                    SET owner_token = ?, receipt_id = ?, fence = ?,
                        acquired_at_ms = ?, heartbeat_at_ms = ?, expires_at_ms = ?
                    WHERE run_id = ? AND owner_token = ? AND receipt_id = ?
                      AND fence = ? AND expires_at_ms = ? AND expires_at_ms <= ?
                    """,
                    (
                        owner_token,
                        receipt_id,
                        fence,
                        now,
                        now,
                        expires_at_ms,
                        self.run_id,
                        str(row[0]),
                        str(row[1]),
                        int(row[2]),
                        int(row[3]),
                        now,
                    ),
                )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
        self._acquired_execution_leases[receipt_id] = (
            owner_token,
            fence,
            expires_at_ms,
        )
        return {
            "owner_token": owner_token,
            "fence": fence,
            "expires_at_ms": expires_at_ms,
            "receipt_id": receipt_id,
        }

    def heartbeat_execution_lease(
        self,
        owner_token: str,
        fence: int,
        *,
        ttl_ms: int = 15_000,
    ) -> bool:
        if int(ttl_ms) <= 0:
            raise ValueError("execution lease TTL must be positive")
        now = int(time.time() * 1000)
        expires_at_ms = now + int(ttl_ms)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT receipt_id FROM execution_leases
                WHERE run_id = ? AND owner_token = ? AND fence = ?
                  AND expires_at_ms > ?
                """,
                (self.run_id, owner_token, int(fence), now),
            ).fetchone()
            if lease is None:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE execution_leases
                SET heartbeat_at_ms = ?, expires_at_ms = ?
                WHERE run_id = ? AND owner_token = ? AND fence = ?
                  AND expires_at_ms > ?
                """,
                (
                    now,
                    expires_at_ms,
                    self.run_id,
                    owner_token,
                    int(fence),
                    now,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    UPDATE resume_receipts
                    SET claim_expires_at_ms = ?
                    WHERE idempotency_key = ? AND execution_claimed = 1
                      AND claim_owner_token = ? AND claim_fence = ?
                    """,
                    (expires_at_ms, str(lease[0]), owner_token, int(fence)),
                )
            connection.commit()
        if cursor.rowcount == 1 and str(lease[0]) in self._acquired_execution_leases:
            self._acquired_execution_leases[str(lease[0])] = (
                owner_token,
                int(fence),
                expires_at_ms,
            )
        return cursor.rowcount == 1

    def release_execution_lease(self, owner_token: str, fence: int) -> bool:
        now = int(time.time() * 1000)
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                """
                SELECT receipt_id, expires_at_ms
                FROM execution_leases
                WHERE run_id = ? AND owner_token = ? AND fence = ?
                """,
                (self.run_id, owner_token, int(fence)),
            ).fetchone()
            if lease is None or int(lease[1]) <= now:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE execution_leases
                SET heartbeat_at_ms = ?, expires_at_ms = ?
                WHERE run_id = ? AND owner_token = ? AND fence = ?
                  AND expires_at_ms > ?
                """,
                (now, now, self.run_id, owner_token, int(fence), now),
            )
            if cursor.rowcount == 1:
                # Releasing the worker lease makes its resume claim stale in
                # the same transaction. A later owner can then replace it
                # with a higher fence, while an old owner still cannot clear
                # the replacement claim.
                connection.execute(
                    """
                    UPDATE resume_receipts
                    SET claim_expires_at_ms = ?
                    WHERE idempotency_key = ? AND execution_claimed = 1
                      AND claim_owner_token = ? AND claim_fence = ?
                    """,
                    (now, str(lease[0]), owner_token, int(fence)),
                )
            connection.commit()
        return cursor.rowcount == 1

    def resume_receipt(self, idempotency_key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT command_hash, response_json, execution_claimed,
                       claim_owner_token, claim_fence, claim_expires_at_ms,
                       execution_status, durable_run_status, execution_started_at,
                       execution_completed_at, execution_error,
                       source, thread_id, protocol_run_id,
                       parent_protocol_run_id, checkpoint_id_before,
                       checkpoint_id_after, created_at
                FROM resume_receipts WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if not row:
            return None
        return {
            "command_hash": str(row[0]),
            "response": json.loads(row[1]),
            "execution_claimed": bool(row[2]),
            "claim_owner_token": str(row[3]) if row[3] else None,
            "claim_fence": int(row[4] or 0),
            "claim_expires_at_ms": int(row[5]) if row[5] is not None else None,
            "execution_status": str(row[6] or "legacy_unverified"),
            "durable_run_status": str(row[7]) if row[7] else None,
            "execution_started_at": str(row[8]) if row[8] else None,
            "execution_completed_at": str(row[9]) if row[9] else None,
            "execution_error": str(row[10]) if row[10] else None,
            "source": str(row[11]),
            "thread_id": str(row[12]) if row[12] else None,
            "protocol_run_id": str(row[13]) if row[13] else None,
            "parent_protocol_run_id": str(row[14]) if row[14] else None,
            "checkpoint_id_before": int(row[15]),
            "checkpoint_id_after": int(row[16]),
            "created_at": str(row[17]),
        }

    def resume_receipt_audit(self) -> list[dict[str, Any]]:
        """Return resume authorization and execution transitions without lease secrets."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            receipts = connection.execute(
                """
                SELECT idempotency_key, source, thread_id, protocol_run_id,
                       parent_protocol_run_id, checkpoint_id_before,
                       checkpoint_id_after, execution_claimed, claim_owner_token,
                       claim_fence, claim_expires_at_ms, execution_status,
                       durable_run_status,
                       execution_started_at, execution_completed_at,
                       execution_error, created_at
                FROM resume_receipts ORDER BY created_at, idempotency_key
                """
            ).fetchall()
            transitions = connection.execute(
                """
                SELECT transition_id, transition_key, transition_kind,
                       idempotency_key, from_status, to_status,
                       owner_fence, owner_token_fingerprint,
                       handoff_message_id, agent_invocation_id,
                       agent_id, operation, superseded_handoff_message_id,
                       reason, created_at
                FROM resume_execution_transitions
                ORDER BY transition_id
                """
            ).fetchall()
        by_receipt: dict[str, list[dict[str, Any]]] = {}
        for transition in transitions:
            item = dict(transition)
            by_receipt.setdefault(str(item.pop("idempotency_key")), []).append(item)
        result: list[dict[str, Any]] = []
        for receipt in receipts:
            item = dict(receipt)
            owner_token = item.pop("claim_owner_token", None)
            item["claim_owner_fingerprint"] = _owner_token_fingerprint(
                str(owner_token) if owner_token else None
            )
            item["execution_claimed"] = bool(item["execution_claimed"])
            item["transitions"] = by_receipt.get(str(item["idempotency_key"]), [])
            result.append(item)
        return result

    def resume_receipt_audit_page(
        self,
        *,
        limit: int = 50,
        after: object = 0,
    ) -> dict[str, Any]:
        """Read resume receipts by rowid and attach only their transitions."""
        limit = self._audit_limit(limit)
        cursor = self._audit_cursor(after)
        receipt_after = self._audit_after(cursor, "receipt")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            receipts = connection.execute(
                """
                SELECT rowid AS _audit_rowid, idempotency_key, source,
                       thread_id, protocol_run_id, parent_protocol_run_id,
                       checkpoint_id_before, checkpoint_id_after,
                       execution_claimed, claim_owner_token, claim_fence,
                       claim_expires_at_ms, execution_status,
                       durable_run_status, execution_started_at,
                       execution_completed_at, execution_error, created_at
                FROM resume_receipts
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (receipt_after, limit + 1),
            ).fetchall()
            visible = receipts[:limit]
            by_receipt: dict[str, list[dict[str, Any]]] = {}
            keys = [str(row["idempotency_key"]) for row in visible]
            if keys:
                placeholders = ",".join("?" for _ in keys)
                transitions = connection.execute(
                    f"""
                    SELECT transition_id, transition_key, transition_kind,
                           idempotency_key, from_status, to_status,
                           owner_fence, owner_token_fingerprint,
                           handoff_message_id, agent_invocation_id,
                           agent_id, operation, superseded_handoff_message_id,
                           reason, created_at
                    FROM resume_execution_transitions
                    WHERE idempotency_key IN ({placeholders})
                    ORDER BY transition_id
                    """,
                    keys,
                ).fetchall()
                for transition in transitions:
                    item = dict(transition)
                    by_receipt.setdefault(
                        str(item.pop("idempotency_key")), []
                    ).append(item)
        result: list[dict[str, Any]] = []
        for receipt in visible:
            item = dict(receipt)
            item.pop("_audit_rowid", None)
            owner_token = item.pop("claim_owner_token", None)
            item["claim_owner_fingerprint"] = _owner_token_fingerprint(
                str(owner_token) if owner_token else None
            )
            item["execution_claimed"] = bool(item["execution_claimed"])
            item["transitions"] = by_receipt.get(str(item["idempotency_key"]), [])
            result.append(item)
        has_more = len(receipts) > limit
        return {
            "items": result,
            "has_more": has_more,
            "next_cursor": json.dumps(
                {"receipt": int(visible[-1]["_audit_rowid"])},
                separators=(",", ":"),
            )
            if has_more and visible
            else None,
        }

    def create_agui_interrupt(
        self,
        thread_id: str,
        protocol_run_id: str,
        reason: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        with agui_interrupt_index_lock(self.run_dir.parent):
            with closing(sqlite3.connect(self.database_path)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT interrupt_id FROM agui_interrupts
                    WHERE thread_id = ? AND reason = ? AND status = 'open'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (thread_id, reason),
                ).fetchone()
                if existing:
                    return str(existing[0])
                interrupt_id = f"int:v1:{uuid.uuid4().hex}"
                connection.execute(
                    """
                    INSERT INTO agui_interrupts(
                        interrupt_id, thread_id, protocol_run_id, reason,
                        response_schema_json, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'open', ?)
                    """,
                    (
                        interrupt_id,
                        thread_id,
                        protocol_run_id,
                        reason,
                        json.dumps(response_schema, ensure_ascii=False)
                        if response_schema is not None
                        else None,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                connection.commit()
            return interrupt_id

    def open_agui_interrupts(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT interrupt_id, thread_id, protocol_run_id, reason,
                       response_schema_json
                FROM agui_interrupts WHERE status = 'open'
                ORDER BY created_at, interrupt_id
                """
            ).fetchall()
        return [
            {
                "interrupt_id": str(row[0]),
                "thread_id": str(row[1]),
                "protocol_run_id": str(row[2]),
                "reason": str(row[3]),
                "response_schema": json.loads(row[4]) if row[4] else None,
            }
            for row in rows
        ]

    def agui_interrupt_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT interrupt_id, thread_id, protocol_run_id, reason,
                       response_schema_json, status, created_at, consumed_at,
                       resume_receipt_id
                FROM agui_interrupts
                ORDER BY created_at, interrupt_id
                """
            ).fetchall()
        return [
            {
                "interrupt_id": str(row[0]),
                "thread_id": str(row[1]),
                "protocol_run_id": str(row[2]),
                "reason": str(row[3]),
                "response_schema": json.loads(row[4]) if row[4] else None,
                "status": str(row[5]),
                "created_at": str(row[6]),
                "consumed_at": str(row[7]) if row[7] else None,
                "resume_receipt_id": str(row[8]) if row[8] else None,
            }
            for row in rows
        ]

    def agui_message_snapshot_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT thread_id, messages_json, updated_at
                FROM agui_message_snapshots ORDER BY thread_id
                """
            ).fetchall()
        result = []
        for thread_id, messages_json, updated_at in rows:
            messages = json.loads(messages_json)
            result.append(
                {
                    "thread_id": str(thread_id),
                    "message_count": len(messages),
                    "message_ids": [
                        str(item.get("id", ""))
                        for item in messages
                        if isinstance(item, dict)
                    ],
                    "roles": [
                        str(item.get("role", ""))
                        for item in messages
                        if isinstance(item, dict)
                    ],
                    "updated_at": str(updated_at),
                }
            )
        return result

    def execution_lease_audit(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT receipt_id, fence, acquired_at_ms,
                       heartbeat_at_ms, expires_at_ms
                FROM execution_leases WHERE run_id = ?
                """,
                (self.run_id,),
            ).fetchone()
        if not row:
            return None
        now = int(time.time() * 1000)
        return {
            "receipt_id": str(row[0]),
            "fence": int(row[1]),
            "acquired_at_ms": int(row[2]),
            "heartbeat_at_ms": int(row[3]),
            "expires_at_ms": int(row[4]),
            "active": int(row[4]) > now,
        }

    def begin_operation(
        self,
        operation_key: str,
        node: str,
        semantic_input_hash: str,
        *,
        kind: str = "model",
        idempotent: bool = False,
        lease_ms: int = 30_000,
    ) -> dict[str, Any]:
        now_ms = int(time.time() * 1000)
        owner_token = self._execution_owner_token or self._operation_owner_token
        owner_fence = self._execution_fence or 0
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            lease_expires_at_ms = now_ms + max(1, int(lease_ms))
            if self._execution_owner_token is not None:
                execution_lease = connection.execute(
                    """
                    SELECT expires_at_ms FROM execution_leases
                    WHERE run_id = ? AND owner_token = ? AND fence = ?
                    """,
                    (
                        self.run_id,
                        self._execution_owner_token,
                        self._execution_fence,
                    ),
                ).fetchone()
                if execution_lease is not None:
                    lease_expires_at_ms = int(execution_lease[0])
            row = connection.execute(
                "SELECT * FROM operations WHERE operation_key = ?", (operation_key,)
            ).fetchone()
            if row is not None:
                if (
                    str(row["node"]) != node
                    or str(row["semantic_input_hash"]) != semantic_input_hash
                    or str(row["kind"]) != kind
                    or bool(row["idempotent"]) != bool(idempotent)
                ):
                    connection.rollback()
                    raise ValueError(
                        "operation idempotency key was reused with different semantics"
                    )
                if row["status"] == "started":
                    owner_is_expired = False
                    prior_owner = row["owner_token"]
                    prior_fence = int(row["owner_fence"] or 0)
                    prior_expiry = row["lease_expires_at_ms"]
                    if prior_owner and prior_fence > 0:
                        active_owner = connection.execute(
                            """
                            SELECT 1 FROM execution_leases
                            WHERE run_id = ? AND owner_token = ? AND fence = ?
                              AND expires_at_ms > ?
                            """,
                            (self.run_id, prior_owner, prior_fence, now_ms),
                        ).fetchone()
                        owner_is_expired = active_owner is None
                    elif prior_owner and prior_expiry is not None:
                        owner_is_expired = int(prior_expiry) <= now_ms

                    if idempotent and owner_is_expired:
                        started_at = datetime.now(UTC).isoformat()
                        cursor = connection.execute(
                            """
                            UPDATE operations
                            SET status = 'started', started_at = ?, completed_at = NULL,
                                result_json = NULL, error = NULL,
                                attempt_count = attempt_count + 1,
                                owner_token = ?, owner_fence = ?,
                                lease_expires_at_ms = ?, last_invocation_id = NULL,
                                result_invocation_id = NULL,
                                side_effect_status = 'unknown'
                            WHERE operation_key = ? AND status = 'started'
                              AND owner_token = ?
                              AND COALESCE(owner_fence, 0) = ?
                              AND COALESCE(lease_expires_at_ms, 0) = ?
                            """,
                            (
                                started_at,
                                owner_token,
                                owner_fence,
                                lease_expires_at_ms,
                                operation_key,
                                prior_owner,
                                prior_fence,
                                int(prior_expiry or 0),
                            ),
                        )
                        if cursor.rowcount == 1:
                            connection.commit()
                            return {
                                "operation_key": operation_key,
                                "node": node,
                                "semantic_input_hash": semantic_input_hash,
                                "status": "new",
                                "started_at": started_at,
                                "attempt_count": int(row["attempt_count"]) + 1,
                                "retry_reason": "expired_owner_fence",
                            }
                        row = connection.execute(
                            "SELECT * FROM operations WHERE operation_key = ?",
                            (operation_key,),
                        ).fetchone()
                    response = dict(row)
                    response["status"] = "in_progress"
                    response["owner_expired"] = owner_is_expired
                    connection.commit()
                    return response

                if row["status"] == "external_outcome_unknown":
                    response = dict(row)
                    response["status"] = "external_outcome_unknown"
                    response["retry_requires_confirmation"] = True
                    connection.commit()
                    return response

                if row["status"] in {"failed", "retry_authorized"}:
                    if row["status"] == "failed" and _is_non_retryable_operation_error(
                        row["error"]
                    ):
                        response = dict(row)
                        response["status"] = "non_retryable"
                        response["retryable"] = False
                        connection.commit()
                        return response
                    started_at = datetime.now(UTC).isoformat()
                    cursor = connection.execute(
                        """
                        UPDATE operations
                        SET status = 'started', started_at = ?, completed_at = NULL,
                            result_json = NULL, error = NULL,
                            attempt_count = attempt_count + 1,
                            kind = ?, idempotent = ?, owner_token = ?,
                            owner_fence = ?, lease_expires_at_ms = ?,
                            last_invocation_id = NULL,
                            result_invocation_id = NULL,
                            side_effect_status = 'unknown'
                        WHERE operation_key = ? AND status = ?
                        """,
                        (
                            started_at,
                            kind,
                            int(idempotent),
                            owner_token,
                            owner_fence,
                            lease_expires_at_ms,
                            operation_key,
                            row["status"],
                        ),
                    )
                    if cursor.rowcount != 1:
                        connection.rollback()
                        raise RuntimeError("operation changed during conditional retry")
                    connection.commit()
                    return {
                        "operation_key": operation_key,
                        "node": node,
                        "semantic_input_hash": semantic_input_hash,
                        "status": "new",
                        "started_at": started_at,
                        "attempt_count": int(row["attempt_count"]) + 1,
                    }
                connection.commit()
                return dict(row)
            started_at = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT INTO operations(
                    operation_key, node, kind, idempotent, attempt_count,
                    semantic_input_hash, status, started_at, owner_token,
                    owner_fence, lease_expires_at_ms, side_effect_status
                ) VALUES (?, ?, ?, ?, 1, ?, 'started', ?, ?, ?, ?, 'unknown')
                """,
                (
                    operation_key,
                    node,
                    kind,
                    int(idempotent),
                    semantic_input_hash,
                    started_at,
                    owner_token,
                    owner_fence,
                    lease_expires_at_ms,
                ),
            )
            connection.commit()
        return {
            "operation_key": operation_key,
            "node": node,
            "semantic_input_hash": semantic_input_hash,
            "status": "new",
            "started_at": started_at,
            "attempt_count": 1,
        }

    def record_model_usage_event(
        self,
        operation_key: str,
        usage: dict[str, Any],
    ) -> bool:
        """Append usage from one successful provider response immediately.

        This deliberately records response deltas rather than a cumulative
        provider snapshot.  A later operation summary remains useful for
        replay and recovery, but aggregate cost accounting uses these events
        whenever they exist to avoid waiting for a multi-call operation.
        """

        settled_at = datetime.now(UTC).isoformat()
        owner_token = self._execution_owner_token or self._operation_owner_token
        owner_fence = self._execution_fence or 0
        # The live usage write races harmlessly with status readers and the
        # worker heartbeat.  Give it the same bounded SQLite wait budget as
        # the other durable writes instead of silently losing an update on a
        # short-lived read/write overlap.
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.row_factory = sqlite3.Row
            operation = connection.execute(
                """
                SELECT kind, status, attempt_count, owner_token, owner_fence
                FROM operations WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if operation is None:
                connection.rollback()
                raise ValueError("cannot record usage for a ghost operation")
            if str(operation["kind"] or "model") != "model":
                connection.rollback()
                raise ValueError("only model operations can record model usage")
            if str(operation["status"] or "") != "started":
                connection.rollback()
                raise RuntimeError(
                    f"operation {operation_key} is not active for usage recording"
                )
            if (
                str(operation["owner_token"] or "") != owner_token
                or int(operation["owner_fence"] or 0) != owner_fence
            ):
                connection.rollback()
                raise RuntimeError(
                    f"operation {operation_key} is not owned by this worker"
                )
            evidence = _usage_evidence(usage, operation_kind="model")
            if int(evidence["model_calls"]) <= 0:
                connection.rollback()
                return False
            settlement_index = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(settlement_index), 0) + 1
                    FROM usage_settlement_events
                    WHERE operation_key = ? AND attempt_count = ?
                    """,
                    (operation_key, int(operation["attempt_count"])),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO usage_settlement_events(
                    operation_key, attempt_count, settlement_index,
                    model_calls, model_cache_hits, input_tokens, output_tokens,
                    estimated_cost_usd, usage_status, reason, provider,
                    pricing_status, pricing_reason, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    operation_key,
                    int(operation["attempt_count"]),
                    settlement_index,
                    evidence["model_calls"],
                    evidence["model_cache_hits"],
                    evidence["input_tokens"],
                    evidence["output_tokens"],
                    evidence["estimated_cost_usd"],
                    evidence["usage_status"],
                    evidence["usage_reason"],
                    evidence["provider"],
                    evidence["pricing_status"],
                    evidence["pricing_reason"],
                    settled_at,
                ),
            )
            connection.commit()
        return True

    def settle_model_usage(
        self,
        operation_key: str,
        usage: dict[str, Any],
    ) -> bool:
        """Persist returned model usage before result serialization finishes.

        The settlement is scoped to an operation attempt rather than the
        operation key alone. A user-approved retry may therefore retain the
        first paid response and add the retry's own measured usage.
        """

        settled_at = datetime.now(UTC).isoformat()
        owner_token = self._execution_owner_token or self._operation_owner_token
        owner_fence = self._execution_fence or 0
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.row_factory = sqlite3.Row
            operation = connection.execute(
                """
                SELECT kind, status, attempt_count, owner_token, owner_fence
                FROM operations WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if operation is None:
                connection.rollback()
                raise ValueError("cannot settle usage for a ghost operation")
            if str(operation["kind"] or "model") != "model":
                connection.rollback()
                raise ValueError("only model operations can settle model usage")
            if str(operation["status"] or "") != "started":
                connection.rollback()
                raise RuntimeError(
                    f"operation {operation_key} is not active for usage settlement"
                )
            if (
                str(operation["owner_token"] or "") != owner_token
                or int(operation["owner_fence"] or 0) != owner_fence
            ):
                connection.rollback()
                raise RuntimeError(
                    f"operation {operation_key} is not owned by this worker"
                )
            evidence = _usage_evidence(usage, operation_kind="model")
            cursor = connection.execute(
                """
                INSERT INTO usage_settlements(
                    operation_key, attempt_count, model_calls, model_cache_hits,
                    input_tokens, output_tokens, estimated_cost_usd,
                    usage_status, reason, provider, pricing_status,
                    pricing_reason, settled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_key, attempt_count) DO NOTHING
                """,
                (
                    operation_key,
                    int(operation["attempt_count"]),
                    evidence["model_calls"],
                    evidence["model_cache_hits"],
                    evidence["input_tokens"],
                    evidence["output_tokens"],
                    evidence["estimated_cost_usd"],
                    evidence["usage_status"],
                    evidence["usage_reason"],
                    evidence["provider"],
                    evidence["pricing_status"],
                    evidence["pricing_reason"],
                    settled_at,
                ),
            )
            connection.commit()
        return cursor.rowcount == 1

    def complete_operation(
        self,
        operation_key: str,
        result: Any,
        usage: dict[str, Any] | None = None,
    ) -> None:
        completed_at = datetime.now(UTC).isoformat()
        owner_token = self._execution_owner_token or self._operation_owner_token
        owner_fence = self._execution_fence or 0
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            operation_kind_row = connection.execute(
                "SELECT kind FROM operations WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
            if operation_kind_row is None:
                connection.rollback()
                raise ValueError("cannot complete a ghost operation")
            operation_kind = str(operation_kind_row[0] or "model")
            usage_evidence = _usage_evidence(
                usage,
                operation_kind=operation_kind,
            )
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = 'succeeded', completed_at = ?, result_json = ?,
                    error = NULL, side_effect_status = 'committed',
                    result_invocation_id = last_invocation_id
                WHERE operation_key = ? AND status = 'started'
                  AND owner_token = ? AND COALESCE(owner_fence, 0) = ?
                """,
                (
                    completed_at,
                    json.dumps(result, ensure_ascii=False),
                    operation_key,
                    owner_token,
                    owner_fence,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError(f"operation {operation_key} is not in started state")
            if usage is not None or operation_kind == "model":
                connection.execute(
                    """
                    INSERT INTO usage_ledger(
                        operation_key, model_calls, model_cache_hits,
                        input_tokens, output_tokens, estimated_cost_usd,
                        usage_status, reason, usage_reason, provider,
                        pricing_status, pricing_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation_key,
                        usage_evidence["model_calls"],
                        usage_evidence["model_cache_hits"],
                        usage_evidence["input_tokens"],
                        usage_evidence["output_tokens"],
                        usage_evidence["estimated_cost_usd"],
                        usage_evidence["usage_status"],
                        usage_evidence["usage_reason"],
                        usage_evidence["usage_reason"],
                        usage_evidence["provider"],
                        usage_evidence["pricing_status"],
                        usage_evidence["pricing_reason"],
                    ),
                )
            connection.commit()

    def fail_operation(self, operation_key: str, error: str) -> None:
        owner_token = self._execution_owner_token or self._operation_owner_token
        owner_fence = self._execution_fence or 0
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.execute(
                """
                UPDATE operations
                SET status = 'failed', completed_at = ?, error = ?,
                    side_effect_status = 'not_committed'
                WHERE operation_key = ? AND status = 'started'
                  AND owner_token = ? AND COALESCE(owner_fence, 0) = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    error[:2000],
                    operation_key,
                    owner_token,
                    owner_fence,
                ),
            )
            connection.commit()

    def mark_external_outcome_unknown(
        self,
        operation_key: str,
        *,
        invocation_id: str | None = None,
        error: str = "execution fence was lost after the provider call started",
    ) -> bool:
        """Fence an operation as unknown without letting a stale worker write success.

        This is intentionally the one stale-owner write path. It only changes a
        still-started operation whose owner/fence exactly match this store. A
        newer owner therefore cannot be overwritten by a late HTTP response.
        """

        owner_token = self._execution_owner_token
        owner_fence = self._execution_fence
        if not owner_token or owner_fence is None or int(owner_fence) <= 0:
            return False
        now = datetime.now(UTC).isoformat()
        message = f"external_outcome_unknown: {str(error)[:1800]}"
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT status, owner_token, owner_fence, last_invocation_id
                FROM operations WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if (
                row is None
                or str(row[0]) != "started"
                or str(row[1] or "") != owner_token
                or int(row[2] or 0) != int(owner_fence)
            ):
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                UPDATE operations
                SET status = 'external_outcome_unknown', completed_at = ?,
                    error = ?, side_effect_status = 'unknown'
                WHERE operation_key = ? AND status = 'started'
                  AND owner_token = ? AND COALESCE(owner_fence, 0) = ?
                """,
                (now, message, operation_key, owner_token, int(owner_fence)),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return False
            target_id = invocation_id or str(row[3] or "") or None
            if target_id:
                invocation_row = connection.execute(
                    """
                    SELECT invocation_json FROM agent_invocations
                    WHERE invocation_id = ? AND operation_key = ? AND status = 'running'
                    """,
                    (target_id, operation_key),
                ).fetchone()
                if invocation_row is not None:
                    try:
                        invocation_json = json.loads(str(invocation_row[0]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        invocation_json = {}
                    if isinstance(invocation_json, dict):
                        invocation_json["status"] = "failed"
                        invocation_json["ended_at"] = now
                        invocation_json["error"] = message[:1000]
                        invocation_json["side_effect_status"] = "unknown"
                        outcome = _invocation_outcome(invocation_json)
                        outcome_json = json.dumps(
                            outcome,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        connection.execute(
                            """
                            UPDATE agent_invocations
                            SET status = 'failed', side_effect_status = 'unknown',
                                invocation_json = ?, outcome_json = ?,
                                outcome_hash = ?, updated_at = ?
                            WHERE invocation_id = ? AND operation_key = ?
                              AND status = 'running'
                            """,
                            (
                                json.dumps(invocation_json, ensure_ascii=False, sort_keys=True),
                                outcome_json,
                                _invocation_outcome_hash(outcome),
                                now,
                                target_id,
                                operation_key,
                            ),
                        )
            connection.commit()
        return True

    def authorize_operation_retry(self, operation_key: str) -> bool:
        """Explicitly unlock an ambiguous non-idempotent operation for retry."""
        return self.authorize_operation_retries([operation_key])

    def authorize_operation_retries(self, operation_keys: list[str]) -> bool:
        """Atomically unlock the complete confirmed set of ambiguous operations."""
        keys = list(dict.fromkeys(operation_keys))
        if not keys:
            return True
        placeholders = ",".join("?" for _ in keys)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                f"""
                SELECT COUNT(*) FROM operations
                WHERE operation_key IN ({placeholders})
                  AND (
                      (status = 'started' AND idempotent = 0)
                      OR status = 'external_outcome_unknown'
                  )
                """,
                keys,
            ).fetchone()[0]
            if int(count) != len(keys):
                connection.rollback()
                return False
            cursor = connection.execute(
                f"""
                UPDATE operations
                SET status = 'failed', completed_at = ?,
                    error = 'manual retry authorized by user'
                WHERE operation_key IN ({placeholders})
                  AND (
                      (status = 'started' AND idempotent = 0)
                      OR status = 'external_outcome_unknown'
                  )
                """,
                (datetime.now(UTC).isoformat(), *keys),
            )
            connection.commit()
        return cursor.rowcount == len(keys)

    def ambiguous_operations(self) -> list[dict[str, Any]]:
        """Return operations whose provider outcome is unsafe to retry silently."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT operation_key, node, kind, attempt_count, started_at,
                       original_invocation_id, side_effect_status,
                       owner_fence, lease_expires_at_ms, status, idempotent
                FROM operations
                WHERE (
                    status = 'started' AND idempotent = 0
                ) OR status = 'external_outcome_unknown'
                ORDER BY started_at
                """
            ).fetchall()
        return [
            {
                "operation_key": row[0],
                "node": row[1],
                "kind": row[2],
                "status": str(row[9] or "started"),
                "idempotent": bool(row[10]),
                "attempt_count": int(row[3]),
                "started_at": row[4],
                "original_invocation_id": row[5],
                "side_effect_status": str(row[6] or "unknown"),
                "owner_fence": int(row[7] or 0),
                "lease_expires_at_ms": (
                    int(row[8]) if row[8] is not None else None
                ),
            }
            for row in rows
        ]

    def usage_totals(self) -> dict[str, Any]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            # Response-level events are the source of truth while an operation
            # is running.  Once its final operation summary exists, use that
            # summary if the append-only receipt set is incomplete (for
            # example a transient database lock delayed one live receipt).
            # This preserves immediate updates without under-counting a
            # completed multi-call operation.
            accounted_usage = """
                WITH response_rollups AS (
                    SELECT operation_key, attempt_count,
                           COALESCE(SUM(model_calls), 0) AS model_calls,
                           COALESCE(SUM(model_cache_hits), 0) AS model_cache_hits,
                           COALESCE(SUM(input_tokens), 0) AS input_tokens,
                           COALESCE(SUM(output_tokens), 0) AS output_tokens,
                           COALESCE(SUM(estimated_cost_usd), 0.0) AS estimated_cost_usd
                    FROM usage_settlement_events
                    GROUP BY operation_key, attempt_count
                ),
                summary_mismatches AS (
                    SELECT settlements.operation_key, settlements.attempt_count
                    FROM usage_settlements AS settlements
                    LEFT JOIN response_rollups AS receipts
                      ON receipts.operation_key = settlements.operation_key
                     AND receipts.attempt_count = settlements.attempt_count
                    WHERE receipts.operation_key IS NULL
                       OR receipts.model_calls != settlements.model_calls
                       OR receipts.model_cache_hits != settlements.model_cache_hits
                       OR receipts.input_tokens != settlements.input_tokens
                       OR receipts.output_tokens != settlements.output_tokens
                       OR ABS(receipts.estimated_cost_usd - settlements.estimated_cost_usd) > 0.0000000001
                ),
                accounted_usage AS (
                    SELECT events.operation_key, events.attempt_count,
                           events.model_calls, events.model_cache_hits,
                           events.input_tokens, events.output_tokens,
                           events.estimated_cost_usd, events.usage_status,
                           events.reason, events.provider, events.pricing_status,
                           events.pricing_reason, events.settled_at,
                           'response_receipt' AS accounting_source
                    FROM usage_settlement_events AS events
                    WHERE NOT EXISTS (
                        SELECT 1 FROM summary_mismatches
                        WHERE summary_mismatches.operation_key = events.operation_key
                          AND summary_mismatches.attempt_count = events.attempt_count
                    )
                    UNION ALL
                    SELECT settlements.operation_key, settlements.attempt_count,
                           settlements.model_calls, settlements.model_cache_hits,
                           settlements.input_tokens, settlements.output_tokens,
                           settlements.estimated_cost_usd, settlements.usage_status,
                           settlements.reason, settlements.provider,
                           settlements.pricing_status, settlements.pricing_reason,
                           settlements.settled_at,
                           'operation_summary' AS accounting_source
                    FROM usage_settlements AS settlements
                    WHERE EXISTS (
                        SELECT 1 FROM summary_mismatches
                        WHERE summary_mismatches.operation_key = settlements.operation_key
                          AND summary_mismatches.attempt_count = settlements.attempt_count
                    )
                    UNION ALL
                    SELECT usage_ledger.operation_key, operations.attempt_count,
                           usage_ledger.model_calls,
                           usage_ledger.model_cache_hits,
                           usage_ledger.input_tokens, usage_ledger.output_tokens,
                           usage_ledger.estimated_cost_usd,
                           usage_ledger.usage_status,
                           COALESCE(usage_ledger.reason, usage_ledger.usage_reason),
                           usage_ledger.provider, usage_ledger.pricing_status,
                           usage_ledger.pricing_reason,
                           COALESCE(operations.completed_at, ''),
                           'completed_ledger' AS accounting_source
                    FROM usage_ledger
                    JOIN operations USING(operation_key)
                    WHERE NOT EXISTS (
                        SELECT 1 FROM usage_settlements
                        WHERE usage_settlements.operation_key = usage_ledger.operation_key
                          AND usage_settlements.attempt_count = operations.attempt_count
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM usage_settlement_events
                        WHERE usage_settlement_events.operation_key = usage_ledger.operation_key
                          AND usage_settlement_events.attempt_count = operations.attempt_count
                    )
                )
            """
            ledger_count = int(
                connection.execute(
                    accounted_usage + "SELECT COUNT(*) FROM accounted_usage"
                ).fetchone()[0]
            )
            row = connection.execute(
                accounted_usage
                + """
                SELECT COALESCE(SUM(model_calls), 0),
                       COALESCE(SUM(model_cache_hits), 0),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(estimated_cost_usd), 0.0)
                FROM accounted_usage
                """
            ).fetchone()
            latest_updated_at = connection.execute(
                accounted_usage
                + """
                SELECT MAX(NULLIF(settled_at, ''))
                FROM accounted_usage
                """
            ).fetchone()[0]
            latest_entry = connection.execute(
                accounted_usage
                + """
                SELECT provider, model_calls, input_tokens, output_tokens,
                       estimated_cost_usd, pricing_status, settled_at,
                       accounting_source
                FROM accounted_usage
                ORDER BY NULLIF(settled_at, '') DESC, operation_key DESC
                LIMIT 1
                """
            ).fetchone()
            provider_rows = connection.execute(
                accounted_usage
                + """
                SELECT provider,
                       COALESCE(SUM(model_calls), 0),
                       COALESCE(SUM(input_tokens), 0),
                       COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(estimated_cost_usd), 0.0),
                       COUNT(*),
                       MAX(NULLIF(settled_at, '')),
                       CASE
                           WHEN SUM(CASE WHEN usage_status != 'not_applicable' THEN 1 ELSE 0 END) = 0
                               THEN 'not_applicable'
                           WHEN SUM(CASE WHEN usage_status = 'complete' THEN 1 ELSE 0 END)
                                = SUM(CASE WHEN usage_status != 'not_applicable' THEN 1 ELSE 0 END)
                               THEN 'complete'
                           WHEN SUM(CASE WHEN usage_status IN ('complete', 'partial') THEN 1 ELSE 0 END) > 0
                               THEN 'partial'
                           ELSE 'unavailable'
                       END,
                       CASE
                           WHEN SUM(CASE WHEN pricing_status != 'not_applicable' THEN 1 ELSE 0 END) = 0
                               THEN 'not_applicable'
                           WHEN SUM(CASE WHEN pricing_status = 'complete' THEN 1 ELSE 0 END)
                                = SUM(CASE WHEN pricing_status != 'not_applicable' THEN 1 ELSE 0 END)
                               THEN 'complete'
                           WHEN SUM(CASE WHEN pricing_status IN ('complete', 'partial') THEN 1 ELSE 0 END) > 0
                               THEN 'partial'
                           ELSE 'unavailable'
                       END,
                       GROUP_CONCAT(DISTINCT reason),
                       GROUP_CONCAT(DISTINCT pricing_reason)
                FROM accounted_usage
                GROUP BY provider
                ORDER BY provider
                """
            ).fetchall()
            reconciled_operations = int(
                connection.execute(
                    accounted_usage + "SELECT COUNT(*) FROM summary_mismatches"
                ).fetchone()[0]
            )
            pending_model_operations, settled_model_operations = connection.execute(
                """
                WITH settled_operations AS (
                    SELECT DISTINCT operation_key, attempt_count
                    FROM usage_settlement_events
                    UNION
                    SELECT operation_key, attempt_count
                    FROM usage_settlements
                )
                SELECT
                    COALESCE(SUM(CASE WHEN settled_operations.operation_key IS NULL THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN settled_operations.operation_key IS NOT NULL THEN 1 ELSE 0 END), 0)
                FROM operations
                LEFT JOIN settled_operations
                  ON settled_operations.operation_key = operations.operation_key
                 AND settled_operations.attempt_count = operations.attempt_count
                WHERE operations.status = 'started' AND operations.kind = 'model'
                """
            ).fetchone()
            response_settlement_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM usage_settlement_events"
                ).fetchone()[0]
            )
            usage_revision = int(
                connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM usage_settlement_events)
                        + (SELECT COUNT(*) FROM usage_settlements)
                        + (SELECT COUNT(*) FROM usage_ledger)
                    """
                ).fetchone()[0]
            )
            evidence_rows = connection.execute(
                accounted_usage
                + """
                SELECT usage_status, reason, provider,
                       pricing_status, pricing_reason
                FROM accounted_usage
                ORDER BY operation_key
                """
            ).fetchall()
        usage_statuses = [str(row[0] or "unavailable") for row in evidence_rows]
        pricing_statuses = [str(row[3] or "unavailable") for row in evidence_rows]
        usage_status = _aggregate_evidence_status(usage_statuses)
        pricing_status = _aggregate_evidence_status(pricing_statuses)
        providers = sorted({str(row[2] or "unknown") for row in evidence_rows})
        usage_reasons = [str(row[1] or "") for row in evidence_rows if row[1]]
        pricing_reasons = [str(row[4] or "") for row in evidence_rows if row[4]]
        provider_breakdown = [
            {
                "provider": str(item[0] or "unknown"),
                "model_calls": int(item[1]),
                "input_tokens": int(item[2]),
                "output_tokens": int(item[3]),
                "estimated_cost_usd": float(item[4]),
                "ledger_entry_count": int(item[5]),
                "updated_at": str(item[6]) if item[6] else None,
                "usage_status": str(item[7] or "unavailable"),
                "pricing_status": str(item[8] or "unavailable"),
                "usage_reason": str(item[9] or ""),
                "pricing_reason": str(item[10] or ""),
            }
            for item in provider_rows
        ]
        latest_usage_entry = (
            {
                "provider": str(latest_entry[0] or "unknown"),
                "model_calls": int(latest_entry[1]),
                "input_tokens": int(latest_entry[2]),
                "output_tokens": int(latest_entry[3]),
                "estimated_cost_usd": float(latest_entry[4]),
                "pricing_status": str(latest_entry[5] or "unavailable"),
                "updated_at": str(latest_entry[6]) if latest_entry[6] else None,
                "accounting_source": str(latest_entry[7] or "unknown"),
            }
            if latest_entry is not None
            else None
        )
        return {
            "model_calls": int(row[0]),
            "model_cache_hits": int(row[1]),
            "input_tokens": int(row[2]),
            "output_tokens": int(row[3]),
            "estimated_cost_usd": float(row[4]),
            "ledger_entry_count": ledger_count,
            "usage_revision": usage_revision,
            "usage_status": usage_status,
            "reason": "; ".join(dict.fromkeys(usage_reasons)),
            "usage_reason": "; ".join(dict.fromkeys(usage_reasons)),
            "provider": providers[0] if len(providers) == 1 else ",".join(providers),
            "providers": ",".join(providers),
            "pricing_status": pricing_status,
            "pricing_reason": "; ".join(dict.fromkeys(pricing_reasons)),
            # These fields make the live UI distinguish the last durable
            # settlement from a model request that is still in flight.
            "updated_at": str(latest_updated_at) if latest_updated_at else None,
            "pending_model_operations": int(pending_model_operations),
            "settled_model_operations": int(settled_model_operations),
            "settled_model_responses": response_settlement_count,
            "reconciled_model_operations": reconciled_operations,
            "provider_breakdown": provider_breakdown,
            "latest_entry": latest_usage_entry,
        }

    def published_event_count(self) -> int:
        """Return the durable number of events published to the JSONL stream."""

        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE published_at IS NOT NULL"
            ).fetchone()
        return int(row[0])

    def ensure_legacy_usage_baseline(self, counters: Counters) -> None:
        """Seed old checkpoint counters once before operation-ledger accounting."""
        if not any(
            (
                counters.model_calls,
                counters.model_cache_hits,
                counters.input_tokens,
                counters.output_tokens,
                counters.estimated_cost_usd,
            )
        ):
            return
        operation_key = f"legacy-usage-baseline:{self.run_id}"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            row = connection.execute(
                "SELECT 1 FROM usage_ledger LIMIT 1"
            ).fetchone()
            if row is not None:
                connection.commit()
                return
            now = datetime.now(UTC).isoformat()
            connection.execute(
                """
                INSERT OR IGNORE INTO operations(
                    operation_key, node, semantic_input_hash, status,
                    started_at, completed_at, result_json
                ) VALUES (?, 'legacy_usage', 'legacy', 'succeeded', ?, ?, '{}')
                """,
                (operation_key, now, now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO usage_ledger(
                    operation_key, model_calls, model_cache_hits,
                    input_tokens, output_tokens, estimated_cost_usd,
                    usage_status, reason, usage_reason, provider,
                    pricing_status, pricing_reason
                ) VALUES (?, ?, ?, ?, ?, ?, 'partial', ?, ?, ?, 'unavailable', ?)
                """,
                (
                    operation_key,
                    counters.model_calls,
                    counters.model_cache_hits,
                    counters.input_tokens,
                    counters.output_tokens,
                    counters.estimated_cost_usd,
                    "Historical checkpoint counters lack per-operation usage evidence.",
                    "Historical checkpoint counters lack per-operation usage evidence.",
                    "legacy_unknown",
                    "Historical baseline has no verified token pricing.",
                ),
            )
            connection.commit()

    def operation_rows(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM operations ORDER BY started_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def operation_detail(self, operation_key: str) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                WITH live_usage AS (
                    SELECT operation_key, attempt_count,
                           SUM(model_calls) AS model_calls,
                           SUM(model_cache_hits) AS model_cache_hits,
                           SUM(input_tokens) AS input_tokens,
                           SUM(output_tokens) AS output_tokens,
                           SUM(estimated_cost_usd) AS estimated_cost_usd,
                           CASE
                               WHEN SUM(CASE WHEN usage_status != 'not_applicable' THEN 1 ELSE 0 END) = 0
                                   THEN 'not_applicable'
                               WHEN SUM(CASE WHEN usage_status = 'complete' THEN 1 ELSE 0 END) = COUNT(*)
                                   THEN 'complete'
                               WHEN SUM(CASE WHEN usage_status IN ('complete', 'partial') THEN 1 ELSE 0 END) > 0
                                   THEN 'partial'
                               ELSE 'unavailable'
                           END AS usage_status,
                           GROUP_CONCAT(DISTINCT reason) AS reason,
                           GROUP_CONCAT(DISTINCT provider) AS provider,
                           CASE
                               WHEN SUM(CASE WHEN pricing_status != 'not_applicable' THEN 1 ELSE 0 END) = 0
                                   THEN 'not_applicable'
                               WHEN SUM(CASE WHEN pricing_status = 'complete' THEN 1 ELSE 0 END) = COUNT(*)
                                   THEN 'complete'
                               WHEN SUM(CASE WHEN pricing_status IN ('complete', 'partial') THEN 1 ELSE 0 END) > 0
                                   THEN 'partial'
                               ELSE 'unavailable'
                           END AS pricing_status,
                           GROUP_CONCAT(DISTINCT pricing_reason) AS pricing_reason,
                           MAX(settled_at) AS settled_at,
                           COUNT(*) AS settlement_count
                    FROM usage_settlement_events
                    GROUP BY operation_key, attempt_count
                )
                SELECT operations.*,
                       COALESCE(live_usage.model_calls, usage_settlements.model_calls, usage_ledger.model_calls, 0) AS model_calls,
                       COALESCE(live_usage.model_cache_hits, usage_settlements.model_cache_hits, usage_ledger.model_cache_hits, 0) AS model_cache_hits,
                       COALESCE(live_usage.input_tokens, usage_settlements.input_tokens, usage_ledger.input_tokens, 0) AS input_tokens,
                       COALESCE(live_usage.output_tokens, usage_settlements.output_tokens, usage_ledger.output_tokens, 0) AS output_tokens,
                       COALESCE(live_usage.estimated_cost_usd, usage_settlements.estimated_cost_usd, usage_ledger.estimated_cost_usd, 0.0) AS estimated_cost_usd,
                       COALESCE(live_usage.usage_status, usage_settlements.usage_status, usage_ledger.usage_status) AS usage_status,
                       COALESCE(live_usage.reason, usage_settlements.reason, usage_ledger.reason, usage_ledger.usage_reason) AS reason,
                       usage_ledger.usage_reason,
                       COALESCE(live_usage.provider, usage_settlements.provider, usage_ledger.provider) AS usage_provider,
                       COALESCE(live_usage.pricing_status, usage_settlements.pricing_status, usage_ledger.pricing_status) AS pricing_status,
                       COALESCE(live_usage.pricing_reason, usage_settlements.pricing_reason, usage_ledger.pricing_reason) AS pricing_reason,
                       COALESCE(live_usage.settled_at, usage_settlements.settled_at) AS usage_settled_at,
                       COALESCE(live_usage.settlement_count, 0) AS live_usage_settlement_count
                FROM operations
                LEFT JOIN usage_ledger USING(operation_key)
                LEFT JOIN live_usage
                  ON live_usage.operation_key = operations.operation_key
                 AND live_usage.attempt_count = operations.attempt_count
                LEFT JOIN usage_settlements
                  ON usage_settlements.operation_key = operations.operation_key
                 AND usage_settlements.attempt_count = operations.attempt_count
                WHERE operations.operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def preview_source_fetch_record_id(
        self,
        *,
        source_id: str,
        operation_key: str,
        invocation_id: str,
        status: str,
        attempt: int,
    ) -> str:
        """Return the immutable fetch ID before its snapshot is written."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
            raise ValueError("invalid source id")
        if status not in {"fetched", "failed", "unknown"}:
            raise ValueError("source fetch status is invalid")
        if int(attempt) <= 0:
            raise ValueError("source fetch attempt must be positive")
        return _source_fetch_record_id(
            self.run_id,
            source_id,
            operation_key,
            invocation_id,
            status,
            int(attempt),
        )

    def record_source_fetch(
        self,
        *,
        source_id: str,
        requested_url: str,
        operation_key: str,
        invocation_id: str,
        result_invocation_id: str | None,
        execution_mode: str,
        provider: str,
        fetch_mode: str,
        status: str,
        attempt: int,
        final_url: str | None = None,
        content_hash: str | None = None,
        content_hash_scope: str | None = None,
        snapshot_sha256: str | None = None,
        error: str | None = None,
        fetched_at: str | None = None,
    ) -> dict[str, Any]:
        """Bind one source outcome to its durable fetch operation and invocation."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
            raise ValueError("invalid source id")
        if execution_mode not in {"executed", "replayed"}:
            raise ValueError("source fetch execution mode is invalid")
        if status not in {"fetched", "failed", "unknown"}:
            raise ValueError("source fetch status is invalid")
        if not str(provider).strip():
            raise ValueError("source fetch provider is required")
        if int(attempt) <= 0:
            raise ValueError("source fetch attempt must be positive")
        canonical_requested_url = _canonical_source_fetch_url(requested_url)
        normalized_content_hash_scope = (
            str(content_hash_scope or "unknown").strip() or "unknown"
        )
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            operation = connection.execute(
                """
                SELECT node, kind, status, semantic_input_hash,
                       result_invocation_id
                FROM operations WHERE operation_key = ?
                """,
                (operation_key,),
            ).fetchone()
            if (
                operation is None
                or str(operation["node"]) != "fetch"
                or str(operation["kind"]) != "fetch"
            ):
                raise ValueError("source fetch references a non-fetch operation")

            def load_invocation(value: str) -> tuple[AgentInvocation, dict[str, Any]]:
                row = connection.execute(
                    "SELECT * FROM agent_invocations WHERE invocation_id = ?",
                    (value,),
                ).fetchone()
                if row is None:
                    raise ValueError("source fetch references a ghost invocation")
                projected, validation = _project_invocation_row(dict(row))
                if (
                    validation["stored_identity_hash"]
                    != validation["recomputed_identity_hash"]
                    or validation["stored_outcome_hash"]
                    != validation["recomputed_outcome_hash"]
                ):
                    raise ValueError(
                        "source fetch invocation canonical record is inconsistent"
                    )
                return projected, validation

            invocation, invocation_validation = load_invocation(invocation_id)
            if (
                invocation.run_id != self.run_id
                or invocation.trace_id != self.run_id
                or invocation.operation_key != operation_key
                or invocation.operation != "fetch"
                or invocation.execution_mode != execution_mode
                or int(invocation.attempt) != int(attempt)
            ):
                raise ValueError("source fetch invocation binding is inconsistent")

            durable_result_invocation_id = (
                str(operation["result_invocation_id"] or "") or None
            )
            expected_operation_status = {
                "fetched": "succeeded",
                "failed": "failed",
                "unknown": "started",
            }[status]
            if str(operation["status"]) != expected_operation_status:
                raise ValueError("source fetch operation status is inconsistent")
            if status == "fetched" and invocation.status != "succeeded":
                raise ValueError("successful source fetch must have a successful invocation")
            if status in {"failed", "unknown"} and invocation.status not in {
                "failed",
                "running",
            }:
                raise ValueError("failed source fetch has an incompatible invocation status")
            if result_invocation_id != durable_result_invocation_id:
                raise ValueError("source fetch result invocation binding is inconsistent")

            semantic_invocation = invocation
            semantic_validation = invocation_validation
            if execution_mode == "replayed":
                if invocation.replay_of_invocation_id != durable_result_invocation_id:
                    raise ValueError(
                        "source fetch replay does not point to the durable result invocation"
                    )
                semantic_invocation, semantic_validation = load_invocation(
                    str(durable_result_invocation_id or "")
                )
                if (
                    semantic_invocation.run_id != self.run_id
                    or semantic_invocation.trace_id != self.run_id
                    or semantic_invocation.operation_key != operation_key
                    or semantic_invocation.operation != "fetch"
                    or semantic_invocation.execution_mode != "executed"
                    or semantic_invocation.status != "succeeded"
                ):
                    raise ValueError(
                        "source fetch replay semantic invocation is inconsistent"
                    )

            semantic_input: dict[str, Any] | None
            try:
                parsed_input = json.loads(semantic_invocation.input_summary)
                semantic_input = parsed_input if isinstance(parsed_input, dict) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                semantic_input = None

            missing_fields = {"source_id", "requested_url", "provider"}
            if semantic_input is not None:
                semantic_hash = hashlib.sha256(
                    _canonical_json(semantic_input).encode("utf-8")
                ).hexdigest()
                if semantic_hash != str(operation["semantic_input_hash"]):
                    raise ValueError(
                        "source fetch invocation input does not match the operation"
                    )
                missing_fields = {
                    field
                    for field in missing_fields
                    if field not in semantic_input
                    or semantic_input[field] is None
                    or semantic_input[field] == ""
                }
                if "requested_url" in semantic_input:
                    semantic_url = _canonical_source_fetch_url(
                        str(semantic_input["requested_url"])
                    )
                    if semantic_url != canonical_requested_url:
                        raise ValueError(
                            "source fetch requested URL does not match invocation input"
                        )
                if (
                    "source_id" in semantic_input
                    and str(semantic_input["source_id"]) != source_id
                ):
                    raise ValueError(
                        "source fetch source id does not match invocation input"
                    )
                if (
                    "provider" in semantic_input
                    and str(semantic_input["provider"]) != provider
                ):
                    raise ValueError(
                        "source fetch provider does not match invocation input"
                    )

            server_bound = bool(
                semantic_input is not None
                and not missing_fields
                and semantic_validation["identity_version"]
                == INVOCATION_IDENTITY_VERSION
            )
            binding_status = "server_bound" if server_bound else "legacy_unverified"
            validation_reason = (
                "Canonical invocation input, operation hash, source, URL, provider, attempt, and execution mode agree."
                if server_bound
                else "Canonical historical invocation input lacks fields required for a server-bound source assertion."
            )
            binding = {
                "version": SOURCE_FETCH_BINDING_VERSION,
                "run_id": self.run_id,
                "source_id": source_id,
                "canonical_requested_url": canonical_requested_url,
                "operation_key": operation_key,
                "invocation_id": invocation_id,
                "semantic_invocation_id": semantic_invocation.invocation_id,
                "result_invocation_id": durable_result_invocation_id,
                "execution_mode": execution_mode,
                "provider": provider,
                "fetch_mode": fetch_mode,
                "status": status,
                "attempt": int(attempt),
                "content_hash": content_hash or None,
                "content_hash_scope": normalized_content_hash_scope,
                "snapshot_sha256": snapshot_sha256 or None,
                "semantic_input_fields": sorted(semantic_input or {}),
                "missing_semantic_fields": sorted(missing_fields),
                "validation_reason": validation_reason,
            }
            binding_json = _canonical_json(binding)
            binding_digest = _source_fetch_binding_digest(binding)
            fetch_record_id = _source_fetch_record_id(
                self.run_id,
                source_id,
                operation_key,
                invocation_id,
                status,
                int(attempt),
            )
            now = datetime.now(UTC).isoformat()
            existing = connection.execute(
                """
                SELECT * FROM source_fetches WHERE fetch_record_id = ?
                """,
                (fetch_record_id,),
            ).fetchone()
            if existing is not None:
                try:
                    existing_canonical_url = str(
                        existing["canonical_requested_url"]
                        or _canonical_source_fetch_url(str(existing["requested_url"]))
                    )
                except ValueError as error:
                    raise ValueError(
                        "durable source fetch requested URL is invalid"
                    ) from error
                expected_existing = {
                    "run_id": self.run_id,
                    "source_id": source_id,
                    "canonical_requested_url": canonical_requested_url,
                    "operation_key": operation_key,
                    "invocation_id": invocation_id,
                    "result_invocation_id": result_invocation_id,
                    "execution_mode": execution_mode,
                    "provider": provider,
                    "fetch_mode": fetch_mode,
                    "status": status,
                    "attempt": int(attempt),
                    "final_url": final_url,
                    "content_hash": content_hash,
                    "content_hash_scope": normalized_content_hash_scope,
                    "snapshot_sha256": snapshot_sha256,
                    "error": error,
                    "fetched_at": fetched_at,
                }
                durable_existing = {
                    key: existing[key] for key in expected_existing
                }
                durable_existing["attempt"] = int(durable_existing["attempt"])
                durable_existing["canonical_requested_url"] = existing_canonical_url
                if durable_existing != expected_existing:
                    raise ValueError("durable source fetch binding is inconsistent")
                if (
                    str(existing["binding_status"]) == "server_bound"
                    and (
                        str(existing["binding_version"])
                        != SOURCE_FETCH_BINDING_VERSION
                        or str(existing["binding_json"]) != binding_json
                        or str(existing["binding_digest"]) != binding_digest
                    )
                ):
                    raise ValueError("durable source fetch digest is inconsistent")
                connection.commit()
                return {
                    "fetch_record_id": fetch_record_id,
                    "binding_status": str(existing["binding_status"]),
                    "recorded_at": str(existing["recorded_at"]),
                }

            connection.execute(
                """
                INSERT INTO source_fetches(
                    fetch_record_id, run_id, source_id, requested_url,
                    canonical_requested_url, final_url,
                    operation_key, invocation_id, result_invocation_id,
                    execution_mode, provider, fetch_mode, status, attempt,
                    content_hash, content_hash_scope, snapshot_sha256,
                    error, fetched_at,
                    binding_version, binding_json, binding_digest,
                    binding_status, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fetch_record_id,
                    self.run_id,
                    source_id,
                    canonical_requested_url,
                    canonical_requested_url,
                    final_url,
                    operation_key,
                    invocation_id,
                    result_invocation_id,
                    execution_mode,
                    provider,
                    fetch_mode,
                    status,
                    int(attempt),
                    content_hash,
                    normalized_content_hash_scope,
                    snapshot_sha256,
                    error,
                    fetched_at,
                    SOURCE_FETCH_BINDING_VERSION,
                    binding_json,
                    binding_digest,
                    binding_status,
                    now,
                ),
            )
            connection.commit()
        return {
            "fetch_record_id": fetch_record_id,
            "binding_status": binding_status,
            "recorded_at": now,
        }

    def _source_fetch_snapshot_integrity(
        self, item: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Return (available, hash_matches) for a successful immutable fetch."""
        if str(item.get("status") or "") != "fetched":
            return True, True
        fetch_record_id = str(item.get("fetch_record_id") or "").strip()
        expected = str(item.get("snapshot_sha256") or "").strip().lower()
        if not fetch_record_id or not re.fullmatch(r"[0-9a-f]{64}", expected):
            return False, False
        path = self.run_dir / "sources" / f"{fetch_record_id}.txt"
        try:
            actual = hashlib.sha256(path.read_bytes()).hexdigest().lower()
        except (OSError, ValueError):
            return False, False
        return True, actual == expected

    def source_fetch_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM source_fetches ORDER BY recorded_at, fetch_record_id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            binding: dict[str, Any] | None = None
            try:
                parsed = json.loads(str(item.get("binding_json") or ""))
                if isinstance(parsed, dict):
                    binding = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            digest_valid = bool(
                binding is not None
                and str(item.get("binding_digest") or "")
                == _source_fetch_binding_digest(binding)
            )
            fields_match = bool(
                binding is not None
                and str(binding.get("run_id") or "") == self.run_id
                and str(binding.get("source_id") or "") == str(item["source_id"])
                and str(binding.get("canonical_requested_url") or "")
                == str(item["canonical_requested_url"])
                and str(binding.get("operation_key") or "")
                == str(item["operation_key"])
                and str(binding.get("invocation_id") or "")
                == str(item["invocation_id"])
                and str(binding.get("result_invocation_id") or "")
                == str(item.get("result_invocation_id") or "")
                and str(binding.get("execution_mode") or "")
                == str(item["execution_mode"])
                and str(binding.get("provider") or "") == str(item["provider"])
                and str(binding.get("fetch_mode") or "")
                == str(item.get("fetch_mode") or "")
                and str(binding.get("status") or "")
                == str(item.get("status") or "")
                and int(binding.get("attempt") or 0) == int(item["attempt"])
                and str(binding.get("content_hash") or "")
                == str(item.get("content_hash") or "")
                and str(binding.get("content_hash_scope") or "unknown")
                == str(item.get("content_hash_scope") or "unknown")
                and str(binding.get("snapshot_sha256") or "")
                == str(item.get("snapshot_sha256") or "")
            )
            item["binding"] = binding
            item["binding_digest_valid"] = digest_valid
            item["binding_fields_match"] = fields_match
            snapshot_available, snapshot_hash_valid = (
                self._source_fetch_snapshot_integrity(item)
            )
            item["snapshot_available"] = snapshot_available
            item["snapshot_hash_valid"] = snapshot_hash_valid
            item["binding_valid"] = bool(
                item["binding_status"] == "server_bound"
                and item["binding_version"] == SOURCE_FETCH_BINDING_VERSION
                and digest_valid
                and fields_match
                and snapshot_hash_valid
            )
            result.append(item)
        return result

    def source_fetch_audit_page(
        self,
        *,
        limit: int = 50,
        after: int = 0,
    ) -> dict[str, Any]:
        """Read fetch bindings by immutable SQLite rowid keyset."""
        limit = self._audit_limit(limit)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT rowid AS _audit_rowid, * FROM source_fetches
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (max(0, int(after)), limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        visible = rows[:limit]
        result: list[dict[str, Any]] = []
        for row in visible:
            item = dict(row)
            item.pop("_audit_rowid", None)
            binding: dict[str, Any] | None = None
            try:
                parsed = json.loads(str(item.get("binding_json") or ""))
                if isinstance(parsed, dict):
                    binding = parsed
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            digest_valid = bool(
                binding is not None
                and str(item.get("binding_digest") or "")
                == _source_fetch_binding_digest(binding)
            )
            fields_match = bool(
                binding is not None
                and str(binding.get("run_id") or "") == self.run_id
                and str(binding.get("source_id") or "") == str(item["source_id"])
                and str(binding.get("canonical_requested_url") or "")
                == str(item["canonical_requested_url"])
                and str(binding.get("operation_key") or "")
                == str(item["operation_key"])
                and str(binding.get("invocation_id") or "")
                == str(item["invocation_id"])
                and str(binding.get("result_invocation_id") or "")
                == str(item.get("result_invocation_id") or "")
                and str(binding.get("execution_mode") or "")
                == str(item["execution_mode"])
                and str(binding.get("provider") or "") == str(item["provider"])
                and str(binding.get("fetch_mode") or "")
                == str(item.get("fetch_mode") or "")
                and str(binding.get("status") or "")
                == str(item.get("status") or "")
                and int(binding.get("attempt") or 0) == int(item["attempt"])
                and str(binding.get("content_hash") or "")
                == str(item.get("content_hash") or "")
                and str(binding.get("content_hash_scope") or "unknown")
                == str(item.get("content_hash_scope") or "unknown")
                and str(binding.get("snapshot_sha256") or "")
                == str(item.get("snapshot_sha256") or "")
            )
            item["binding"] = binding
            item["binding_digest_valid"] = digest_valid
            item["binding_fields_match"] = fields_match
            snapshot_available, snapshot_hash_valid = (
                self._source_fetch_snapshot_integrity(item)
            )
            item["snapshot_available"] = snapshot_available
            item["snapshot_hash_valid"] = snapshot_hash_valid
            item["binding_valid"] = bool(
                item["binding_status"] == "server_bound"
                and item["binding_version"] == SOURCE_FETCH_BINDING_VERSION
                and digest_valid
                and fields_match
                and snapshot_hash_valid
            )
            result.append(item)
        return {
            "items": result,
            "has_more": has_more,
            "next_cursor": str(visible[-1]["_audit_rowid"])
            if has_more and visible
            else None,
        }

    def tool_operation_totals(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT kind, COUNT(*), COALESCE(SUM(attempt_count), 0),
                       COALESCE(SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END), 0)
                FROM operations
                WHERE kind IN ('search', 'fetch')
                GROUP BY kind
                """
            ).fetchall()
        totals = {
            "search_operations": 0,
            "search_attempts": 0,
            "fetch_operations": 0,
            "fetch_attempts": 0,
            "pages_fetched": 0,
        }
        for kind, operations, attempts, succeeded in rows:
            totals[f"{kind}_operations"] = int(operations)
            totals[f"{kind}_attempts"] = int(attempts)
            if kind == "fetch":
                totals["pages_fetched"] = int(succeeded)
        return totals

    def latest(self) -> ResearchState | None:
        latest = self.latest_with_id()
        return latest[1] if latest else None

    def latest_with_id(self) -> tuple[int, ResearchState] | None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                "SELECT id, state_json FROM checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return (int(row[0]), _state_from_dict(json.loads(row[1]))) if row else None

    def write_final(self, state: ResearchState) -> None:
        path = self.run_dir / "final.json"
        temporary = self.run_dir / ".final.json.tmp"
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            temporary.write_text(
                json.dumps(state.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            connection.commit()

    def write_artifact(
        self,
        artifact: ArtifactRef,
        payload: dict[str, Any],
    ) -> None:
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            prepared = self._prepare_artifact(
                connection,
                artifact,
                payload,
                require_manifest=False,
                allow_orphan_recovery=False,
            )
            self._insert_artifact_manifest(
                connection,
                artifact,
                prepared,
                checkpoint_id=None,
            )
            self._write_artifact_files(artifact, prepared)
            connection.commit()

    def _prepare_artifact(
        self,
        connection: sqlite3.Connection,
        artifact: ArtifactRef,
        payload: dict[str, Any],
        *,
        require_manifest: bool,
        allow_orphan_recovery: bool,
    ) -> dict[str, Any]:
        if not re.fullmatch(r"A[A-Za-z0-9_-]{1,79}", artifact.artifact_id):
            raise ValueError("invalid artifact id")
        if artifact.content_uri != f"artifacts/{artifact.artifact_id}.json":
            raise ArtifactIntegrityError("artifact content URI is not canonical")
        if artifact.media_type != "application/json":
            raise ArtifactIntegrityError("artifact media type is not canonical JSON")
        if artifact.canonicalization != "json-sort-keys-utf8-v1":
            raise ArtifactIntegrityError("artifact canonicalization is unsupported")

        content = canonical_artifact_bytes(payload)
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != artifact.checksum:
            raise ValueError("artifact checksum does not match canonical payload")
        if artifact.byte_length is not None and artifact.byte_length != len(content):
            raise ValueError("artifact byte length does not match canonical payload")
        artifact.byte_length = len(content)

        expected_metadata_hash = artifact_metadata_hash(artifact)
        if artifact.metadata_hash and artifact.metadata_hash != expected_metadata_hash:
            raise ArtifactIntegrityError("artifact metadata hash does not match manifest")
        artifact.metadata_hash = expected_metadata_hash

        invocation_valid = False
        if artifact.producer_invocation_id:
            producer = connection.execute(
                """
                SELECT run_id, trace_id, agent_id, status
                FROM agent_invocations WHERE invocation_id = ?
                """,
                (artifact.producer_invocation_id,),
            ).fetchone()
            if producer is None:
                raise ArtifactIntegrityError("artifact references a ghost producer invocation")
            if str(producer[0]) != self.run_id or str(producer[1]) != self.run_id:
                raise ArtifactIntegrityError("artifact producer belongs to another run or trace")
            if str(producer[2]) != artifact.producer:
                raise ArtifactIntegrityError("artifact producer does not match its invocation")
            if str(producer[3]) != "succeeded":
                raise ArtifactIntegrityError("artifact producer invocation is not successful")
            invocation_valid = True

        manifest_valid = bool(
            invocation_valid
            and artifact.handoff_message_id
            and artifact.metadata_hash
        )
        if require_manifest and not manifest_valid:
            raise ArtifactIntegrityError(
                "canonical artifact requires producer invocation, handoff, and metadata hash"
            )

        if artifact.parent_artifact_id:
            if artifact.parent_artifact_id == artifact.artifact_id:
                raise ArtifactIntegrityError("artifact cannot be its own parent")
            parent = connection.execute(
                """
                SELECT status FROM artifact_manifests
                WHERE artifact_id = ? AND run_id = ?
                """,
                (artifact.parent_artifact_id, self.run_id),
            ).fetchone()
            if parent is None or str(parent[0]) not in {
                "committed",
                "legacy_verified",
            }:
                raise ArtifactIntegrityError("artifact parent is missing or uncommitted")
            parent_path = (
                self.run_dir / "artifacts" / f"{artifact.parent_artifact_id}.json"
            )
            parent_metadata_path = (
                self.run_dir
                / "artifacts"
                / f"{artifact.parent_artifact_id}.meta.json"
            )
            if not parent_path.exists() or not parent_metadata_path.exists():
                raise ArtifactIntegrityError("artifact parent files are missing")

        registered = connection.execute(
            "SELECT metadata_json FROM artifact_manifests WHERE artifact_id = ?",
            (artifact.artifact_id,),
        ).fetchone()
        if registered is not None:
            raise ArtifactIntegrityError("duplicate artifact id is immutable")

        metadata = json.dumps(
            asdict(artifact),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        directory = self.run_dir / "artifacts"
        path = directory / f"{artifact.artifact_id}.json"
        metadata_path = directory / f"{artifact.artifact_id}.meta.json"
        orphan_recovery = False
        if path.exists() or metadata_path.exists():
            exact_orphan = (
                path.exists()
                and metadata_path.exists()
                and path.read_bytes() == content
                and metadata_path.read_bytes() == metadata
            )
            if not allow_orphan_recovery or not exact_orphan:
                raise ArtifactIntegrityError(
                    "artifact id already exists as an unregistered or mismatched artifact"
                )
            orphan_recovery = True
        return {
            "content": content,
            "metadata": metadata,
            "manifest_valid": manifest_valid,
            "orphan_recovery": orphan_recovery,
        }

    def _insert_artifact_manifest(
        self,
        connection: sqlite3.Connection,
        artifact: ArtifactRef,
        prepared: dict[str, Any],
        *,
        checkpoint_id: int | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO artifact_manifests(
                artifact_id, run_id, checksum, metadata_hash, metadata_json,
                producer_invocation_id, handoff_message_id,
                parent_artifact_id, checkpoint_id, manifest_valid,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?)
            """,
            (
                artifact.artifact_id,
                self.run_id,
                artifact.checksum,
                artifact.metadata_hash,
                prepared["metadata"].decode("utf-8"),
                artifact.producer_invocation_id,
                artifact.handoff_message_id,
                artifact.parent_artifact_id,
                checkpoint_id,
                int(bool(prepared["manifest_valid"])),
                datetime.now(UTC).isoformat(),
            ),
        )

    def _write_artifact_files(
        self,
        artifact: ArtifactRef,
        prepared: dict[str, Any],
    ) -> None:
        if prepared["orphan_recovery"]:
            return
        directory = self.run_dir / "artifacts"
        directory.mkdir(exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
        path = directory / f"{artifact.artifact_id}.json"
        metadata_path = directory / f"{artifact.artifact_id}.meta.json"
        temporary = directory / f".{artifact.artifact_id}.json.tmp"
        metadata_temporary = directory / f".{artifact.artifact_id}.meta.json.tmp"
        temporary.write_bytes(prepared["content"])
        os.chmod(temporary, 0o600)
        metadata_temporary.write_bytes(prepared["metadata"])
        os.chmod(metadata_temporary, 0o600)
        os.replace(temporary, path)
        os.replace(metadata_temporary, metadata_path)

    def load_artifact_ref(self, artifact_id: str | None) -> ArtifactRef | None:
        if not artifact_id or not re.fullmatch(r"A[A-Za-z0-9_-]{1,79}", artifact_id):
            return None
        metadata_path = self.run_dir / "artifacts" / f"{artifact_id}.meta.json"
        path = self.run_dir / "artifacts" / f"{artifact_id}.json"
        producer_binding = None
        handoff_binding = None
        parent_binding = None
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            manifest = connection.execute(
                "SELECT * FROM artifact_manifests WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
            if manifest is not None and bool(manifest["manifest_valid"]):
                producer_binding = connection.execute(
                    """
                    SELECT run_id, trace_id, agent_id, status
                    FROM agent_invocations WHERE invocation_id = ?
                    """,
                    (manifest["producer_invocation_id"],),
                ).fetchone()
                handoff_binding = connection.execute(
                    """
                    SELECT run_id, trace_id, producer_invocation_id,
                           producer, envelope_json
                    FROM handoff_messages WHERE message_id = ?
                    """,
                    (manifest["handoff_message_id"],),
                ).fetchone()
                if manifest["parent_artifact_id"] is not None:
                    parent_binding = connection.execute(
                        """
                        SELECT status FROM artifact_manifests
                        WHERE artifact_id = ? AND run_id = ?
                        """,
                        (manifest["parent_artifact_id"], self.run_id),
                    ).fetchone()
        if not metadata_path.exists() and not path.exists() and manifest is None:
            return None
        if not metadata_path.exists() or not path.exists():
            raise ArtifactIntegrityError(
                f"artifact {artifact_id} registry/files are incomplete"
            )
        raw = json.loads(metadata_path.read_text(encoding="utf-8"))
        if manifest is None and any(
            raw.get(key)
            for key in (
                "metadata_hash",
                "producer_invocation_id",
                "handoff_message_id",
                "parent_artifact_id",
            )
        ):
            raise ArtifactIntegrityError(
                f"artifact {artifact_id} is an unregistered orphan"
            )
        artifact = ArtifactRef(**raw)
        content = path.read_bytes()
        if artifact.content_uri != f"artifacts/{artifact_id}.json":
            raise RuntimeError(f"artifact {artifact_id} content URI verification failed")
        if artifact.media_type != "application/json":
            raise RuntimeError(f"artifact {artifact_id} media type verification failed")
        if artifact.canonicalization != "json-sort-keys-utf8-v1":
            raise RuntimeError(
                f"artifact {artifact_id} canonicalization verification failed"
            )
        if artifact.metadata_hash:
            expected_metadata_hash = artifact_metadata_hash(artifact)
            if artifact.metadata_hash != expected_metadata_hash:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} metadata hash verification failed"
                )
        if manifest is not None:
            if str(manifest["run_id"]) != self.run_id:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} registry run verification failed"
                )
            if str(manifest["status"]) not in {"committed", "legacy_verified"}:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} registry status is not committed"
                )
            if str(manifest["checksum"]) != artifact.checksum:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} registry checksum verification failed"
                )
            if str(manifest["metadata_hash"]) != artifact.metadata_hash:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} registry metadata hash verification failed"
                )
            registered_metadata = json.loads(str(manifest["metadata_json"]))
            if registered_metadata != raw:
                raise ArtifactIntegrityError(
                    f"artifact {artifact_id} metadata overwrite detected"
                )
            if bool(manifest["manifest_valid"]):
                if (
                    producer_binding is None
                    or str(producer_binding[0]) != self.run_id
                    or str(producer_binding[1]) != self.run_id
                    or str(producer_binding[2]) != artifact.producer
                    or str(producer_binding[3]) != "succeeded"
                ):
                    raise ArtifactIntegrityError(
                        f"artifact {artifact_id} producer binding verification failed"
                    )
                if (
                    handoff_binding is None
                    or str(handoff_binding[0]) != self.run_id
                    or str(handoff_binding[1]) != self.run_id
                    or str(handoff_binding[2]) != artifact.producer_invocation_id
                    or str(handoff_binding[3]) != artifact.producer
                ):
                    raise ArtifactIntegrityError(
                        f"artifact {artifact_id} handoff binding verification failed"
                    )
                try:
                    bound_envelope = json.loads(str(handoff_binding[4]))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ArtifactIntegrityError(
                        f"artifact {artifact_id} handoff envelope is invalid"
                    ) from error
                if (
                    not isinstance(bound_envelope, dict)
                    or str(bound_envelope.get("message_id") or "")
                    != artifact.handoff_message_id
                    or str(bound_envelope.get("producer_invocation_id") or "")
                    != artifact.producer_invocation_id
                    or bound_envelope.get("output_artifacts") != [asdict(artifact)]
                ):
                    raise ArtifactIntegrityError(
                        f"artifact {artifact_id} handoff envelope verification failed"
                    )
                if artifact.parent_artifact_id is not None and (
                    parent_binding is None
                    or str(parent_binding[0]) not in {
                        "committed",
                        "legacy_verified",
                    }
                    or not (
                        self.run_dir
                        / "artifacts"
                        / f"{artifact.parent_artifact_id}.json"
                    ).exists()
                    or not (
                        self.run_dir
                        / "artifacts"
                        / f"{artifact.parent_artifact_id}.meta.json"
                    ).exists()
                ):
                    raise ArtifactIntegrityError(
                        f"artifact {artifact_id} parent binding verification failed"
                    )
        if hashlib.sha256(content).hexdigest() != artifact.checksum:
            raise RuntimeError(f"artifact {artifact_id} checksum verification failed")
        if artifact.byte_length is not None and len(content) != artifact.byte_length:
            raise RuntimeError(f"artifact {artifact_id} byte length verification failed")
        parsed = json.loads(content.decode("utf-8"))
        if not isinstance(parsed, dict) or canonical_artifact_bytes(parsed) != content:
            raise RuntimeError(f"artifact {artifact_id} canonical JSON verification failed")
        return artifact

    def read_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        artifact = self.load_artifact_ref(artifact_id)
        if artifact is None:
            return None
        path = self.run_dir / "artifacts" / f"{artifact_id}.json"
        content = path.read_bytes()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                """
                SELECT *
                FROM artifact_manifests WHERE artifact_id = ?
                """,
                (artifact_id,),
            ).fetchone()
        manifest = dict(row) if row else None
        registered_metadata = (
            json.loads(str(manifest["metadata_json"])) if manifest else None
        )
        envelope = None
        if manifest and manifest.get("handoff_message_id"):
            with closing(sqlite3.connect(self.database_path)) as connection:
                envelope_row = connection.execute(
                    """
                    SELECT envelope_json FROM handoff_messages
                    WHERE message_id = ?
                    """,
                    (manifest["handoff_message_id"],),
                ).fetchone()
            envelope = json.loads(str(envelope_row[0])) if envelope_row else None
        recomputed_metadata_hash = artifact_metadata_hash(artifact)
        return {
            "artifact": asdict(artifact),
            "disk_metadata": asdict(artifact),
            "registry_manifest": manifest,
            "registry_metadata": registered_metadata,
            "handoff_envelope": envelope,
            "recomputed_sha256": hashlib.sha256(content).hexdigest(),
            "recomputed_bytes": len(content),
            "recomputed_metadata_hash": recomputed_metadata_hash,
            "canonical_json": content.decode("utf-8"),
            "manifest_valid": bool(manifest["manifest_valid"]) if manifest else False,
            "registry_status": str(manifest["status"]) if manifest else "legacy_unregistered",
            "checkpoint_id": (
                int(manifest["checkpoint_id"])
                if manifest and manifest["checkpoint_id"] is not None
                else None
            ),
            "cross_checks": {
                "content_checksum_matches_disk_metadata": (
                    hashlib.sha256(content).hexdigest() == artifact.checksum
                ),
                "byte_length_matches_disk_metadata": len(content) == artifact.byte_length,
                "metadata_hash_recomputes": (
                    recomputed_metadata_hash == artifact.metadata_hash
                ),
                "registry_metadata_matches_disk": registered_metadata == asdict(artifact),
                "registry_checksum_matches_disk": bool(
                    manifest and str(manifest["checksum"]) == artifact.checksum
                ),
                "registry_metadata_hash_matches_disk": bool(
                    manifest
                    and str(manifest["metadata_hash"]) == artifact.metadata_hash
                ),
                "handoff_output_matches_disk": bool(
                    envelope
                    and envelope.get("output_artifacts") == [asdict(artifact)]
                ),
            },
        }

    def artifact_manifest_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM artifact_manifests ORDER BY created_at, artifact_id"
            ).fetchall()
        result: list[dict[str, Any]] = []
        registered_ids = {str(row["artifact_id"]) for row in rows}
        directory = self.run_dir / "artifacts"
        for row in rows:
            item = dict(row)
            artifact_id = str(row["artifact_id"])
            item["manifest_valid"] = bool(item["manifest_valid"])
            item["files_present"] = (
                (directory / f"{artifact_id}.json").exists()
                and (directory / f"{artifact_id}.meta.json").exists()
            )
            try:
                integrity_valid = self.load_artifact_ref(artifact_id) is not None
                integrity_error = ""
            except (
                ArtifactIntegrityError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                integrity_valid = False
                integrity_error = str(error)
            item["integrity_status"] = (
                "verified" if integrity_valid else "invalid"
            )
            item["integrity_error"] = integrity_error
            item["passable"] = bool(
                item["manifest_valid"]
                and item["status"] == "committed"
                and item["files_present"]
                and integrity_valid
            )
            result.append(item)
        if directory.exists():
            filesystem_ids = {
                path.name.removesuffix(".meta.json")
                for path in directory.glob("A*.meta.json")
            } | {
                path.stem for path in directory.glob("A*.json")
                if not path.name.endswith(".meta.json")
            }
            for artifact_id in sorted(filesystem_ids - registered_ids):
                result.append(
                    {
                        "artifact_id": artifact_id,
                        "run_id": self.run_id,
                        "status": "orphan",
                        "manifest_valid": False,
                        "files_present": True,
                        "passable": False,
                    }
                )
        return result

    def artifact_manifest_audit_page(
        self,
        *,
        limit: int = 50,
        after: object = 0,
    ) -> dict[str, Any]:
        """Read manifests and filesystem orphans with a two-phase keyset."""
        limit = self._audit_limit(limit)
        cursor = self._audit_cursor(after)
        phase = str(cursor.get("phase") or "db")
        if phase not in {"db", "orphan"}:
            raise ValueError("invalid artifact audit cursor")
        db_after = self._audit_after(cursor.get("db", cursor.get("rowid", 0)))
        orphan_after = str(cursor.get("orphan") or "")
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            registered_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT artifact_id FROM artifact_manifests"
                ).fetchall()
            }
            rows = (
                connection.execute(
                    """
                    SELECT rowid AS _audit_rowid, * FROM artifact_manifests
                    WHERE rowid > ?
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (db_after, limit + 1),
                ).fetchall()
                if phase == "db"
                else []
            )
        result: list[dict[str, Any]] = []
        directory = self.run_dir / "artifacts"
        visible = rows[:limit]

        def project_manifest(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item.pop("_audit_rowid", None)
            artifact_id = str(row["artifact_id"])
            item["manifest_valid"] = bool(item["manifest_valid"])
            item["files_present"] = (
                (directory / f"{artifact_id}.json").exists()
                and (directory / f"{artifact_id}.meta.json").exists()
            )
            try:
                integrity_valid = self.load_artifact_ref(artifact_id) is not None
                integrity_error = ""
            except (
                ArtifactIntegrityError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                integrity_valid = False
                integrity_error = str(error)
            item["integrity_status"] = "verified" if integrity_valid else "invalid"
            item["integrity_error"] = integrity_error
            item["passable"] = bool(
                item["manifest_valid"]
                and item["status"] == "committed"
                and item["files_present"]
                and integrity_valid
            )
            return item

        for row in visible:
            result.append(project_manifest(row))

        db_has_more = len(rows) > limit
        filesystem_ids = (
            {
                path.name.removesuffix(".meta.json")
                for path in directory.glob("A*.meta.json")
            }
            | {
                path.stem
                for path in directory.glob("A*.json")
                if not path.name.endswith(".meta.json")
            }
            if directory.exists()
            else set()
        )
        orphan_ids = sorted(filesystem_ids - registered_ids)
        visible_orphans: list[str] = []
        if not db_has_more and len(result) < limit:
            visible_orphans = [
                artifact_id
                for artifact_id in orphan_ids
                if phase != "orphan" or artifact_id > orphan_after
            ][: limit - len(result)]
            result.extend(
                {
                    "artifact_id": artifact_id,
                    "run_id": self.run_id,
                    "status": "orphan",
                    "manifest_valid": False,
                    "files_present": True,
                    "passable": False,
                }
                for artifact_id in visible_orphans
            )
        orphan_has_more = (
            not db_has_more
            and len(visible_orphans) < len(
                [
                    artifact_id
                    for artifact_id in orphan_ids
                    if phase != "orphan" or artifact_id > orphan_after
                ]
            )
        )
        has_more = db_has_more or orphan_has_more
        if db_has_more:
            next_cursor: str | None = json.dumps(
                {"phase": "db", "db": int(visible[-1]["_audit_rowid"])},
                separators=(",", ":"),
            )
        elif orphan_has_more:
            next_cursor = json.dumps(
                {
                    "phase": "orphan",
                    "db": int(visible[-1]["_audit_rowid"])
                    if visible
                    else db_after,
                    "orphan": visible_orphans[-1],
                },
                separators=(",", ":"),
            )
        else:
            next_cursor = None
        return {
            "items": result,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    def _validate_handoff(
        self,
        connection: sqlite3.Connection,
        envelope: dict[str, Any],
        artifact: ArtifactRef | None,
    ) -> dict[str, Any]:
        if str(envelope.get("run_id", "")) != self.run_id:
            raise HandoffValidationError("handoff run does not match durable run")
        if str(envelope.get("trace_id", "")) != self.run_id:
            raise HandoffValidationError("handoff trace does not match durable run")
        message_id = str(envelope.get("message_id", ""))
        if not message_id:
            raise HandoffValidationError("handoff message id is required")
        if connection.execute(
            "SELECT 1 FROM handoff_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone():
            raise HandoffValidationError("duplicate handoff message id")
        self._validate_resume_handoff_binding(connection, envelope, message_id)

        producer_invocation_id = str(
            envelope.get("producer_invocation_id") or ""
        )
        if artifact is not None:
            if artifact.handoff_message_id != message_id:
                raise HandoffValidationError(
                    "artifact handoff message id does not match the envelope"
                )
            if artifact.producer_invocation_id != producer_invocation_id:
                raise HandoffValidationError(
                    "artifact producer invocation does not match the envelope"
                )
            if artifact.producer != str(envelope.get("producer") or ""):
                raise HandoffValidationError(
                    "artifact producer does not match the envelope"
                )
        producer = connection.execute(
            """
            SELECT run_id, trace_id, agent_id, status
            FROM agent_invocations WHERE invocation_id = ?
            """,
            (producer_invocation_id,),
        ).fetchone()
        if producer is None:
            raise HandoffValidationError("handoff references a ghost producer invocation")
        if str(producer[0]) != self.run_id or str(producer[1]) != self.run_id:
            raise HandoffValidationError(
                "handoff producer invocation belongs to another run or trace"
            )
        if str(producer[2]) != str(envelope.get("producer", "")):
            raise HandoffValidationError("handoff producer identity is inconsistent")
        if str(producer[3]) != "succeeded":
            raise HandoffValidationError("handoff producer invocation is not successful")
        intended_consumer = str(envelope.get("intended_consumer") or "")
        if not intended_consumer:
            raise HandoffValidationError("handoff intended consumer is required")
        if str(envelope.get("consumer") or "") != intended_consumer:
            raise HandoffValidationError(
                "handoff legacy consumer alias disagrees with intended consumer"
            )

        outputs = envelope.get("output_artifacts")
        if artifact is not None:
            if not isinstance(outputs, list) or len(outputs) != 1:
                raise HandoffValidationError(
                    "stage handoff must declare exactly one output artifact"
                )
            output = outputs[0]
            if not isinstance(output, dict):
                raise HandoffValidationError("handoff artifact reference is invalid")
            if output != asdict(artifact):
                raise HandoffValidationError(
                    "handoff output artifact does not exactly match the committed ArtifactRef"
                )
            inputs = envelope.get("input_artifacts")
            expected_parent = artifact.parent_artifact_id
            if expected_parent is None:
                if inputs not in (None, []):
                    raise HandoffValidationError(
                        "root artifact cannot claim an input parent"
                    )
            else:
                parent_row = connection.execute(
                    """
                    SELECT metadata_json FROM artifact_manifests
                    WHERE artifact_id = ? AND run_id = ?
                    """,
                    (expected_parent, self.run_id),
                ).fetchone()
                if parent_row is None:
                    raise HandoffValidationError(
                        "handoff input artifact references a missing manifest parent"
                    )
                try:
                    expected_input = asdict(
                        ArtifactRef(**json.loads(str(parent_row[0])))
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise HandoffValidationError(
                        "handoff parent artifact manifest is invalid"
                    ) from error
                if inputs != [expected_input]:
                    raise HandoffValidationError(
                        "handoff input artifact does not exactly match the manifest parent"
                    )

        return self._validate_handoff_receipt(connection, envelope)

    def _validate_handoff_receipt(
        self,
        connection: sqlite3.Connection,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        receipt = envelope.get("receipt")
        if receipt is None:
            if envelope.get("receipt_validation") == "invalid":
                raise HandoffValidationError(
                    str(envelope.get("receipt_validation_error") or "invalid receipt")
                )
            return {
                "valid": None,
                "status": "not_present",
                "reason": "This handoff has no upstream message to acknowledge.",
                "checks": {},
            }
        if not isinstance(receipt, dict):
            raise HandoffValidationError("handoff receipt must be an object")
        if receipt.get("valid") is not True:
            raise HandoffValidationError(
                str(receipt.get("validation_error") or "receipt is marked invalid")
            )
        if str(receipt.get("run_id") or "") != self.run_id:
            raise HandoffValidationError("receipt run does not match durable run")
        if str(receipt.get("trace_id") or "") != self.run_id:
            raise HandoffValidationError("receipt trace does not match durable run")

        consumed_message_id = str(receipt.get("message_id") or "")
        source = connection.execute(
            """
            SELECT run_id, trace_id, producer_invocation_id, producer,
                   intended_consumer, route_target, created_at
            FROM handoff_messages WHERE message_id = ?
            """,
            (consumed_message_id,),
        ).fetchone()
        if source is None:
            raise HandoffValidationError("receipt references a ghost handoff message")
        if str(source[0]) != self.run_id or str(source[1]) != self.run_id:
            raise HandoffValidationError(
                "receipt references a handoff from another run or trace"
            )
        if connection.execute(
            "SELECT 1 FROM handoff_receipts WHERE message_id = ?",
            (consumed_message_id,),
        ).fetchone():
            raise HandoffValidationError("duplicate receipt for handoff message")

        invocation_id = str(receipt.get("consumed_by_invocation_id") or "")
        consumer = connection.execute(
            """
            SELECT run_id, trace_id, agent_id, operation, attempt, status,
                   started_at, consumed_handoff_ids_json
            FROM agent_invocations WHERE invocation_id = ?
            """,
            (invocation_id,),
        ).fetchone()
        if consumer is None:
            raise HandoffValidationError("receipt references a ghost invocation")
        if str(consumer[0]) != self.run_id or str(consumer[1]) != self.run_id:
            raise HandoffValidationError(
                "receipt invocation belongs to another run or trace"
            )
        consumed_by_agent_id = str(receipt.get("consumed_by_agent_id") or "")
        if str(consumer[2]) != consumed_by_agent_id:
            raise HandoffValidationError("receipt consumer identity is inconsistent")
        if str(source[4]) != consumed_by_agent_id:
            raise HandoffValidationError("receipt was issued by the wrong consumer")
        if str(consumer[5]) != "succeeded":
            raise HandoffValidationError("receipt invocation is not successful")
        try:
            consumed_ids = json.loads(str(consumer[7] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise HandoffValidationError(
                "receipt invocation consumption identity is invalid"
            ) from error
        if consumed_message_id not in consumed_ids:
            raise HandoffValidationError(
                "receipt invocation did not declare consumption of the message"
            )
        if str(envelope.get("producer_invocation_id") or "") != invocation_id:
            raise HandoffValidationError(
                "receipt consumer is not the current handoff producer invocation"
            )

        expected_operation = _ROUTE_TARGET_OPERATIONS.get(str(source[5]))
        if expected_operation is None:
            raise HandoffValidationError(
                "receipt source route target has no registered consumer operation"
            )
        if str(consumer[3]) != expected_operation:
            raise HandoffValidationError(
                "receipt consumer operation does not match the source route target"
            )
        receipt_operation = receipt.get("consumed_by_operation")
        modern_receipt = str(envelope.get("schema_version") or "").startswith(
            "deep-research-handoff/1.1"
        )
        if receipt_operation is None:
            if modern_receipt:
                raise HandoffValidationError(
                    "receipt is missing consumed_by_operation"
                )
        elif str(receipt_operation) != str(consumer[3]):
            raise HandoffValidationError(
                "receipt consumed_by_operation is inconsistent"
            )

        source_producer_invocation_id = str(source[2])
        receipt_source_producer = receipt.get(
            "consumed_from_producer_invocation_id"
        )
        if receipt_source_producer is None:
            if modern_receipt:
                raise HandoffValidationError(
                    "receipt is missing consumed_from_producer_invocation_id"
                )
        elif str(receipt_source_producer) != source_producer_invocation_id:
            raise HandoffValidationError(
                "receipt source producer invocation is inconsistent"
            )

        consumption = connection.execute(
            """
            SELECT consumer_agent_id, consumer_operation,
                   source_producer_invocation_id, binding_status, recorded_at,
                   consumer_attempt, consumption_fence,
                   superseded_by_invocation_id
            FROM handoff_consumptions
            WHERE message_id = ? AND consumer_invocation_id = ?
            """,
            (consumed_message_id, invocation_id),
        ).fetchone()
        if consumption is None:
            raise HandoffValidationError(
                "receipt has no explicit server-side consumption binding"
            )
        if (
            str(consumption[0]) != consumed_by_agent_id
            or str(consumption[1]) != str(consumer[3])
            or str(consumption[2]) != source_producer_invocation_id
            or int(consumption[5] or 0) != int(consumer[4] or 0)
        ):
            raise HandoffValidationError(
                "receipt disagrees with the explicit consumption binding"
            )
        consumption_status = str(consumption[3])
        consumption_fence = int(consumption[6] or 0)
        if (
            consumption_status not in _HANDOFF_PENDING_BINDING_STATUSES
            or consumption_fence <= 0
            or str(consumption[7] or "")
        ):
            raise HandoffValidationError(
                "receipt does not reference the active consumption fence"
            )
        max_fence = connection.execute(
            """
            SELECT MAX(consumption_fence)
            FROM handoff_consumptions
            WHERE message_id = ? AND consumption_fence > 0
            """,
            (consumed_message_id,),
        ).fetchone()[0]
        if max_fence is None or int(max_fence) != consumption_fence:
            raise HandoffValidationError(
                "receipt does not reference the current consumption fence"
            )
        competing = connection.execute(
            """
            SELECT consumer_invocation_id, binding_status,
                   consumption_fence, superseded_by_invocation_id
            FROM handoff_consumptions
            WHERE message_id = ? AND consumer_invocation_id != ?
            """,
            (consumed_message_id, invocation_id),
        ).fetchall()
        if any(
            str(item[3] or "") == ""
            and str(item[1]) in {
                *_HANDOFF_PENDING_BINDING_STATUSES,
                "server_validated",
            }
            for item in competing
        ):
            raise HandoffValidationError(
                "receipt source message has another active consumer"
            )
        if any(str(item[1]) == "server_validated" for item in competing):
            raise HandoffValidationError(
                "receipt source message has another server-validated consumer"
            )

        source_created = _parse_utc_timestamp(source[6], "source handoff")
        consumer_started = _parse_utc_timestamp(consumer[6], "consumer invocation")
        consumed_at = _parse_utc_timestamp(receipt.get("consumed_at"), "receipt")
        current_handoff_created = _parse_utc_timestamp(
            envelope.get("created_at"), "current handoff"
        )
        if source_created > consumer_started:
            raise HandoffValidationError(
                "receipt consumer started before the source handoff existed"
            )
        if consumer_started > consumed_at:
            raise HandoffValidationError(
                "receipt predates the consumer invocation"
            )
        if consumed_at > current_handoff_created:
            raise HandoffValidationError(
                "receipt was recorded after the current handoff was created"
            )

        return {
            "valid": True,
            "status": "server_validated",
            "reason": "Producer, route, consumer operation, explicit consumption, and timestamps agree.",
            "checks": {
                "run_trace_scope": True,
                "source_message_exists": True,
                "source_producer_binding": True,
                "intended_consumer_binding": True,
                "consumer_invocation_exists": True,
                "consumer_operation_matches_route": True,
                "explicit_consumption_binding": True,
                "active_consumption_fence": True,
                "single_consumer": True,
                "timestamp_order": True,
            },
            "source_producer_invocation_id": source_producer_invocation_id,
            "consumer_invocation_id": invocation_id,
            "consumer_operation": str(consumer[3]),
            "consumption_fence": consumption_fence,
        }

    def _insert_handoff(
        self,
        connection: sqlite3.Connection,
        envelope: dict[str, Any],
        checkpoint_id: int,
        *,
        receipt_validation: dict[str, Any] | None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO handoff_messages(
                message_id, run_id, trace_id, producer,
                producer_invocation_id, intended_consumer, route_target,
                envelope_json, checkpoint_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(envelope["message_id"]),
                self.run_id,
                self.run_id,
                str(envelope["producer"]),
                str(envelope["producer_invocation_id"]),
                str(envelope["intended_consumer"]),
                str(envelope["route_target"]),
                json.dumps(envelope, ensure_ascii=False, sort_keys=True),
                checkpoint_id,
                str(envelope["created_at"]),
            ),
        )
        receipt = envelope.get("receipt")
        if isinstance(receipt, dict):
            connection.execute(
                """
                INSERT INTO handoff_receipts(
                    message_id, run_id, trace_id,
                    consumed_by_invocation_id, consumed_by_agent_id,
                    consumed_by_operation,
                    consumed_from_producer_invocation_id,
                    receipt_json, consumed_at, validation_status,
                    validation_json, validated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(receipt["message_id"]),
                    self.run_id,
                    self.run_id,
                    str(receipt["consumed_by_invocation_id"]),
                    str(receipt["consumed_by_agent_id"]),
                    receipt.get("consumed_by_operation"),
                    receipt.get("consumed_from_producer_invocation_id"),
                    json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                    str(receipt["consumed_at"]),
                    str((receipt_validation or {}).get("status") or "unverified"),
                    json.dumps(
                        receipt_validation or {},
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    datetime.now(UTC).isoformat(),
                ),
            )
            cursor = connection.execute(
                """
                UPDATE handoff_consumptions
                SET binding_status = 'server_validated', recorded_at = ?
                WHERE message_id = ?
                  AND consumer_invocation_id = ?
                  AND binding_status IN (
                      'pending_receipt', 'retry_pending_receipt',
                      'replay_pending_receipt'
                  )
                  AND superseded_by_invocation_id IS NULL
                  AND consumption_fence = ?
                """,
                (
                    datetime.now(UTC).isoformat(),
                    str(receipt["message_id"]),
                    str(receipt["consumed_by_invocation_id"]),
                    int((receipt_validation or {}).get("consumption_fence") or 0),
                ),
            )
            if cursor.rowcount != 1:
                raise HandoffValidationError(
                    "receipt consumption fence is no longer active"
                )

    def _record_rejected_receipt(
        self,
        envelope: dict[str, Any],
        reason: str,
    ) -> None:
        if not isinstance(envelope, dict):
            return
        receipt = envelope.get("receipt")
        receipt_payload = receipt if isinstance(receipt, dict) else envelope
        with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            connection.execute(
                """
                INSERT INTO handoff_receipt_rejections(
                    message_id, run_id, trace_id,
                    consumed_by_invocation_id, consumed_by_agent_id,
                    reason, receipt_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_payload.get("message_id"),
                    receipt_payload.get("run_id"),
                    receipt_payload.get("trace_id"),
                    receipt_payload.get("consumed_by_invocation_id"),
                    receipt_payload.get("consumed_by_agent_id"),
                    reason[:2000],
                    json.dumps(receipt_payload, ensure_ascii=False, sort_keys=True),
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()

    def validate_handoff_receipt(
        self,
        envelope: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            with closing(sqlite3.connect(self.database_path)) as connection:
                self._validate_handoff_receipt(connection, envelope)
        except HandoffValidationError as error:
            return {"valid": False, "status": "invalid", "reason": str(error)}
        return {"valid": True, "status": "valid", "reason": ""}

    def handoff_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT handoff_messages.*,
                       handoff_receipts.consumed_by_invocation_id,
                       handoff_receipts.consumed_by_agent_id,
                       handoff_receipts.consumed_by_operation,
                       handoff_receipts.consumed_from_producer_invocation_id,
                       handoff_receipts.consumed_at,
                       handoff_receipts.validation_status,
                       handoff_receipts.validation_json,
                       handoff_receipts.validated_at
                FROM handoff_messages
                LEFT JOIN handoff_receipts USING(message_id)
                ORDER BY handoff_messages.checkpoint_id
                """
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(item.pop("envelope_json"))
            if item.get("validation_json"):
                item["receipt_validation"] = json.loads(
                    str(item.pop("validation_json"))
                )
            else:
                item.pop("validation_json", None)
                item["receipt_validation"] = None
            item["server_validation_status"] = str(
                item.get("validation_status") or "legacy_unverified"
            )
            item["receipt_status"] = (
                "valid"
                if item.get("server_validation_status") == "server_validated"
                else "unverified"
                if item["consumed_by_invocation_id"]
                else "not_consumed"
            )
            result.append(item)
        return result

    def handoff_audit_page(
        self,
        *,
        limit: int = 50,
        after: object = 0,
    ) -> dict[str, Any]:
        """Read durable handoff envelopes with a rowid keyset."""
        limit = self._audit_limit(limit)
        handoff_after = self._audit_after(after)
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT handoff_messages.rowid AS _audit_rowid,
                       handoff_messages.*,
                       handoff_receipts.consumed_by_invocation_id,
                       handoff_receipts.consumed_by_agent_id,
                       handoff_receipts.consumed_by_operation,
                       handoff_receipts.consumed_from_producer_invocation_id,
                       handoff_receipts.consumed_at,
                       handoff_receipts.validation_status,
                       handoff_receipts.validation_json,
                       handoff_receipts.validated_at
                FROM handoff_messages
                LEFT JOIN handoff_receipts USING(message_id)
                WHERE handoff_messages.rowid > ?
                ORDER BY handoff_messages.rowid
                LIMIT ?
                """,
                (handoff_after, limit + 1),
            ).fetchall()
        visible = rows[:limit]
        result: list[dict[str, Any]] = []
        for row in visible:
            item = dict(row)
            item.pop("_audit_rowid", None)
            item["envelope"] = json.loads(item.pop("envelope_json"))
            if item.get("validation_json"):
                item["receipt_validation"] = json.loads(
                    str(item.pop("validation_json"))
                )
            else:
                item.pop("validation_json", None)
                item["receipt_validation"] = None
            item["server_validation_status"] = str(
                item.get("validation_status") or "legacy_unverified"
            )
            item["receipt_status"] = (
                "valid"
                if item.get("server_validation_status") == "server_validated"
                else "unverified"
                if item["consumed_by_invocation_id"]
                else "not_consumed"
            )
            result.append(item)
        has_more = len(rows) > limit
        return {
            "items": result,
            "has_more": has_more,
            "next_cursor": str(visible[-1]["_audit_rowid"])
            if has_more and visible
            else None,
        }

    def handoff_producer_invocation_id(self, message_id: str) -> str | None:
        """Return the durable producer binding for one previously committed handoff."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT producer_invocation_id
                FROM handoff_messages
                WHERE message_id = ? AND run_id = ? AND trace_id = ?
                """,
                (str(message_id), self.run_id, self.run_id),
            ).fetchone()
        return str(row[0]) if row else None

    def handoff_route(self, message_id: str) -> dict[str, str] | None:
        """Return the server-bound route for one durable handoff."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT producer, intended_consumer, route_target,
                       producer_invocation_id
                FROM handoff_messages
                WHERE message_id = ? AND run_id = ? AND trace_id = ?
                """,
                (str(message_id), self.run_id, self.run_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "producer": str(row[0]),
            "intended_consumer": str(row[1]),
            "route_target": str(row[2]),
            "producer_invocation_id": str(row[3]),
        }

    def handoff_resume_binding(self, message_id: str) -> dict[str, Any] | None:
        """Return control-plane binding fields for a durable handoff."""
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT envelope_json, intended_consumer, route_target
                FROM handoff_messages
                WHERE message_id = ? AND run_id = ? AND trace_id = ?
                """,
                (str(message_id), self.run_id, self.run_id),
            ).fetchone()
        if row is None:
            return None
        try:
            envelope = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(envelope, dict):
            return None
        return {
            "resume_receipt_id": envelope.get("resume_receipt_id"),
            "claim_fence": envelope.get("claim_fence"),
            "intended_consumer": str(row[1]),
            "route_target": str(row[2]),
        }

    def receipt_audit(self) -> list[dict[str, Any]]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            valid_rows = connection.execute(
                "SELECT * FROM handoff_receipts ORDER BY consumed_at"
            ).fetchall()
            rejected_rows = connection.execute(
                "SELECT * FROM handoff_receipt_rejections ORDER BY id"
            ).fetchall()
        result = [
            {
                **dict(row),
                "valid": str(row["validation_status"]) == "server_validated",
                "status": str(row["validation_status"]),
                "reason": (
                    json.loads(str(row["validation_json"])).get("reason", "")
                    if row["validation_json"]
                    else ""
                ),
                "validation": (
                    json.loads(str(row["validation_json"]))
                    if row["validation_json"]
                    else {}
                ),
            }
            for row in valid_rows
        ]
        result.extend(
            {
                **dict(row),
                "valid": False,
                "status": "invalid",
            }
            for row in rejected_rows
        )
        return result

    def receipt_audit_page(
        self,
        *,
        limit: int = 50,
        after: object = 0,
    ) -> dict[str, Any]:
        """Page valid receipts before rejected receipts without offset scans."""
        limit = self._audit_limit(limit)
        cursor = self._audit_cursor(after)
        valid_after = self._audit_after(
            cursor.get("valid", cursor.get("rowid", 0))
        )
        rejected_after = self._audit_after(cursor.get("rejected", 0))
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.row_factory = sqlite3.Row
            valid_rows = connection.execute(
                """
                SELECT rowid AS _audit_rowid, *
                FROM handoff_receipts
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (valid_after, limit + 1),
            ).fetchall()
            visible_valid = valid_rows[:limit]
            valid_has_more = len(valid_rows) > limit
            rejected_rows: list[sqlite3.Row] = []
            if not valid_has_more and len(visible_valid) < limit:
                rejected_rows = connection.execute(
                    """
                    SELECT rowid AS _audit_rowid, *
                    FROM handoff_receipt_rejections
                    WHERE rowid > ?
                    ORDER BY rowid
                    LIMIT ?
                    """,
                    (rejected_after, limit - len(visible_valid) + 1),
                ).fetchall()
        visible_rejected = rejected_rows[: max(0, limit - len(visible_valid))]
        result: list[dict[str, Any]] = []
        for row in visible_valid:
            item = dict(row)
            item.pop("_audit_rowid", None)
            item["valid"] = str(row["validation_status"]) == "server_validated"
            item["status"] = str(row["validation_status"])
            item["reason"] = (
                json.loads(str(row["validation_json"])).get("reason", "")
                if row["validation_json"]
                else ""
            )
            item["validation"] = (
                json.loads(str(row["validation_json"]))
                if row["validation_json"]
                else {}
            )
            result.append(item)
        for row in visible_rejected:
            item = dict(row)
            item.pop("_audit_rowid", None)
            item["valid"] = False
            item["status"] = "invalid"
            result.append(item)
        rejected_has_more = len(rejected_rows) > len(visible_rejected)
        has_more = valid_has_more or rejected_has_more
        next_cursor: str | None = None
        if has_more:
            next_cursor = json.dumps(
                {
                    "valid": int(visible_valid[-1]["_audit_rowid"])
                    if visible_valid
                    else valid_after,
                    "rejected": int(visible_rejected[-1]["_audit_rowid"])
                    if visible_rejected
                    else rejected_after,
                },
                separators=(",", ":"),
            )
        return {
            "items": result,
            "has_more": has_more,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _validate_snapshot_key(value: str, label: str) -> str:
        normalized = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", normalized):
            raise ValueError(f"invalid {label}")
        return normalized

    def write_source_snapshot(
        self,
        source_id: str,
        page: Page,
        *,
        fetch_record_id: str | None = None,
    ) -> dict[str, Any]:
        source_id = self._validate_snapshot_key(source_id, "source id")
        fetch_record_id = (
            self._validate_snapshot_key(fetch_record_id, "fetch record id")
            if fetch_record_id
            else None
        )
        directory = self.run_dir / "sources"
        snapshot_keys = [source_id]
        if fetch_record_id and fetch_record_id != source_id:
            snapshot_keys.append(fetch_record_id)
        encoded = page.text.encode()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_execution_fence(connection)
            directory.mkdir(exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
            for key in snapshot_keys:
                path = directory / f"{key}.txt"
                temporary = directory / f".{key}.txt.tmp"
                temporary.write_bytes(encoded)
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            connection.commit()
        return {
            "source_id": source_id,
            "fetch_record_id": fetch_record_id,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "bytes": len(encoded),
        }

    def read_source_snapshot(
        self,
        source_id: str,
        *,
        fetch_record_id: str | None = None,
    ) -> dict[str, Any] | None:
        source_id = self._validate_snapshot_key(source_id, "source id")
        fetch_record_id = (
            self._validate_snapshot_key(fetch_record_id, "fetch record id")
            if fetch_record_id
            else None
        )
        snapshot_key = fetch_record_id or source_id
        path = self.run_dir / "sources" / f"{snapshot_key}.txt"
        if not path.exists() or not path.is_file():
            return None
        text = path.read_text(encoding="utf-8")
        return {
            "source_id": source_id,
            "fetch_record_id": fetch_record_id,
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "bytes": len(text.encode()),
        }

    def acquire_lease(self) -> "RunLease":
        path = self.run_dir / ".run.lock"
        handle = path.open("a+")
        os.chmod(path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            handle.close()
            raise RuntimeError(f"run {self.run_id} is already owned by another executor") from error
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} acquired_at={datetime.now(UTC).isoformat()}\n")
        handle.flush()
        return RunLease(handle)

    def _event_record(
        self, event_type: str, node: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "run_id": self.run_id,
            "event_type": event_type,
            "node": node,
            "payload": payload,
        }

    def _flush_outbox(self) -> None:
        with self.events_lock_path.open("a+b") as lock_handle:
            os.chmod(self.events_lock_path, 0o600)
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            with closing(sqlite3.connect(self.database_path, timeout=10)) as connection:
                connection.execute("PRAGMA busy_timeout = 10000")
                connection.execute("BEGIN IMMEDIATE")
                rows = connection.execute(
                    "SELECT event_id, event_json FROM outbox "
                    "WHERE published_at IS NULL ORDER BY created_at, event_id"
                ).fetchall()
                if not rows:
                    connection.commit()
                    return

                existing_ids: set[str] = set()
                with self.events_path.open("a+b") as events_handle:
                    events_handle.seek(0)
                    content = events_handle.read()
                    complete_end = content.rfind(b"\n") + 1
                    complete_content = content[:complete_end]
                    for raw_line in complete_content.splitlines():
                        try:
                            event_id = json.loads(raw_line.decode("utf-8")).get(
                                "event_id"
                            )
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        if event_id:
                            existing_ids.add(str(event_id))

                    # A crash may leave a partial JSON record. Remove only that
                    # unterminated suffix before appending complete outbox rows.
                    if complete_end != len(content):
                        events_handle.seek(complete_end)
                        events_handle.truncate()
                    events_handle.seek(0, os.SEEK_END)
                    for event_id, event_json in rows:
                        if str(event_id) not in existing_ids:
                            events_handle.write(str(event_json).encode("utf-8") + b"\n")
                    events_handle.flush()
                    os.fsync(events_handle.fileno())
                os.chmod(self.events_path, 0o600)
                published_at = datetime.now(UTC).isoformat()
                connection.executemany(
                    "UPDATE outbox SET published_at = ? WHERE event_id = ?",
                    [(published_at, event_id) for event_id, _ in rows],
                )
                connection.commit()


class RunLease:
    def __init__(self, handle) -> None:
        self.handle = handle

    def release(self) -> None:
        if self.handle.closed:
            return
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        self.handle.close()


RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("run_id must contain only letters, digits, '_' or '-' (max 64)")
    return run_id


def _state_from_dict(raw: dict[str, Any]) -> ResearchState:
    plan_raw = raw.get("plan")
    plan = None
    if plan_raw:
        plan = ResearchPlan(
            answer_type=plan_raw["answer_type"],
            slots=[AnswerSlot(**slot) for slot in plan_raw["slots"]],
            subgoals=[Subgoal(**subgoal) for subgoal in plan_raw["subgoals"]],
        )
    closure_raw = raw.get("closure")
    closure = None
    if closure_raw:
        closure = ClosureReport(
            **{
                **closure_raw,
                "gaps": [EvidenceGap(**gap) for gap in closure_raw.get("gaps", [])],
                "slot_audits": [
                    SlotGateAudit(**item)
                    for item in closure_raw.get("slot_audits", [])
                ],
            }
        )
    verification_raw = raw.get("verification")
    verification = None
    if verification_raw:
        verification = VerificationReport(
            passed=verification_raw["passed"],
            items=[VerificationItem(**item) for item in verification_raw["items"]],
            provider_passed=verification_raw.get("provider_passed"),
            expected_item_count=int(verification_raw.get("expected_item_count", 0)),
            provider_item_count=int(verification_raw.get("provider_item_count", 0)),
            contract_version=str(verification_raw.get("contract_version", "")),
        )
    return ResearchState(
        run_id=raw["run_id"],
        question=raw["question"],
        status=raw["status"],
        next_node=(raw["next_node"] if "next_node" in raw else _infer_next_node(raw)),
        plan=plan,
        queries=[Query(**query) for query in raw.get("queries", [])],
        pending_queries=[Query(**query) for query in raw.get("pending_queries", [])],
        pending_pages=[Page(**page) for page in raw.get("pending_pages", [])],
        pending_gaps=[EvidenceGap(**gap) for gap in raw.get("pending_gaps", [])],
        input_attachments=[
            InputAttachment(**item) for item in raw.get("input_attachments", [])
        ],
        attachment_observations=[
            AttachmentObservation(
                **{
                    **item,
                    "observations": [
                        GroundedObservation(**observation)
                        for observation in item.get("observations", [])
                    ],
                }
            )
            for item in raw.get("attachment_observations", [])
        ],
        attachment_pages=[Page(**page) for page in raw.get("attachment_pages", [])],
        attachments_ingested=bool(raw.get("attachments_ingested", False)),
        evidence=[Evidence(**item) for item in raw.get("evidence", [])],
        sources=[SourceRecord(**item) for item in raw.get("sources", [])],
        contradiction_checked_slots=raw.get("contradiction_checked_slots", []),
        contradiction_checks=[
            ContradictionAudit(**item)
            for item in raw.get("contradiction_checks", [])
        ],
        last_artifact_id=raw.get("last_artifact_id"),
        handoff_ids=raw.get("handoff_ids", []),
        agent_invocations=[
            AgentInvocation(**item) for item in raw.get("agent_invocations", [])
        ],
        closure=closure,
        draft_answer=raw.get("draft_answer"),
        answer_delivery=(
            dict(raw.get("answer_delivery", {}))
            if isinstance(raw.get("answer_delivery"), dict)
            else {}
        ),
        verification=verification,
        evidence_revision=raw.get("evidence_revision", len(raw.get("evidence", []))),
        closure_revision=raw.get("closure_revision", -1),
        draft_revision=raw.get("draft_revision", -1),
        verification_revision=raw.get("verification_revision", -1),
        methodology=raw.get("methodology", {}),
        operation_replays=raw.get("operation_replays", []),
        operation_replay_details=raw.get("operation_replay_details", []),
        failures=raw.get("failures", []),
        suspension=raw.get("suspension", {}),
        resume_transition=raw.get("resume_transition", {}),
        budget_limits=raw.get("budget_limits", {}),
        budget_ceilings=raw.get("budget_ceilings", {}),
        budget_expansions=raw.get("budget_expansions", []),
        counters=Counters(**raw.get("counters", {})),
    )


def _infer_next_node(raw: dict[str, Any]) -> str:
    if raw.get("status") in {"completed", "verification_failed", "evidence_incomplete", "cancelled"}:
        return "finalize"
    if raw.get("verification") and raw.get("draft_answer"):
        return "verify"
    if raw.get("draft_answer"):
        return "verify"
    if (raw.get("closure") or {}).get("hard_gate_passed"):
        return "draft"
    if raw.get("plan"):
        return "generate_queries"
    return "plan"
