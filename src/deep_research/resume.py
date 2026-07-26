"""Exactly-once resume preparation shared by browser and AG-UI entry points."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Any

from .config import AppConfig
from .storage import RunStore, agui_interrupt_index_lock


class ResumePreparationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "invalid",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


@dataclass(slots=True)
class PreparedResume:
    run_id: str
    question: str
    offline: bool
    budget_limits: dict[str, int]
    response: dict[str, Any]
    idempotency_key: str
    replayed: bool
    execution_claimed: bool
    should_start_worker: bool


_STALE_RECOVERY_STATES = frozenset(
    {"initialized", "perceiving", "planning", "running", "drafting"}
)


def prepare_resume(
    config: AppConfig,
    run_id: str,
    payload: dict[str, Any],
    *,
    source: str,
    idempotency_key: str,
    thread_id: str | None = None,
    protocol_run_id: str | None = None,
    parent_run_id: str | None = None,
    interrupt_responses: list[dict[str, Any]] | None = None,
    interrupt_index_lock_held: bool = False,
) -> PreparedResume:
    """Serialize AG-UI resume authorization with cross-run interrupt creation."""
    if source == "agui" and not interrupt_index_lock_held:
        with agui_interrupt_index_lock(config.runs_dir):
            return _prepare_resume_unlocked(
                config,
                run_id,
                payload,
                source=source,
                idempotency_key=idempotency_key,
                thread_id=thread_id,
                protocol_run_id=protocol_run_id,
                parent_run_id=parent_run_id,
                interrupt_responses=interrupt_responses,
            )
    return _prepare_resume_unlocked(
        config,
        run_id,
        payload,
        source=source,
        idempotency_key=idempotency_key,
        thread_id=thread_id,
        protocol_run_id=protocol_run_id,
        parent_run_id=parent_run_id,
        interrupt_responses=interrupt_responses,
    )


def prepare_crash_recovery(
    config: AppConfig,
    run_id: str,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
) -> PreparedResume:
    """Resume a stale worker only after the user confirms uncertain calls.

    A process can stop after an external model request begins but before its
    response is durably committed.  The regular resume path intentionally
    accepts terminal research states only.  This companion path turns the
    stale durable state into a normal, receipt-backed manual resume without
    silently retrying the uncertain request or expanding its budget.
    """
    if not re.fullmatch(r"[A-Za-z0-9:._-]{8,240}", idempotency_key):
        raise ResumePreparationError("invalid resume idempotency key")
    confirmed = _strict_bool(payload, "confirm_ambiguous_retry", default=False)
    if any(
        key in payload
        for key in (
            "additional_iterations",
            "additional_search_calls",
            "additional_pages",
            "recheck_saved_evidence",
        )
    ):
        raise ResumePreparationError(
            "crash recovery keeps the already approved budget; finish this recovery before requesting more budget",
            kind="conflict",
        )

    command = {
        "run_id": run_id,
        "source": "manual",
        "mode": "stale_worker_recovery",
        "confirm_ambiguous_retry": confirmed,
    }
    command_hash = hashlib.sha256(
        json.dumps(command, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    store = RunStore(config.runs_dir, run_id)
    existing = store.resume_receipt(idempotency_key)
    if existing:
        if existing["command_hash"] != command_hash:
            raise ResumePreparationError(
                "resume idempotency key was reused with a different command",
                kind="conflict",
            )
        replay_response = dict(existing["response"])
        replay_response["replayed"] = True
        return _prepared_from_receipt(
            store,
            idempotency_key,
            replay_response,
            execution_claimed=existing["execution_claimed"],
        )

    latest = store.latest_with_id()
    if latest is None:
        raise ResumePreparationError("run not found", kind="not_found")
    checkpoint_id, state = latest
    if state.status not in _STALE_RECOVERY_STATES:
        raise ResumePreparationError(
            f"run is not a stale recovery state; current durable status is {state.status}",
            kind="conflict",
        )
    ambiguous_operations = store.ambiguous_operations()
    if not ambiguous_operations:
        raise ResumePreparationError(
            "stale worker has no uncertain operation; use normal recovery",
            kind="conflict",
        )
    if not confirmed:
        raise ResumePreparationError(
            "explicit confirmation is required because retry may incur another provider charge",
            kind="conflict",
            details={"ambiguous_operations": ambiguous_operations},
        )

    resume_node = str(state.next_node or ambiguous_operations[0]["node"])
    if resume_node in {"", "done", "finalize"}:
        resume_node = str(ambiguous_operations[0]["node"] or "plan")
    prior_status = state.status
    prior_handoff_message_id = state.handoff_ids[-1] if state.handoff_ids else None
    state.status = "initialized"
    state.next_node = resume_node
    state.draft_answer = None
    state.answer_delivery = {}
    state.verification = None
    state.draft_revision = -1
    state.verification_revision = -1
    state.suspension = {}
    state.resume_transition = {
        "schema_version": "deep-research-resume-transition/1.0",
        "resume_receipt_id": idempotency_key,
        "source": "manual",
        "mode": "stale_worker_recovery",
        "status": "authorized",
        "authorized_at": datetime.now(UTC).isoformat(),
        "checkpoint_id_before": checkpoint_id,
        "from_status": prior_status,
        "target_node": resume_node,
        "previous_handoff_message_id": prior_handoff_message_id,
        "ambiguous_operation_retry_confirmed": True,
    }

    configured = {
        "iterations": config.budget.max_iterations,
        "search_calls": config.budget.max_search_calls,
        "pages": config.budget.max_pages,
    }
    prior_limits = {
        key: int(state.budget_limits.get(key, configured[key])) for key in configured
    }
    consumed = {
        "iterations": state.counters.iterations,
        "search_calls": state.counters.search_calls,
        "pages": state.counters.pages_selected,
    }
    # Do not add budget during recovery.  Normalizing to current consumption
    # only prevents a stale checkpoint from claiming less than it has spent.
    effective_limits = {
        key: max(prior_limits[key], consumed[key]) for key in prior_limits
    }
    state.budget_limits = effective_limits
    operation_keys = [str(item["operation_key"]) for item in ambiguous_operations]
    event_payload = {
        "run_id": run_id,
        "status": "queued",
        "source": "manual",
        "mode": "stale_worker_recovery",
        "next_node": resume_node,
        "budget_extensions": {key: 0 for key in effective_limits},
        "budget_configured_before": prior_limits,
        "budget_before": effective_limits,
        "budget_after": effective_limits,
        "budget_consumed": consumed,
        "ambiguous_retry_confirmed": True,
        "ambiguous_operation_keys": operation_keys,
        "ambiguous_operations_confirmed": len(operation_keys),
        "resume_handoff_status": "authorized",
        "previous_handoff_message_id": prior_handoff_message_id,
        "crash_recovery": True,
    }
    committed = store.commit_resume(
        state,
        expected_checkpoint_id=checkpoint_id,
        idempotency_key=idempotency_key,
        command_hash=command_hash,
        source="manual",
        thread_id=None,
        protocol_run_id=None,
        payload=event_payload,
        confirmed_operation_keys=operation_keys,
        interrupt_responses=[],
    )
    if committed["status"] == "conflict":
        raise ResumePreparationError(
            str(committed.get("reason") or "resume commit conflict"),
            kind="conflict",
            details={
                key: value
                for key, value in committed.items()
                if key not in {"status", "reason"}
            },
        )
    response = dict(committed["response"])
    response["replayed"] = committed["status"] == "replayed"
    return _prepared_from_receipt(
        store,
        idempotency_key,
        response,
        execution_claimed=bool(committed.get("execution_claimed")),
    )


def _prepare_resume_unlocked(
    config: AppConfig,
    run_id: str,
    payload: dict[str, Any],
    *,
    source: str,
    idempotency_key: str,
    thread_id: str | None = None,
    protocol_run_id: str | None = None,
    parent_run_id: str | None = None,
    interrupt_responses: list[dict[str, Any]] | None = None,
) -> PreparedResume:
    """CAS, authorize and receipt one resume command before worker startup."""
    if source not in {"manual", "agui"}:
        raise ResumePreparationError("unsupported resume source")
    if not re.fullmatch(r"[A-Za-z0-9:._-]{8,240}", idempotency_key):
        raise ResumePreparationError("invalid resume idempotency key")
    confirm_ambiguous_retry = _strict_bool(
        payload, "confirm_ambiguous_retry", default=False
    )
    recheck_saved_evidence = _strict_bool(
        payload, "recheck_saved_evidence", default=False
    )
    responses = interrupt_responses or []
    command = {
        "run_id": run_id,
        "source": source,
        "thread_id": thread_id,
        "protocol_run_id": protocol_run_id,
        "parent_run_id": parent_run_id,
        "extensions": {
            "iterations": payload.get("additional_iterations"),
            "search_calls": payload.get("additional_search_calls"),
            "pages": payload.get("additional_pages"),
        },
        "confirm_ambiguous_retry": confirm_ambiguous_retry,
        "recheck_saved_evidence": recheck_saved_evidence,
        "interrupt_responses": responses,
    }
    command_hash = hashlib.sha256(
        json.dumps(command, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    store = RunStore(config.runs_dir, run_id)
    existing = store.resume_receipt(idempotency_key)
    if existing:
        if existing["command_hash"] != command_hash:
            raise ResumePreparationError(
                "resume idempotency key was reused with a different command",
                kind="conflict",
            )
        replay_response = dict(existing["response"])
        replay_response["replayed"] = True
        return _prepared_from_receipt(
            store,
            idempotency_key,
            replay_response,
            execution_claimed=existing["execution_claimed"],
        )

    latest = store.latest_with_id()
    if latest is None:
        raise ResumePreparationError("run not found", kind="not_found")
    checkpoint_id, state = latest
    if state.status not in {
        "failed",
        "verification_failed",
        "evidence_incomplete",
        "cancelled",
    }:
        concurrent_receipt = store.resume_receipt(idempotency_key)
        if concurrent_receipt and concurrent_receipt["command_hash"] == command_hash:
            replay_response = dict(concurrent_receipt["response"])
            replay_response["replayed"] = True
            return _prepared_from_receipt(
                store,
                idempotency_key,
                replay_response,
                execution_claimed=concurrent_receipt["execution_claimed"],
            )
        if state.status == "completed":
            raise ResumePreparationError(
                "completed run does not need resume",
                kind="conflict",
            )
        raise ResumePreparationError(
            f"run is not suspended; current durable status is {state.status}",
            kind="conflict",
        )

    if source == "agui":
        open_interrupts = store.open_agui_interrupts()
        response_ids = {str(item.get("interrupt_id", "")) for item in responses}
        open_ids = {item["interrupt_id"] for item in open_interrupts}
        if response_ids != open_ids or not open_ids:
            raise ResumePreparationError(
                "resume must cover the complete open interrupt set",
                kind="conflict",
                details={"open_interrupt_ids": sorted(open_ids)},
            )
        if any(item["thread_id"] != thread_id for item in open_interrupts):
            raise ResumePreparationError(
                "resume thread does not own the open interrupts",
                kind="conflict",
            )
        producing_run_ids = {item["protocol_run_id"] for item in open_interrupts}
        if protocol_run_id in producing_run_ids:
            raise ResumePreparationError(
                "resume must use a new external AG-UI runId",
                kind="conflict",
            )
        if parent_run_id and parent_run_id not in producing_run_ids:
            raise ResumePreparationError(
                "parentRunId does not match the run that produced the interrupt",
                kind="conflict",
            )
        if any(
            item.get("status") not in {"resolved", "cancelled"}
            for item in responses
        ):
            raise ResumePreparationError(
                "interrupt status must be resolved or cancelled",
                kind="conflict",
            )
        reasons = {item["reason"] for item in open_interrupts}
        if state.status not in reasons:
            raise ResumePreparationError(
                "durable run state no longer matches the open interrupt",
                kind="conflict",
            )

        if all(item.get("status") == "cancelled" for item in responses):
            committed = store.commit_interrupt_cancellation(
                expected_checkpoint_id=checkpoint_id,
                idempotency_key=idempotency_key,
                command_hash=command_hash,
                thread_id=str(thread_id),
                protocol_run_id=str(protocol_run_id),
                parent_protocol_run_id=parent_run_id,
                interrupt_responses=responses,
            )
            if committed["status"] == "conflict":
                raise ResumePreparationError(
                    str(committed.get("reason") or "interrupt cancellation conflict"),
                    kind="conflict",
                    details={
                        key: value
                        for key, value in committed.items()
                        if key not in {"status", "reason"}
                    },
                )
            response = dict(committed["response"])
            response["replayed"] = committed["status"] == "replayed"
            return _prepared_from_receipt(
                store,
                idempotency_key,
                response,
                execution_claimed=False,
            )

    failure = state.failures[-1] if state.failures else {}
    ambiguous_operations = store.ambiguous_operations()
    if ambiguous_operations and not confirm_ambiguous_retry:
        raise ResumePreparationError(
            "explicit confirmation is required because retry may incur another provider charge",
            kind="conflict",
            details={"ambiguous_operations": ambiguous_operations},
        )

    resume_node = str(state.suspension.get("resume_node", ""))
    if not resume_node:
        resume_node = str(
            ambiguous_operations[0]["node"]
            if ambiguous_operations
            else failure.get("next_node") or ""
        )
    if not resume_node or resume_node == "finalize":
        resume_node = "generate_queries" if state.plan else "plan"
    recheck_existing_answer = bool(
        recheck_saved_evidence
        and state.status == "verification_failed"
        and str(state.draft_answer or "").strip()
        and state.draft_revision == state.evidence_revision
    )
    # This is explicit because ordinary resume means "continue retrieval".
    # It is useful after a closure-policy or provenance repair: re-evaluate
    # saved evidence before issuing another search or model request.
    if (
        recheck_saved_evidence
        and state.status == "evidence_incomplete"
        and state.plan
        and state.evidence
    ):
        resume_node = "assess_closure"
    elif recheck_existing_answer:
        resume_node = "verify"
    if state.closure and state.closure.gaps:
        state.pending_gaps = list(state.closure.gaps)
    prior_status = state.status
    prior_handoff_message_id = state.handoff_ids[-1] if state.handoff_ids else None
    state.status = "initialized"
    state.next_node = resume_node
    # A regular resume must not show a stale candidate as the new answer. A
    # targeted local recheck of a failed verifier is different: it carries the
    # exact same draft and evidence revision into verification without another
    # writer call.
    if not recheck_existing_answer:
        state.draft_answer = None
        state.answer_delivery = {}
        state.verification = None
        state.draft_revision = -1
        state.verification_revision = -1
    state.suspension = {}
    state.resume_transition = {
        "schema_version": "deep-research-resume-transition/1.0",
        "resume_receipt_id": idempotency_key,
        "source": source,
        "status": "authorized",
        "authorized_at": datetime.now(UTC).isoformat(),
        "checkpoint_id_before": checkpoint_id,
        "from_status": prior_status,
        "target_node": resume_node,
        "previous_handoff_message_id": prior_handoff_message_id,
        "thread_id": thread_id,
        "protocol_run_id": protocol_run_id,
        "parent_protocol_run_id": parent_run_id,
        "recheck_existing_answer": recheck_existing_answer,
    }

    defaults = (0, 0, 0) if source == "agui" else (1, 3, 5)
    extensions = {
        "iterations": _bounded_int(
            payload, "additional_iterations", defaults[0], 5
        ),
        "search_calls": _bounded_int(
            payload, "additional_search_calls", defaults[1], 20
        ),
        "pages": _bounded_int(payload, "additional_pages", defaults[2], 30),
    }
    consumed = {
        "iterations": state.counters.iterations,
        "search_calls": state.counters.search_calls,
        "pages": state.counters.pages_selected,
    }
    configured = {
        "iterations": config.budget.max_iterations,
        "search_calls": config.budget.max_search_calls,
        "pages": config.budget.max_pages,
    }
    configured_ceilings = {
        "iterations": max(
            config.budget.max_total_iterations,
            configured["iterations"],
        ),
        "search_calls": max(
            config.budget.max_total_search_calls,
            configured["search_calls"],
        ),
        "pages": max(config.budget.max_total_pages, configured["pages"]),
    }
    persisted = {
        key: int(state.budget_limits[key])
        if key in state.budget_limits
        else configured[key]
        for key in configured
    }
    persisted_ceilings = (
        state.budget_ceilings
        if isinstance(state.budget_ceilings, dict)
        else {}
    )
    ceilings: dict[str, int] = {}
    for key in configured:
        if key in persisted_ceilings:
            raw_ceiling = persisted_ceilings[key]
            if isinstance(raw_ceiling, bool):
                raise ResumePreparationError(
                    f"persisted budget ceiling for {key} is invalid",
                    kind="conflict",
                )
            try:
                ceiling = int(raw_ceiling)
            except (TypeError, ValueError) as error:
                raise ResumePreparationError(
                    f"persisted budget ceiling for {key} is invalid",
                    kind="conflict",
                ) from error
            if ceiling < 0:
                raise ResumePreparationError(
                    f"persisted budget ceiling for {key} is invalid",
                    kind="conflict",
                )
        else:
            # Legacy checkpoints did not persist ceilings. Preserve an already
            # approved limit, but never raise a ceiling that is already durable.
            ceiling = max(configured_ceilings[key], persisted[key])
        ceilings[key] = ceiling
    effective_before = {
        key: max(persisted[key], consumed[key]) for key in extensions
    }
    remaining = {
        key: max(0, ceilings[key] - effective_before[key]) for key in extensions
    }
    exceeded = {
        key: {
            "requested": extensions[key],
            "remaining": remaining[key],
            "ceiling": ceilings[key],
            "before": effective_before[key],
        }
        for key in extensions
        if extensions[key] > remaining[key]
    }
    if exceeded:
        raise ResumePreparationError(
            "resume budget exceeds the persisted per-run ceiling",
            kind="conflict",
            details={
                "budget_ceiling": ceilings,
                "budget_before": effective_before,
                "budget_remaining": remaining,
                "budget_exceeded": exceeded,
            },
        )
    new_limits = {
        key: effective_before[key] + extensions[key] for key in extensions
    }
    state.budget_limits = new_limits
    state.budget_ceilings = ceilings
    operation_keys = [
        str(item["operation_key"]) for item in ambiguous_operations
    ]
    event_payload = {
        "run_id": run_id,
        "status": "queued",
        "source": source,
        "next_node": resume_node,
        "budget_extensions": extensions,
        "budget_configured_before": persisted,
        "budget_before": effective_before,
        "budget_after": new_limits,
        "budget_ceiling": ceilings,
        "budget_remaining_before": remaining,
        "budget_remaining_after": {
            key: max(0, ceilings[key] - new_limits[key]) for key in new_limits
        },
        "budget_consumed": consumed,
        "ambiguous_retry_confirmed": bool(ambiguous_operations),
        "ambiguous_operation_keys": operation_keys,
        "ambiguous_operations_confirmed": len(ambiguous_operations),
        "protocol_run_id": protocol_run_id,
        "thread_id": thread_id,
        "resume_handoff_status": "authorized",
        "previous_handoff_message_id": prior_handoff_message_id,
    }
    committed = store.commit_resume(
        state,
        expected_checkpoint_id=checkpoint_id,
        idempotency_key=idempotency_key,
        command_hash=command_hash,
        source=source,
        thread_id=thread_id,
        protocol_run_id=protocol_run_id,
        parent_protocol_run_id=parent_run_id,
        payload=event_payload,
        confirmed_operation_keys=operation_keys,
        interrupt_responses=responses,
    )
    if committed["status"] == "conflict":
        raise ResumePreparationError(
            str(committed.get("reason") or "resume commit conflict"),
            kind="conflict",
            details={
                key: value
                for key, value in committed.items()
                if key not in {"status", "reason"}
            },
        )
    response = dict(committed["response"])
    response["replayed"] = committed["status"] == "replayed"
    return _prepared_from_receipt(
        store,
        idempotency_key,
        response,
        execution_claimed=bool(committed.get("execution_claimed")),
    )


def _prepared_from_receipt(
    store: RunStore,
    idempotency_key: str,
    response: dict[str, Any],
    *,
    execution_claimed: bool,
) -> PreparedResume:
    state = store.latest()
    if state is None:
        raise ResumePreparationError("run not found", kind="not_found")
    receipt = store.resume_receipt(idempotency_key)
    if receipt is None:
        raise ResumePreparationError("resume receipt is missing", kind="conflict")
    execution_status = str(receipt.get("execution_status") or "legacy_unverified")
    response = dict(response)
    response["execution_status"] = execution_status
    response["durable_run_status"] = str(
        receipt.get("durable_run_status") or ""
    )
    worker_required = bool(response.get("worker_required", True))
    latest_failure = state.failures[-1] if state.failures else {}
    retryable_worker_failure = bool(
        execution_status == "failed"
        and state.status == "failed"
        and state.suspension
        and latest_failure.get("retryable", True)
    )
    restartable = execution_status in {
        "pending",
        "running",
        "startup_failed",
    } or retryable_worker_failure
    return PreparedResume(
        run_id=store.run_id,
        question=state.question,
        offline=state.methodology.get("model_provider") == "MockModelProvider",
        budget_limits={
            key: int(value)
            for key, value in response.get("budget_after", state.budget_limits).items()
        },
        response=response,
        idempotency_key=idempotency_key,
        replayed=bool(response.get("replayed")),
        execution_claimed=bool(receipt.get("execution_claimed", execution_claimed)),
        should_start_worker=worker_required and restartable,
    )


def _bounded_int(
    payload: dict[str, Any],
    key: str,
    default: int,
    maximum: int,
) -> int:
    try:
        value = payload.get(key, default)
        if isinstance(value, bool):
            raise ValueError
        return max(0, min(maximum, int(value)))
    except (TypeError, ValueError) as error:
        raise ResumePreparationError(f"{key} must be an integer") from error


def _strict_bool(
    payload: dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ResumePreparationError(f"{key} must be a boolean")
    return value
