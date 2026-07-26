from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RecoveryAction:
    next_node: str
    retryable: bool
    instruction: str


RECOVERY_POLICY: dict[str, RecoveryAction] = {
    "planning_error": RecoveryAction("plan", True, "Regenerate only missing slots."),
    "query_error": RecoveryAction("generate_queries", True, "Change query strategy."),
    "retrieval_miss": RecoveryAction(
        "generate_queries", True, "Target a different source type or bridge entity."
    ),
    "fetch_error": RecoveryAction("search_and_fetch", True, "Use a fallback fetcher."),
    "evidence_error": RecoveryAction(
        "ingest_evidence", True, "Re-extract evidence with stricter grounding."
    ),
    "citation_error": RecoveryAction(
        "generate_queries", True, "Search only for unsupported answer claims."
    ),
    "model_transport_error": RecoveryAction(
        "plan",
        True,
        "The model TLS handshake failed before request data was sent; retry is safe.",
    ),
    "budget_error": RecoveryAction("draft", False, "Draft with explicit uncertainty."),
    "external_outcome_unknown": RecoveryAction(
        "finalize",
        False,
        "The provider outcome is unknown; require explicit confirmation before retry.",
    ),
    "runtime_error": RecoveryAction("finalize", False, "Stop and preserve diagnostics."),
}


def recovery_for(error_type: str) -> RecoveryAction:
    return RECOVERY_POLICY.get(error_type, RECOVERY_POLICY["runtime_error"])
