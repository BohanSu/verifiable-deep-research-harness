from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import UTC, datetime
from email.parser import BytesParser
from email.policy import default as email_policy
import hashlib
import json
import mimetypes
import sqlite3
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlsplit

from .config import AppConfig, normalize_model_choice, normalize_model_profile
from .engine import ExecutionFenceLostError, ResearchEngine
from .methodology import methodology_contract
from .multimodal import (
    MAX_ATTACHMENT_BYTES,
    MAX_ATTACHMENT_COUNT,
    MAX_TOTAL_ATTACHMENT_BYTES,
    SUPPORTED_ATTACHMENT_MEDIA_TYPES,
    validate_attachment,
)
from .providers import (
    MockModelProvider,
    ReplaySearchProvider,
    build_model_team,
    build_providers,
)
from .protocol_index import AgUiProtocolIndex, ProtocolIndexConflict
from .protocols.agui import (
    custom_audit,
    interrupt_response_schema,
    messages_snapshot,
    parse_run_agent_input_detailed,
    run_error,
    run_finished,
    run_started,
    state_snapshot,
)
from .storage import (
    ArtifactIntegrityError,
    RunStore,
    agui_interrupt_index_lock,
    validate_run_id,
)
from .state import ResearchState
from .system_contract import system_contract
from .resume import ResumePreparationError, prepare_crash_recovery, prepare_resume


WEB_ROOT = Path(__file__).resolve().parents[2] / "web"
PROTOCOL_VERIFICATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "官方协议版本核验_20260718.md"
)
_jobs: dict[str, dict[str, object]] = {}
_cancel_events: dict[str, threading.Event] = {}
_agui_stream_counts: dict[str, int] = {}
_jobs_lock = threading.Lock()
_worker_slots = threading.BoundedSemaphore(2)
_stream_slots = threading.BoundedSemaphore(8)


def _durable_job_view(
    job: dict[str, object], state: ResearchState | None
) -> dict[str, object]:
    """Never let an older in-memory job hide a durable terminal checkpoint."""
    projected = dict(job)
    if state is not None and state.status in TERMINAL_RUN_STATES:
        projected["status"] = state.status
        if state.status != "failed":
            projected["error"] = ""
    return projected


def _public_job_view(job: dict[str, object]) -> dict[str, object]:
    """Remove lease credentials while retaining browser-visible job state."""
    projected = dict(job)
    projected.pop("owner_token", None)
    return projected


def _live_usage_snapshot(store: RunStore | None) -> dict[str, object] | None:
    """Return a lightweight, browser-orderable view of the durable usage ledger.

    ``usage_totals`` is the accounting source of truth.  ``snapshot_at`` is
    deliberately not persisted: it lets a browser distinguish two observations
    of the same ledger revision whose in-flight operation count changed.
    """

    if store is None:
        return None
    snapshot = dict(store.usage_totals())
    snapshot["snapshot_at"] = datetime.now(UTC).isoformat()
    return snapshot


def _usage_snapshot_fingerprint(usage: dict[str, object] | None) -> str:
    """Fingerprint ledger content without turning every observation into an update."""

    durable = dict(usage or {})
    durable.pop("snapshot_at", None)
    return hashlib.sha256(
        json.dumps(
            durable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _redact_owner_tokens(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _redact_owner_tokens(item)
            for key, item in value.items()
            if key != "owner_token"
        }
    if isinstance(value, list):
        return [_redact_owner_tokens(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_owner_tokens(item) for item in value)
    return value


MAX_REQUEST_BODY = 32_768
MAX_MULTIPART_REQUEST_BODY = MAX_TOTAL_ATTACHMENT_BYTES + 1_048_576
MAX_QUESTION_LENGTH = 4_000
MAX_EVENT_TAIL_BYTES = 1_048_576
MAX_AUDIT_RESPONSE_BYTES = 512 * 1024
DEFAULT_AUDIT_PAGE_LIMIT = 25
MAX_AUDIT_PAGE_LIMIT = 50
AGUI_ADAPTER_ID = "ag-ui-python-sdk-validated-adapter-v4"
TERMINAL_RUN_STATES = {
    "completed",
    "verification_failed",
    "evidence_incomplete",
    "failed",
    "cancelled",
}
RECOVERABLE_RUN_STATES = {
    "initialized",
    "perceiving",
    "planning",
    "running",
    "drafting",
}
ACTIVE_JOB_STATES = {
    "starting",
    "queued",
    "running",
    "perceiving",
    "planning",
    "drafting",
    "cancelling",
}


class _AgUiLifecycle:
    """Enforce one AG-UI start and at most one terminal event per POST."""

    def __init__(self, writer) -> None:
        self._writer = writer
        self.started = False
        self.terminal = False

    def start(self, thread_id: str, run_id: str) -> None:
        if self.started:
            return
        self.started = True
        self._writer(run_started(thread_id, run_id))

    def finish(self, thread_id: str, run_id: str, **kwargs: object) -> bool:
        if not self.started:
            raise RuntimeError("AG-UI terminal event cannot precede RUN_STARTED")
        if self.terminal:
            return False
        event = run_finished(thread_id, run_id, **kwargs)
        self.terminal = True
        self._writer(event)
        return True

    def error(self, thread_id: str, run_id: str, message: str) -> bool:
        if not self.started:
            raise RuntimeError("AG-UI terminal event cannot precede RUN_STARTED")
        if self.terminal:
            return False
        event = run_error(thread_id, run_id, message)
        self.terminal = True
        self._writer(event)
        return True


class OpenInterruptQueryError(RuntimeError):
    """The thread-wide interrupt index could not be queried safely."""


class AuditResponseTooLargeError(RuntimeError):
    """A single durable audit row cannot fit within the response contract."""


def _decode_audit_cursor(raw: str | None) -> dict[str, object]:
    if not raw:
        return {}
    value = raw.strip()
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode("utf-8")
        payload = json.loads(decoded)
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        try:
            payload = json.loads(value)
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("invalid audit cursor") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("invalid audit cursor")
    ledgers = payload.get("ledgers")
    if not isinstance(ledgers, dict):
        raise ValueError("invalid audit cursor")
    return {
        str(key): value
        for key, value in ledgers.items()
        if str(key)
        in {
            "invocations",
            "handoffs",
            "receipts",
            "source_fetches",
            "artifacts",
            "input_attachments",
            "resume_receipts",
            "worker",
            "external_runs",
            "status_transitions",
            "interrupts",
            "message_snapshots",
        }
    }


def _encode_audit_cursor(ledgers: dict[str, object]) -> str | None:
    if not ledgers:
        return None
    raw = json.dumps(
        {"version": 1, "ledgers": ledgers},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_audit_query(query: str) -> tuple[int | None, dict[str, object] | None]:
    values = parse_qs(query, keep_blank_values=True)
    raw_limits = values.get("audit_limit") or values.get("limit") or []
    raw_cursors = values.get("audit_cursor") or values.get("cursor") or []
    if len(raw_limits) > 1 or len(raw_cursors) > 1:
        raise ValueError("audit limit/cursor must be provided once")
    if not raw_limits and not raw_cursors:
        return None, None
    raw_limit = raw_limits[0] if raw_limits else str(DEFAULT_AUDIT_PAGE_LIMIT)
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as error:
        raise ValueError("audit limit must be an integer") from error
    if limit < 1 or limit > MAX_AUDIT_PAGE_LIMIT:
        raise ValueError(
            f"audit limit must be between 1 and {MAX_AUDIT_PAGE_LIMIT}"
        )
    cursor = _decode_audit_cursor(raw_cursors[0]) if raw_cursors else {}
    return limit, cursor


def _json_size(value: object) -> int:
    return len(
        json.dumps(
            _redact_owner_tokens(value),
            ensure_ascii=False,
        ).encode("utf-8")
    )


def _existing_run_store(runs_dir: Path, run_id: str) -> RunStore | None:
    """Open a durable run only after proving the read cannot create it."""
    run_id = validate_run_id(run_id)
    root = runs_dir.resolve()
    run_dir = (root / run_id).resolve()
    if run_dir.parent != root:
        return None
    if not run_dir.is_dir() or not (run_dir / "checkpoints.sqlite").is_file():
        return None
    return RunStore(runs_dir, run_id)


def _persisted_model_profile(store: RunStore | None) -> str | None:
    if store is None:
        return None
    state = store.latest()
    if state is None or not isinstance(state.methodology, dict):
        return None
    recorded = state.methodology.get("model_profile") or state.methodology.get(
        "model_choice"
    )
    if recorded:
        try:
            return normalize_model_profile(recorded)
        except ValueError:
            return None
    provider = str(state.methodology.get("model_provider", "")).casefold()
    if "deepseek" in provider:
        return "deepseek"
    return None


def _parse_multipart_run_request(
    content_type: str,
    body: bytes,
) -> tuple[dict[str, object], list[tuple[str, str, bytes]]]:
    message = BytesParser(policy=email_policy).parsebytes(
        (
            f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n"
        ).encode("ascii")
        + body
    )
    if not message.is_multipart():
        raise ValueError("multipart/form-data body is malformed")
    fields: dict[str, object] = {}
    files: list[tuple[str, str, bytes]] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        field_name = str(part.get_param("name", header="content-disposition") or "")
        if not field_name:
            raise ValueError("multipart field is missing a name")
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            if field_name != "attachments":
                raise ValueError("file parts must use the attachments field")
            files.append((filename, part.get_content_type(), payload))
            continue
        if field_name in fields:
            raise ValueError(f"multipart field is duplicated: {field_name}")
        if len(payload) > MAX_REQUEST_BODY:
            raise ValueError(f"multipart field is too large: {field_name}")
        try:
            value = payload.decode(part.get_content_charset() or "utf-8")
        except (LookupError, UnicodeDecodeError) as error:
            raise ValueError(f"multipart field is not valid UTF-8: {field_name}") from error
        fields[field_name] = value
    if len(files) > MAX_ATTACHMENT_COUNT:
        raise ValueError(f"at most {MAX_ATTACHMENT_COUNT} attachments are allowed")
    if sum(len(data) for _, _, data in files) > MAX_TOTAL_ATTACHMENT_BYTES:
        raise ValueError("attachments exceed the 24 MB total limit")
    return fields, files


def _request_boolean(value: object, *, field: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().casefold()
    if normalized in {"", "0", "false", "no", "off"}:
        return False
    if normalized in {"1", "true", "yes", "on"}:
        return True
    raise ValueError(f"{field} must be true or false")


def _public_state_dict(state: object) -> dict[str, object]:
    """Project checkpoint state into the browser/API contract without page bodies."""
    payload = state.as_dict()  # type: ignore[attr-defined]
    pending_pages = payload.pop("pending_pages", [])
    payload["pending_page_count"] = len(pending_pages)
    attachment_pages = payload.pop("attachment_pages", [])
    payload["attachment_page_count"] = len(attachment_pages)
    run_id = str(payload.get("run_id") or "")
    for attachment in payload.get("input_attachments", []):
        if not isinstance(attachment, dict):
            continue
        attachment.pop("content_uri", None)
        attachment["content_url"] = (
            f"/api/runs/{run_id}/attachments/{attachment.get('id', '')}"
        )
    return payload


def _public_attachment_audit(store: RunStore) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for raw in store.input_attachment_audit():
        item: dict[str, object] = dict(raw)
        item.pop("content_uri", None)
        item["content_url"] = (
            f"/api/runs/{store.run_id}/attachments/{item.get('id', '')}"
        )
        rows.append(item)
    return rows


def _run_audit_projection(
    store: RunStore,
    *,
    limit: int | None = None,
    cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project all durable audit ledgers through one GET/SSE contract."""

    return _run_audit_projection_page(
        store,
        limit=limit or DEFAULT_AUDIT_PAGE_LIMIT,
        cursor=cursor,
    )


_AUDIT_LEDGER_NAMES = (
    "invocations",
    "handoffs",
    "receipts",
    "source_fetches",
    "artifacts",
    "input_attachments",
    "resume_receipts",
    "worker",
)


def _worker_audit_page(
    store: RunStore,
    *,
    limit: int,
    after: object = 0,
) -> dict[str, object]:
    try:
        offset = max(0, int(after or 0))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid worker audit cursor") from error
    events = _worker_audit_projection(store)
    rows = events[offset : offset + limit + 1]
    visible = rows[:limit]
    has_more = len(rows) > limit
    return {
        "items": visible,
        "has_more": has_more,
        "next_cursor": str(offset + len(visible)) if has_more else None,
    }


def _memory_audit_page(
    items: list[dict[str, object]],
    *,
    limit: int,
    after: object = 0,
) -> dict[str, object]:
    try:
        offset = max(0, int(after or 0))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid protocol audit cursor") from error
    rows = items[offset : offset + limit + 1]
    visible = rows[:limit]
    return {
        "items": visible,
        "has_more": len(rows) > limit,
        "next_cursor": str(offset + len(visible))
        if len(rows) > limit
        else None,
    }


def _protocol_audit_projection(
    config: AppConfig,
    run_id: str,
    store: RunStore,
    *,
    limit: int,
    cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project protocol-control ledgers through the same bounded cursor contract."""
    cursor = cursor or {}
    protocol_index = AgUiProtocolIndex(config.runs_dir)
    external_runs = protocol_index.runs_for_durable(run_id)
    for item in external_runs:
        item["request_hash"] = str(item["request_hash"])[:12]
    status_transitions = protocol_index.status_transitions_for_durable(run_id)
    interrupts = store.agui_interrupt_audit()
    message_snapshots = store.agui_message_snapshot_audit()

    def page_for(name: str, reader, effective_limit: int) -> dict[str, object]:
        if cursor.get(name) == "__done__":
            return {"items": [], "has_more": False, "next_cursor": None}
        return reader(
            limit=effective_limit,
            after=cursor.get(name, 0),
        )

    def build(effective_limit: int) -> dict[str, object]:
        pages = {
            "external_runs": page_for(
                "external_runs",
                lambda *, limit, after: _memory_audit_page(
                    external_runs, limit=limit, after=after
                ),
                effective_limit,
            ),
            "status_transitions": page_for(
                "status_transitions",
                lambda *, limit, after: _memory_audit_page(
                    status_transitions, limit=limit, after=after
                ),
                effective_limit,
            ),
            "interrupts": page_for(
                "interrupts",
                lambda *, limit, after: _memory_audit_page(
                    interrupts, limit=limit, after=after
                ),
                effective_limit,
            ),
            "message_snapshots": page_for(
                "message_snapshots",
                lambda *, limit, after: _memory_audit_page(
                    message_snapshots, limit=limit, after=after
                ),
                effective_limit,
            ),
            "resume_receipts": page_for(
                "resume_receipts", store.resume_receipt_audit_page, effective_limit
            ),
            "worker": page_for(
                "worker",
                lambda *, limit, after: _worker_audit_page(
                    store, limit=limit, after=after
                ),
                effective_limit,
            ),
        }
        next_ledgers: dict[str, object] = {}
        returned: dict[str, int] = {}
        has_more_by_ledger: dict[str, bool] = {}
        has_more = False
        projection: dict[str, object] = {
            "durable_run_id": run_id,
            "execution_lease": store.execution_lease_audit(),
            "limitations": [
                "消息正文不会通过该审计接口返回，只展示 ID、角色和数量。",
                "thread-wide interrupt 的创建、扫描和消费由根目录文件锁协调；这不等于多主机共识。",
                "execution worker 写事务会校验 owner/fence；当前仍是单机本地 SQLite/文件系统设计。",
            ],
        }
        for name in (
            "external_runs",
            "status_transitions",
            "interrupts",
            "message_snapshots",
            "resume_receipts",
            "worker",
        ):
            page = pages[name]
            items = list(page.get("items") or [])
            projection[name] = items
            returned[name] = len(items)
            has_more_by_ledger[name] = bool(page.get("has_more"))
            if bool(page.get("has_more")):
                has_more = True
                next_ledgers[name] = page.get("next_cursor")
            elif items:
                next_ledgers[name] = "__done__"
        next_cursor = _encode_audit_cursor(next_ledgers) if has_more else None
        pagination: dict[str, object] = {
            "window": True,
            "limit": effective_limit,
            "returned": returned,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "response_limit_bytes": MAX_AUDIT_RESPONSE_BYTES,
        }
        for name in (
            "external_runs",
            "status_transitions",
            "interrupts",
            "message_snapshots",
            "resume_receipts",
            "worker",
        ):
            pagination[name] = {
                "window": True,
                "limit": effective_limit,
                "returned_count": returned[name],
                "has_more": has_more_by_ledger[name],
                "next_cursor": next_cursor if has_more_by_ledger[name] else None,
            }
        projection["pagination"] = pagination
        return projection

    effective_limit = max(1, min(MAX_AUDIT_PAGE_LIMIT, int(limit)))
    while True:
        projection = build(effective_limit)
        if _json_size(projection) <= MAX_AUDIT_RESPONSE_BYTES:
            return projection
        if effective_limit == 1:
            raise AuditResponseTooLargeError(
                "one protocol audit record exceeds the response size limit"
            )
        effective_limit = max(1, effective_limit // 2)


def _run_audit_projection_page(
    store: RunStore,
    *,
    limit: int,
    cursor: dict[str, object] | None = None,
) -> dict[str, object]:
    """Project a bounded, independently resumable window of each audit ledger."""
    cursor = cursor or {}

    def page_for(
        name: str,
        reader,
        effective_limit: int,
    ) -> dict[str, object]:
        if cursor.get(name) == "__done__":
            return {"items": [], "has_more": False, "next_cursor": None}
        return reader(limit=effective_limit, after=cursor.get(name, 0))

    def build(effective_limit: int) -> dict[str, object]:
        pages = {
            "invocations": page_for(
                "invocations", store.invocation_rows_page, effective_limit
            ),
            "handoffs": page_for(
                "handoffs", store.handoff_audit_page, effective_limit
            ),
            "receipts": page_for(
                "receipts", store.receipt_audit_page, effective_limit
            ),
            "source_fetches": page_for(
                "source_fetches", store.source_fetch_audit_page, effective_limit
            ),
            "artifacts": page_for(
                "artifacts", store.artifact_manifest_audit_page, effective_limit
            ),
            "input_attachments": page_for(
                "input_attachments",
                lambda *, limit, after: _memory_audit_page(
                    _public_attachment_audit(store), limit=limit, after=after
                ),
                effective_limit,
            ),
            "resume_receipts": page_for(
                "resume_receipts", store.resume_receipt_audit_page, effective_limit
            ),
            "worker": page_for(
                "worker",
                lambda *, limit, after: _worker_audit_page(
                    store, limit=limit, after=after
                ),
                effective_limit,
            ),
        }
        next_ledgers: dict[str, object] = {}
        returned: dict[str, int] = {}
        has_more_by_ledger: dict[str, bool] = {}
        has_more = False
        projection: dict[str, object] = {}
        for name in _AUDIT_LEDGER_NAMES:
            page = pages[name]
            items = list(page.get("items") or [])
            projection[name] = items
            returned[name] = len(items)
            has_more_by_ledger[name] = bool(page.get("has_more"))
            if bool(page.get("has_more")):
                has_more = True
                next_ledgers[name] = page.get("next_cursor")
            elif items:
                # Keep exhausted ledgers from restarting at page one when a
                # different ledger still has more history.
                next_ledgers[name] = "__done__"
        projection["usage"] = store.usage_totals()
        next_cursor = _encode_audit_cursor(next_ledgers) if has_more else None
        pagination: dict[str, object] = {
            "window": True,
            "limit": effective_limit,
            "returned": returned,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "response_limit_bytes": MAX_AUDIT_RESPONSE_BYTES,
        }
        for name in _AUDIT_LEDGER_NAMES:
            pagination[name] = {
                "window": True,
                "limit": effective_limit,
                "returned_count": returned[name],
                "has_more": has_more_by_ledger[name],
                "next_cursor": next_cursor if has_more_by_ledger[name] else None,
            }
        projection["pagination"] = pagination
        return projection

    effective_limit = max(1, min(MAX_AUDIT_PAGE_LIMIT, int(limit)))
    while True:
        projection = build(effective_limit)
        if _json_size(projection) <= MAX_AUDIT_RESPONSE_BYTES:
            return projection
        if effective_limit == 1:
            raise AuditResponseTooLargeError(
                "one durable audit record exceeds the response size limit"
            )
        effective_limit = max(1, effective_limit // 2)


def _worker_audit_projection(store: RunStore) -> list[dict[str, object]]:
    return [
        event
        for event in _read_events(store.events_path)
        if str(event.get("event_type", "")).startswith("worker_")
    ]


def _event_window_projection(
    store: RunStore,
    events: list[dict[str, object]],
) -> dict[str, object]:
    """Describe the bounded event tail without pretending it is full history."""

    total_count = store.published_event_count()
    returned_count = len(events)
    if total_count < returned_count:
        return {
            "returned_count": returned_count,
            "total_count": None,
            "complete": False,
            "first_global_index": None,
            "last_global_index": None,
            "limit": 100,
            "count_status": "legacy_unverified",
            "count_reason": (
                "The JSONL event tail contains records that are not represented "
                "in the durable published outbox count."
            ),
        }
    return {
        "returned_count": returned_count,
        "total_count": total_count,
        "complete": returned_count == total_count,
        "first_global_index": (
            max(1, total_count - returned_count + 1) if returned_count else None
        ),
        "last_global_index": total_count if returned_count else None,
        "limit": 100,
        "count_status": "durable",
        "count_reason": "Global indices derive from the durable published outbox count.",
    }


def _cancel_background_run(run_id: str, reason: str) -> None:
    """Request cooperative cancellation without erasing the durable run state."""
    with _jobs_lock:
        event = _cancel_events.get(run_id)
        current = _jobs.get(run_id, {})
        if event:
            event.set()
        if current.get("status") not in TERMINAL_RUN_STATES:
            _jobs[run_id] = {**current, "status": "cancelling", "error": reason}


def _register_agui_stream(run_id: str) -> int:
    with _jobs_lock:
        count = _agui_stream_counts.get(run_id, 0) + 1
        _agui_stream_counts[run_id] = count
        return count


def _release_agui_stream(run_id: str) -> int:
    with _jobs_lock:
        count = max(0, _agui_stream_counts.get(run_id, 0) - 1)
        if count:
            _agui_stream_counts[run_id] = count
        else:
            _agui_stream_counts.pop(run_id, None)
        return count


def _job_for_lease(
    status: str,
    execution_lease: dict[str, object],
    error: str = "",
) -> dict[str, object]:
    return {
        "status": status,
        "error": error,
        "owner_token": str(execution_lease["owner_token"]),
        "fence": int(execution_lease["fence"]),
    }


def _worker_owns_job(run_id: str, execution_lease: dict[str, object]) -> bool:
    current = _jobs.get(run_id, {})
    return (
        current.get("owner_token") == str(execution_lease["owner_token"])
        and current.get("fence") == int(execution_lease["fence"])
    )


def _claim_resume_execution(
    store: RunStore,
    idempotency_key: str,
    execution_lease: dict[str, object],
) -> bool:
    """Authorize resume execution only with the exact live lease credentials."""
    owner_token = str(execution_lease.get("owner_token", ""))
    fence = int(execution_lease.get("fence", 0))
    expires_at_ms = int(execution_lease.get("expires_at_ms", 0))
    if (
        not owner_token
        or fence <= 0
        or expires_at_ms <= int(time.time() * 1000)
        or str(execution_lease.get("receipt_id", "")) != idempotency_key
    ):
        return False
    return store.claim_resume_execution(
        idempotency_key,
        allow_reclaim=True,
        owner_token=owner_token,
        fence=fence,
    )


def _release_resume_execution_claim(
    store: RunStore,
    idempotency_key: str,
    execution_lease: dict[str, object],
) -> bool:
    return store.release_resume_execution_claim(
        idempotency_key,
        owner_token=str(execution_lease.get("owner_token", "")),
        fence=int(execution_lease.get("fence", 0)),
    )


def _record_worker_audit(
    runs_dir: Path,
    run_id: str,
    execution_lease: dict[str, object],
    event_type: str,
    payload: dict[str, object],
) -> bool:
    """Persist worker failures under the same execution fence as the worker."""
    try:
        store = RunStore(runs_dir, run_id)
        store.bind_execution_fence(
            str(execution_lease["owner_token"]),
            int(execution_lease["fence"]),
        )
        store.event(
            event_type,
            "webapp_worker",
            {
                "run_id": run_id,
                "fence": int(execution_lease["fence"]),
                "receipt_id": str(execution_lease.get("receipt_id", "")),
                **payload,
            },
        )
    except Exception:
        return False
    return True


def _reserve_resume_worker(
    run_id: str,
    execution_lease: dict[str, object] | None = None,
) -> bool:
    with _jobs_lock:
        current_job = _jobs.get(run_id, {})
        current = current_job.get("status")
        active = current in {
            "starting",
            "queued",
            "running",
            "planning",
            "drafting",
            "cancelling",
        }
        newer_fence = bool(
            execution_lease
            and int(execution_lease["fence"])
            > int(current_job.get("fence", 0))
        )
        if active and not newer_fence:
            return False
        _jobs[run_id] = (
            _job_for_lease("starting", execution_lease)
            if execution_lease
            else {"status": "starting", "error": ""}
        )
        return True


def _clear_resume_worker_reservation(
    run_id: str,
    execution_lease: dict[str, object] | None = None,
) -> None:
    with _jobs_lock:
        if (
            _jobs.get(run_id, {}).get("status") in {"starting", "queued"}
            and (execution_lease is None or _worker_owns_job(run_id, execution_lease))
        ):
            _jobs.pop(run_id, None)


def _agui_interrupt_message(status: str) -> str:
    return {
        "cancelled": "研究已取消；持久化现场可由项目恢复接口继续。",
        "verification_failed": "逐句引用核验未通过，需要补充证据后恢复。",
        "evidence_incomplete": "已生成当前可交付回答；仍可补充独立来源、原文位置或反面材料检查后继续研究。",
        "ambiguous_operation": "模型请求结果未知；恢复前必须明确确认可能产生重复费用。",
    }.get(status, "研究未完成，需要人工检查持久化运行状态。")


def _agui_resume_request(
    payload: dict[str, object],
    runs_dir: Path,
    thread_id: str,
    protocol_run_id: str,
    *,
    lock_held: bool = False,
) -> tuple[str, str, dict[str, object]] | None:
    raw_entries = payload.get("resume")
    if raw_entries is None:
        return None
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError("AG-UI resume must be a non-empty array")
    interrupt_records = _open_interrupts_for_thread(
        runs_dir,
        thread_id,
        include_consumed=True,
        lock_held=lock_held,
    )
    by_id = {item["interrupt_id"]: item for item in interrupt_records}
    supplied_ids = {
        str(entry.get("interruptId", ""))
        for entry in raw_entries
        if isinstance(entry, dict)
    }
    if not supplied_ids or not supplied_ids.issubset(by_id) or len(supplied_ids) != len(raw_entries):
        raise ValueError("AG-UI resume references an unknown or duplicate interrupt")
    internal_run_ids = {by_id[item]["internal_run_id"] for item in supplied_ids}
    if len(internal_run_ids) != 1:
        raise ValueError("AG-UI resume cannot combine interrupts from different durable runs")
    internal_run_id = validate_run_id(internal_run_ids.pop())
    idempotency_key = "agui:" + hashlib.sha256(
        f"{thread_id}\0{protocol_run_id}".encode()
    ).hexdigest()
    is_receipt_replay = (
        RunStore(runs_dir, internal_run_id).resume_receipt(idempotency_key)
        is not None
    )
    open_ids = {
        item["interrupt_id"]
        for item in interrupt_records
        if item["status"] == "open"
    }
    if not is_receipt_replay and supplied_ids != open_ids:
        raise ValueError(
            "new AG-UI resume must cover the complete thread-wide open interrupt set"
        )
    normalized_responses: list[dict[str, object]] = []
    resolved_payloads: list[dict[str, object]] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            raise ValueError("AG-UI resume entry must be an object")
        interrupt_id = str(entry.get("interruptId", "")).strip()
        status = str(entry.get("status", "")).strip()
        if status not in {"resolved", "cancelled"}:
            raise ValueError("AG-UI resume status must be resolved or cancelled")
        if status == "cancelled":
            if entry.get("payload") is not None:
                raise ValueError("cancelled AG-UI interrupt must omit payload")
            response_payload: dict[str, object] = {}
        else:
            raw_payload = entry.get("payload")
            if not isinstance(raw_payload, dict):
                raise ValueError("resolved AG-UI interrupt requires an object payload")
            response_payload = dict(raw_payload)
            _validate_agui_interrupt_payload(
                by_id[interrupt_id]["reason"],
                by_id[interrupt_id].get("response_schema"),
                response_payload,
            )
            resolved_payloads.append(response_payload)
        normalized_responses.append(
            {
                "interrupt_id": interrupt_id,
                "status": status,
                "payload": response_payload,
            }
        )
    def maximum(field: str) -> int:
        return max((int(item.get(field, 0)) for item in resolved_payloads), default=0)

    prepared_payload: dict[str, object] = {
        "additional_iterations": maximum("additionalIterations"),
        "additional_search_calls": maximum("additionalSearchCalls"),
        "additional_pages": maximum("additionalPages"),
        "confirm_ambiguous_retry": any(
            item.get("confirmAmbiguousRetry") is True for item in resolved_payloads
        ),
        "interrupt_responses": normalized_responses,
    }
    return internal_run_id, str(raw_entries[0]["interruptId"]), prepared_payload


def _validate_agui_interrupt_payload(
    reason: str,
    persisted_schema: object,
    payload: dict[str, object],
) -> None:
    schema = (
        persisted_schema
        if isinstance(persisted_schema, dict)
        else interrupt_response_schema(reason)
    )
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    for field in required:
        if field not in payload:
            raise ValueError(f"interrupt payload is missing required field {field}")
    if schema.get("additionalProperties") is False:
        unknown = set(payload) - set(properties)
        if unknown:
            raise ValueError(f"interrupt payload has unsupported fields: {sorted(unknown)}")
    for field, value in payload.items():
        rule = properties.get(field, {})
        if "const" in rule and value != rule["const"]:
            raise ValueError(
                f"interrupt payload field {field} must equal {rule['const']!r}"
            )
        expected_type = rule.get("type")
        if expected_type == "integer" and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(f"interrupt payload field {field} must be an integer")
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"interrupt payload field {field} must be a boolean")
        if expected_type == "string" and not isinstance(value, str):
            raise ValueError(f"interrupt payload field {field} must be a string")
        if isinstance(value, int) and not isinstance(value, bool):
            if "minimum" in rule and value < int(rule["minimum"]):
                raise ValueError(f"interrupt payload field {field} is below minimum")
            if "maximum" in rule and value > int(rule["maximum"]):
                raise ValueError(f"interrupt payload field {field} exceeds maximum")


_LEGACY_RUN_TABLE_SIGNATURES = {
    "checkpoints": {"id", "created_at", "node", "state_json"},
    "outbox": {"event_id", "created_at", "event_json", "published_at"},
    "operations": {"operation_key", "node", "semantic_input_hash", "status"},
    "usage_ledger": {"operation_key", "model_calls", "input_tokens", "output_tokens"},
}


def _ensure_agui_interrupt_query_schema(
    connection: sqlite3.Connection,
    run_name: str,
) -> None:
    """Migrate only recognized pre-AG-UI run stores; unknown DBs fail closed."""

    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    if "agui_interrupts" not in tables:
        durable_core_schema = all(
            table_name in tables
            for table_name in ("checkpoints", "outbox", "operations")
        )
        for table_name, required_columns in _LEGACY_RUN_TABLE_SIGNATURES.items():
            if table_name not in tables:
                continue
            columns = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            if not required_columns.issubset(columns):
                raise OpenInterruptQueryError(
                    f"AG-UI interrupt table is missing in unrecognized run {run_name}"
                )
        if not durable_core_schema:
            if "checkpoints" not in tables:
                raise OpenInterruptQueryError(
                    f"AG-UI interrupt table is missing in unrecognized run {run_name}"
                )
            latest = connection.execute(
                "SELECT state_json FROM checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
            try:
                state = json.loads(str(latest[0])) if latest else None
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OpenInterruptQueryError(
                    f"legacy checkpoint identity is invalid in run {run_name}"
                ) from error
            if (
                not isinstance(state, dict)
                or state.get("run_id") != run_name
                or not isinstance(state.get("question"), str)
                or not isinstance(state.get("status"), str)
            ):
                raise OpenInterruptQueryError(
                    f"legacy checkpoint identity is invalid in run {run_name}"
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

    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(agui_interrupts)")
    }
    required = {
        "interrupt_id",
        "thread_id",
        "protocol_run_id",
        "reason",
        "status",
        "created_at",
    }
    if not required.issubset(columns):
        raise OpenInterruptQueryError(
            f"AG-UI interrupt table is malformed in run {run_name}"
        )
    if "response_schema_json" not in columns:
        connection.execute(
            "ALTER TABLE agui_interrupts ADD COLUMN response_schema_json TEXT"
        )
    connection.commit()


def _open_interrupts_for_thread(
    runs_dir: Path,
    thread_id: str,
    *,
    include_consumed: bool = False,
    lock_held: bool = False,
) -> list[dict[str, object]]:
    if lock_held:
        return _scan_open_interrupts_for_thread(
            runs_dir,
            thread_id,
            include_consumed=include_consumed,
        )
    with agui_interrupt_index_lock(runs_dir):
        return _scan_open_interrupts_for_thread(
            runs_dir,
            thread_id,
            include_consumed=include_consumed,
        )


def _scan_open_interrupts_for_thread(
    runs_dir: Path,
    thread_id: str,
    *,
    include_consumed: bool = False,
) -> list[dict[str, object]]:
    open_interrupts: list[dict[str, object]] = []
    try:
        try:
            runs_dir.stat()
        except FileNotFoundError:
            return open_interrupts
        if not runs_dir.is_dir():
            raise OpenInterruptQueryError("AG-UI runs directory is not a directory")
        run_dirs = list(runs_dir.iterdir())
    except OSError as error:
        raise OpenInterruptQueryError(
            "AG-UI open-interrupt query could not read the runs directory"
        ) from error

    for run_dir in run_dirs:
        database = run_dir / "checkpoints.sqlite"
        if not run_dir.is_dir() or not database.is_file():
            continue
        try:
            with contextlib.closing(
                sqlite3.connect(database, timeout=10)
            ) as connection, connection:
                connection.execute("PRAGMA busy_timeout = 10000")
                _ensure_agui_interrupt_query_schema(connection, run_dir.name)
                status_clause = "" if include_consumed else " AND status = 'open'"
                rows = connection.execute(
                    f"""
                    SELECT interrupt_id, protocol_run_id, reason, status,
                           response_schema_json
                    FROM agui_interrupts
                    WHERE thread_id = ?{status_clause}
                    """,
                    (thread_id,),
                ).fetchall()
                records = [
                    {
                        "interrupt_id": str(row[0]),
                        "protocol_run_id": str(row[1]),
                        "reason": str(row[2]),
                        "internal_run_id": run_dir.name,
                        "status": str(row[3]),
                        "response_schema": json.loads(row[4]) if row[4] else None,
                    }
                    for row in rows
                ]
        except OpenInterruptQueryError:
            raise
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            raise OpenInterruptQueryError(
                f"AG-UI open-interrupt query failed for run {run_dir.name}"
            ) from error
        open_interrupts.extend(records)
    open_interrupts.sort(
        key=lambda item: (str(item["internal_run_id"]), str(item["interrupt_id"]))
    )
    return open_interrupts


def _request_metadata_allowed(
    host: str,
    origin: str | None,
    sec_fetch_site: str | None,
    server_port: int,
) -> bool:
    """Validate loopback authority and reject browser requests from another origin."""
    if not host or any(ord(character) <= 32 for character in host):
        return False
    try:
        authority = urlsplit(f"//{host}")
        hostname = (authority.hostname or "").casefold()
        port = authority.port or server_port
    except ValueError:
        return False
    if (
        authority.scheme
        or authority.username is not None
        or authority.password is not None
        or authority.path
        or authority.query
        or authority.fragment
    ):
        return False
    if hostname not in {"127.0.0.1", "localhost", "::1"} or port != server_port:
        return False
    fetch_site = (sec_fetch_site or "").casefold()
    if fetch_site not in {"", "none", "same-origin"}:
        return False
    if not origin:
        return True
    if any(ord(character) <= 32 for character in origin):
        return False
    try:
        parsed = urlparse(origin)
        origin_host = (parsed.hostname or "").casefold()
        origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.username is None
        and parsed.password is None
        and origin_host == hostname
        and origin_port == server_port
        and not parsed.path
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )


class ResearchRequestHandler(BaseHTTPRequestHandler):
    server_version = "DeepResearchUI/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            if not self._request_origin_allowed():
                self._json(
                    {"error": "request origin is not allowed"},
                    HTTPStatus.FORBIDDEN,
                )
                return
        if parsed.path == "/api/config":
            self._config()
            return
        if parsed.path == "/api/methodology":
            self._methodology()
            return
        if parsed.path == "/api/system-contract":
            self._json(system_contract())
            return
        if parsed.path == "/api/protocol-verification":
            self._protocol_verification()
            return
        if parsed.path == "/api/runs":
            self._run_list()
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/stream"):
            run_id = parsed.path.removeprefix("/api/runs/").removesuffix("/stream").rstrip("/")
            self._event_stream(run_id, parsed.query)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/protocol-audit"):
            run_id = parsed.path.removeprefix("/api/runs/").removesuffix(
                "/protocol-audit"
            ).rstrip("/")
            self._protocol_audit(run_id, parsed.query)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/usage"):
            run_id = parsed.path.removeprefix("/api/runs/").removesuffix(
                "/usage"
            ).rstrip("/")
            self._run_usage(run_id)
            return
        snapshot_parts = parsed.path.strip("/").split("/")
        if (
            len(snapshot_parts) == 5
            and snapshot_parts[:2] == ["api", "runs"]
            and snapshot_parts[3] == "artifacts"
        ):
            self._artifact_snapshot(snapshot_parts[2], snapshot_parts[4])
            return
        if (
            len(snapshot_parts) == 5
            and snapshot_parts[:2] == ["api", "runs"]
            and snapshot_parts[3] == "attachments"
        ):
            self._input_attachment(snapshot_parts[2], snapshot_parts[4])
            return
        if (
            len(snapshot_parts) == 6
            and snapshot_parts[:2] == ["api", "runs"]
            and snapshot_parts[3] == "sources"
            and snapshot_parts[5] == "snapshot"
        ):
            self._source_snapshot(snapshot_parts[2], snapshot_parts[4], parsed.query)
            return
        if parsed.path.startswith("/api/runs/"):
            self._run_state(
                parsed.path.removeprefix("/api/runs/"),
                parsed.query,
            )
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        request_path = urlparse(self.path).path
        if request_path == "/api/ag-ui":
            self._agui_run()
            return
        if request_path.startswith("/api/runs/") and request_path.endswith("/resume"):
            run_id = request_path.removeprefix("/api/runs/").removesuffix("/resume").rstrip("/")
            self._resume_run(run_id)
            return
        if request_path != "/api/runs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        slot_acquired = False
        try:
            if not self._request_origin_allowed():
                self._json({"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
                return
            media_type = self.headers.get_content_type()
            raw_content_type = str(self.headers.get("Content-Type", ""))
            if media_type not in {"application/json", "multipart/form-data"}:
                self._json(
                    {"error": "Content-Type must be application/json or multipart/form-data"},
                    HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                )
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            body_limit = (
                MAX_MULTIPART_REQUEST_BODY
                if media_type == "multipart/form-data"
                else MAX_REQUEST_BODY
            )
            if content_length <= 0 or content_length > body_limit:
                self._json({"error": "request body is too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            body = self.rfile.read(content_length)
            if media_type == "multipart/form-data":
                payload, file_parts = _parse_multipart_run_request(
                    raw_content_type,
                    body,
                )
            else:
                payload = json.loads(body or b"{}")
                file_parts = []
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
            question = str(payload.get("question", "")).strip()
            offline = _request_boolean(payload.get("offline", False), field="offline")
            config = AppConfig.from_env()
            requested_profile = payload.get("profile", payload.get("model"))
            model_profile = config.select_profile(requested_profile)
            validated_files: list[tuple[str, str, str, bytes]] = []
            for filename, declared_media_type, data in file_parts:
                clean_name, detected_media_type, modality = validate_attachment(
                    filename,
                    declared_media_type,
                    data,
                )
                validated_files.append(
                    (clean_name, detected_media_type, modality, data)
                )
            required_modalities = {item[2] for item in validated_files}
            if not question:
                self._json({"error": "question is required"}, HTTPStatus.BAD_REQUEST)
                return
            if len(question) > MAX_QUESTION_LENGTH:
                self._json({"error": "question is too long"}, HTTPStatus.BAD_REQUEST)
                return
            if offline and required_modalities - {"text", "document"}:
                raise ValueError(
                    "offline replay can parse text documents but cannot perceive image or audio attachments"
                )
            if not offline:
                config.require_online_profile(
                    model_profile,
                    required_modalities=required_modalities,
                )
                config.require_search_provider()
            if not _worker_slots.acquire(blocking=False):
                self._json(
                    {"error": "too many active research jobs"},
                    HTTPStatus.TOO_MANY_REQUESTS,
                )
                return
            slot_acquired = True
            run_id = uuid.uuid4().hex[:12]
            store = RunStore(config.runs_dir, run_id)
            stored_attachments = [
                store.store_input_attachment(
                    name=name,
                    media_type=attachment_media_type,
                    modality=modality,
                    data=data,
                )
                for name, attachment_media_type, modality, data in validated_files
            ]
            initial_state = ResearchState(
                run_id=run_id,
                question=question,
                status="initialized",
                next_node="perceive_inputs" if stored_attachments else "plan",
                input_attachments=stored_attachments,
                methodology={
                    "model_profile": model_profile,
                    "model_choice": model_profile,
                    "model_provider": (
                        "MockModelProvider"
                        if offline
                        else "ModelProviderTeam"
                        if model_profile == "team"
                        else "OpenAICompatibleModelProvider"
                    ),
                },
            )
            store.checkpoint("queued", initial_state)
            execution_lease = store.acquire_execution_lease(f"producer:{run_id}")
            if execution_lease is None:
                raise RuntimeError("failed to acquire execution lease for new run")
            with _jobs_lock:
                _jobs[run_id] = _job_for_lease("queued", execution_lease)
                _cancel_events[run_id] = threading.Event()
            thread = threading.Thread(
                target=_run_in_background,
                args=(run_id, question, offline, None, execution_lease),
                kwargs={
                    "runs_dir": config.runs_dir,
                    "model_profile": model_profile,
                },
                daemon=True,
            )
            try:
                thread.start()
            except Exception:
                store.release_execution_lease(
                    str(execution_lease["owner_token"]),
                    int(execution_lease["fence"]),
                )
                _clear_resume_worker_reservation(run_id, execution_lease)
                raise
            slot_acquired = False
            self._json(
                {
                    "run_id": run_id,
                    "status": "queued",
                    "model": model_profile if not offline else "offline",
                    "profile": model_profile if not offline else "offline",
                    "attachments": [
                        {
                            "id": item.id,
                            "name": item.name,
                            "media_type": item.media_type,
                            "modality": item.modality,
                            "sha256": item.sha256,
                            "byte_length": item.byte_length,
                        }
                        for item in stored_attachments
                    ],
                },
                HTTPStatus.ACCEPTED,
            )
        except (ValueError, json.JSONDecodeError) as error:
            if slot_acquired:
                _worker_slots.release()
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            if slot_acquired:
                _worker_slots.release()
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _agui_run(self) -> None:
        worker_acquired = False
        stream_acquired = False
        headers_sent = False
        internal_run_id: str | None = None
        protocol_run_id: str | None = None
        thread_id: str | None = None
        resumed_from_interrupt: str | None = None
        resumed_interrupt_ids: list[str] = []
        protocol_messages: list[dict[str, object]] = []
        protocol_index: AgUiProtocolIndex | None = None
        external_run_replay = False
        agui_stream_registered = False
        interrupt_index_stack = contextlib.ExitStack()
        lifecycle = _AgUiLifecycle(self._write_agui_data)
        try:
            if not self._request_origin_allowed():
                self._json({"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
                return
            if self.headers.get_content_type() != "application/json":
                self._json({"error": "Content-Type must be application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_REQUEST_BODY:
                self._json({"error": "request body is too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            payload = json.loads(self.rfile.read(content_length))
            parsed_input = parse_run_agent_input_detailed(payload)
            thread_id = parsed_input.thread_id
            requested_run_id = parsed_input.requested_run_id
            question = parsed_input.question
            if len(question) > MAX_QUESTION_LENGTH:
                self._json({"error": "question is too long"}, HTTPStatus.BAD_REQUEST)
                return
            protocol_run_id = requested_run_id or uuid.uuid4().hex[:12]
            config = AppConfig.from_env()
            # Keep the cross-run interrupt index locked from the snapshot
            # through resume CAS (or the no-resume admission decision).
            resume_lock_held = True
            if resume_lock_held:
                interrupt_index_stack.enter_context(
                    agui_interrupt_index_lock(config.runs_dir)
                )
            resume_request = _agui_resume_request(
                payload,
                config.runs_dir,
                thread_id,
                protocol_run_id,
                lock_held=resume_lock_held,
            )
            internal_run_id = (
                resume_request[0] if resume_request else uuid.uuid4().hex[:12]
            )
            if resume_request:
                resumed_from_interrupt = resume_request[1]
                resumed_interrupt_ids = [
                    str(item["interrupt_id"])
                    for item in resume_request[2]["interrupt_responses"]
                ]
            if not resume_request:
                open_interrupts = _open_interrupts_for_thread(
                    config.runs_dir,
                    thread_id,
                    lock_held=True,
                )
                if open_interrupts:
                    self._json(
                        {
                            "error": "thread has unresolved interrupts; send resume[] before a new question",
                            "open_interrupts": open_interrupts,
                        },
                        HTTPStatus.CONFLICT,
                    )
                    return
            if not _worker_slots.acquire(blocking=False):
                self._json({"error": "too many active research jobs"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            worker_acquired = True
            if not _stream_slots.acquire(blocking=False):
                _worker_slots.release()
                worker_acquired = False
                self._json({"error": "too many active event streams"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            stream_acquired = True
            protocol_index = AgUiProtocolIndex(config.runs_dir)
            request_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()
            registration = protocol_index.register_run(
                thread_id=thread_id,
                run_id=protocol_run_id,
                durable_run_id=internal_run_id,
                kind="resume" if resume_request else "producer",
                parent_run_id=str(payload.get("parentRunId", "")) or None,
                request_hash=request_hash,
            )
            external_run_replay = registration["status"] == "replay"
            registered_internal_run_id = validate_run_id(
                str(registration["durable_run_id"])
            )
            if resume_request and registered_internal_run_id != internal_run_id:
                raise ProtocolIndexConflict(
                    "registered AG-UI resume points to another durable run"
                )
            internal_run_id = registered_internal_run_id
            protocol_store = RunStore(config.runs_dir, internal_run_id)
            if parsed_input.messages:
                protocol_store.save_agui_messages(
                    thread_id,
                    parsed_input.messages,
                )
                protocol_messages = parsed_input.messages
            else:
                protocol_messages = protocol_store.load_agui_messages(thread_id)
            if not protocol_messages and question:
                protocol_messages = [
                    {
                        "id": f"deep-research-question-{internal_run_id}",
                        "role": "user",
                        "content": question,
                    }
                ]
                protocol_store.save_agui_messages(thread_id, protocol_messages)
            budget_limits = None
            model_profile: str | None = None
            resume_store: RunStore | None = None
            prepared = None
            preflight_lease: dict[str, object] | None = None
            if resume_request:
                resume_payload = resume_request[2]
                idempotency_key = "agui:" + hashlib.sha256(
                    f"{thread_id}\0{protocol_run_id}".encode()
                ).hexdigest()
                resume_store = RunStore(config.runs_dir, internal_run_id)
                requires_worker = any(
                    item.get("status") != "cancelled"
                    for item in resume_payload["interrupt_responses"]
                )
                if requires_worker:
                    preflight_lease = resume_store.acquire_execution_lease(
                        idempotency_key
                    )
                    if preflight_lease is None:
                        raise ResumePreparationError(
                            "run is already active or its execution lease is held",
                            kind="conflict",
                        )
                try:
                    prepared = prepare_resume(
                        config,
                        internal_run_id,
                        resume_payload,
                        source="agui",
                        idempotency_key=idempotency_key,
                        thread_id=thread_id,
                        protocol_run_id=protocol_run_id,
                        parent_run_id=str(payload.get("parentRunId", "")) or None,
                        interrupt_responses=[
                            dict(item)
                            for item in resume_payload["interrupt_responses"]
                        ],
                        interrupt_index_lock_held=True,
                    )
                except Exception:
                    if preflight_lease is not None:
                        resume_store.release_execution_lease(
                            str(preflight_lease["owner_token"]),
                            int(preflight_lease["fence"]),
                        )
                    raise
                question = prepared.question
                offline = prepared.offline
                budget_limits = prepared.budget_limits
            else:
                forwarded_props = payload.get("forwardedProps", {})
                offline = bool(
                    forwarded_props.get("offline", False)
                    if isinstance(forwarded_props, dict)
                    else False
                )
                requested_model = (
                    forwarded_props.get("profile", forwarded_props.get("model"))
                    if isinstance(forwarded_props, dict)
                    else None
                )
                model_profile = config.select_profile(requested_model)
                if not offline:
                    config.require_online_profile(model_profile)
                    config.require_search_provider()
            # The complete thread-wide set has now been validated and, for a
            # resume, atomically consumed. Do not hold the cross-run lock while
            # the worker or SSE stream is running.
            interrupt_index_stack.close()
            should_start = False
            execution_lease: dict[str, object] | None = None
            reservation_held = False
            if resume_request:
                execution_lease = preflight_lease
                if not prepared.should_start_worker and execution_lease is not None:
                    resume_store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                    execution_lease = None
                if prepared.should_start_worker and execution_lease is None:
                    raise ResumePreparationError(
                        "resume requires an execution lease but none was acquired",
                        kind="conflict",
                    )
                try:
                    reservation_held = bool(
                        execution_lease
                        and _reserve_resume_worker(
                            internal_run_id, execution_lease
                        )
                    )
                    if execution_lease:
                        should_start = (
                            reservation_held
                            and _claim_resume_execution(
                                resume_store,
                                prepared.idempotency_key,
                                execution_lease,
                            )
                        )
                except Exception:
                    if execution_lease is not None:
                        _clear_resume_worker_reservation(
                            internal_run_id, execution_lease
                        )
                        resume_store.release_execution_lease(
                            str(execution_lease["owner_token"]),
                            int(execution_lease["fence"]),
                        )
                    raise
                if execution_lease and not should_start:
                    _clear_resume_worker_reservation(
                        internal_run_id, execution_lease
                    )
                    resume_store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                    execution_lease = None
                    raise ResumePreparationError(
                        "resume execution claim was not authorized by the active lease",
                        kind="conflict",
                    )
            else:
                latest_existing_state = protocol_store.latest()
                eligible = bool(
                    not external_run_replay
                    or latest_existing_state is None
                    or latest_existing_state.status in RECOVERABLE_RUN_STATES
                )
                if eligible:
                    execution_lease = protocol_store.acquire_execution_lease(
                        f"producer:{protocol_run_id}"
                    )
                reservation_held = bool(
                    execution_lease
                    and _reserve_resume_worker(internal_run_id, execution_lease)
                )
                should_start = reservation_held
                if execution_lease and not should_start:
                    protocol_store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
            if should_start:
                with _jobs_lock:
                    _jobs[internal_run_id] = _job_for_lease("queued", execution_lease)
                    _cancel_events[internal_run_id] = threading.Event()
                worker_thread = threading.Thread(
                    target=_run_in_background,
                    args=(
                        internal_run_id,
                        question,
                        offline,
                        budget_limits,
                        execution_lease,
                    ),
                    kwargs={
                        "runs_dir": config.runs_dir,
                        "model_profile": model_profile,
                    },
                    daemon=True,
                )
                try:
                    worker_thread.start()
                except Exception:
                    protocol_store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                    if resume_request:
                        _release_resume_execution_claim(
                            resume_store,
                            prepared.idempotency_key,
                            execution_lease,
                        )
                    _clear_resume_worker_reservation(internal_run_id, execution_lease)
                    raise
                protocol_index.mark_status(protocol_run_id, "queued")
                worker_acquired = False
            else:
                _worker_slots.release()
                worker_acquired = False
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "X-Deep-Research-Protocol",
                AGUI_ADAPTER_ID,
            )
            self.end_headers()
            headers_sent = True
            _register_agui_stream(internal_run_id)
            agui_stream_registered = True
            lifecycle.start(thread_id, protocol_run_id)
            # A cancelled resume has no worker by contract. A resolved resume
            # replay may also have no worker after its original execution has
            # completed, but it must re-project the terminal result instead of
            # being misclassified as a cancellation.
            if (
                resume_request
                and prepared is not None
                and not bool(prepared.response.get("worker_required", True))
            ):
                cancellation_store = RunStore(config.runs_dir, internal_run_id)
                state = cancellation_store.latest()
                if state is not None:
                    self._write_agui_data(state_snapshot(_public_state_dict(state)))
                    self._write_agui_data(
                        messages_snapshot(protocol_messages)
                    )
                self._write_agui_data(
                    custom_audit(
                        {
                            "adapter": AGUI_ADAPTER_ID,
                            "protocol_run_id": protocol_run_id,
                            "deep_research_run_id": internal_run_id,
                            "cancelled_interrupt_ids": resumed_interrupt_ids,
                            "worker_started": False,
                            "checkpoint_id_before": prepared.response.get(
                                "checkpoint_id_before"
                            ),
                            "checkpoint_id_after": prepared.response.get(
                                "checkpoint_id_after"
                            ),
                            "open_interrupt_count": len(
                                cancellation_store.open_agui_interrupts()
                            ),
                        }
                    )
                )
                protocol_index.mark_status(protocol_run_id, "interrupt_cancelled")
                lifecycle.finish(
                    thread_id,
                    protocol_run_id,
                    result={
                        "outcome": "interrupt_cancelled",
                        "deep_research_run_id": internal_run_id,
                    },
                    success=True,
                )
                return
            last_fingerprint = ""
            while True:
                with _jobs_lock:
                    raw_job = dict(_jobs.get(internal_run_id, {}))
                state = RunStore(config.runs_dir, internal_run_id).latest()
                job = _durable_job_view(raw_job, state)
                events = _read_events(
                    config.runs_dir / internal_run_id / "events.jsonl"
                )
                status = job.get("status") or (state.status if state else "queued")
                fingerprint = f"{status}|{len(events)}|{len(state.handoff_ids) if state else 0}"
                if fingerprint != last_fingerprint:
                    if state is not None:
                        self._write_agui_data(state_snapshot(_public_state_dict(state)))
                    self._write_agui_data(
                        custom_audit(
                            {
                                "adapter": AGUI_ADAPTER_ID,
                                "protocol_run_id": protocol_run_id,
                                "deep_research_run_id": internal_run_id,
                                "resumed_from_interrupt": resumed_from_interrupt,
                                "resumed_interrupt_ids": resumed_interrupt_ids,
                                "external_run_registry": {
                                    "run_id": protocol_run_id,
                                    "thread_id": thread_id,
                                    "replayed": external_run_replay,
                                    "globally_unique": True,
                                },
                                "job": _public_job_view(job),
                                "events": events[-20:],
                            }
                        )
                    )
                    last_fingerprint = fingerprint
                if not raw_job and (
                    state is None or state.status in RECOVERABLE_RUN_STATES
                ):
                    protocol_index.mark_status(protocol_run_id, "worker_unavailable")
                    lifecycle.error(
                        thread_id,
                        protocol_run_id,
                        "durable run has no active worker; use a recovery endpoint",
                    )
                    return
                latest_failure = state.failures[-1] if state and state.failures else {}
                resumable_failure = bool(
                    status == "failed" and latest_failure.get("retryable")
                )
                if status == "failed" and not resumable_failure:
                    protocol_index.mark_status(protocol_run_id, "failed")
                    lifecycle.error(
                        thread_id,
                        protocol_run_id,
                        str(job.get("error") or status),
                    )
                    return
                if status in {
                    "completed",
                    "verification_failed",
                    "evidence_incomplete",
                    "cancelled",
                } or resumable_failure:
                    terminal_reason = (
                        str(latest_failure.get("type") or "failed")
                        if resumable_failure
                        else status
                    )
                    interrupt_id = None
                    terminal_response_schema = None
                    if status != "completed":
                        terminal_store = RunStore(config.runs_dir, internal_run_id)
                        terminal_response_schema = interrupt_response_schema(
                            terminal_reason
                        )
                        interrupt_id = terminal_store.create_agui_interrupt(
                            thread_id,
                            protocol_run_id,
                            terminal_reason,
                            terminal_response_schema,
                        )
                    terminal_result = {
                        "outcome": terminal_reason,
                        "run_status": status,
                        "deep_research_run_id": internal_run_id,
                    }
                    if terminal_reason == "ambiguous_operation":
                        terminal_result["ambiguous_operations"] = RunStore(
                            config.runs_dir, internal_run_id
                        ).ambiguous_operations()
                    if state is not None:
                        self._write_agui_data(
                            state_snapshot(_public_state_dict(state))
                        )
                        self._write_agui_data(
                            messages_snapshot(protocol_messages)
                        )
                    protocol_index.mark_status(protocol_run_id, terminal_reason)
                    self._write_agui_data(
                        custom_audit(
                            {
                                "adapter": AGUI_ADAPTER_ID,
                                "outcome": terminal_reason,
                                "terminal": True,
                            }
                        )
                    )
                    lifecycle.finish(
                        thread_id,
                        protocol_run_id,
                        result=terminal_result,
                        success=status == "completed",
                        interrupt_reason=(
                            None if status == "completed" else terminal_reason
                        ),
                        interrupt_message=(
                            None
                            if status == "completed"
                            else _agui_interrupt_message(terminal_reason)
                        ),
                        interrupt_id=interrupt_id,
                        response_schema=terminal_response_schema,
                    )
                    return
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(0.8)
        except (BrokenPipeError, ConnectionResetError):
            remaining_streams = 0
            if internal_run_id and agui_stream_registered:
                remaining_streams = _release_agui_stream(internal_run_id)
                agui_stream_registered = False
            if internal_run_id and remaining_streams == 0:
                with _jobs_lock:
                    current_status = _jobs.get(internal_run_id, {}).get("status")
                if (
                    thread_id
                    and protocol_run_id
                    and current_status not in TERMINAL_RUN_STATES
                ):
                    store = RunStore(config.runs_dir, internal_run_id)
                    if not store.open_agui_interrupts():
                        store.create_agui_interrupt(
                            thread_id,
                            protocol_run_id,
                            "cancelled",
                            interrupt_response_schema("cancelled"),
                        )
                _cancel_background_run(
                    internal_run_id,
                    "AG-UI client disconnected before a terminal event",
                )
            if protocol_index and protocol_run_id and not lifecycle.terminal:
                with contextlib.suppress(Exception):
                    protocol_index.mark_status(protocol_run_id, "client_disconnected")
            return
        except ResumePreparationError as error:
            if worker_acquired:
                _worker_slots.release()
                worker_acquired = False
            status = {
                "not_found": HTTPStatus.NOT_FOUND,
                "conflict": HTTPStatus.CONFLICT,
            }.get(error.kind, HTTPStatus.BAD_REQUEST)
            if headers_sent:
                if internal_run_id:
                    _cancel_background_run(internal_run_id, str(error))
                if thread_id and protocol_run_id:
                    with contextlib.suppress(Exception):
                        if not lifecycle.started:
                            lifecycle.start(thread_id, protocol_run_id)
                        lifecycle.error(thread_id, protocol_run_id, str(error))
                return
            self._json({"error": str(error), **error.details}, status)
        except OpenInterruptQueryError as error:
            if headers_sent:
                if internal_run_id:
                    _cancel_background_run(internal_run_id, str(error))
                if thread_id and protocol_run_id:
                    with contextlib.suppress(Exception):
                        if not lifecycle.started:
                            lifecycle.start(thread_id, protocol_run_id)
                        lifecycle.error(thread_id, protocol_run_id, str(error))
                return
            self._json(
                {"error": str(error), "fail_closed": True},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
        except ProtocolIndexConflict as error:
            if worker_acquired:
                _worker_slots.release()
                worker_acquired = False
            if headers_sent and thread_id and protocol_run_id:
                with contextlib.suppress(Exception):
                    if not lifecycle.started:
                        lifecycle.start(thread_id, protocol_run_id)
                    lifecycle.error(thread_id, protocol_run_id, str(error))
                return
            self._json({"error": str(error)}, HTTPStatus.CONFLICT)
        except (ValueError, json.JSONDecodeError) as error:
            if headers_sent:
                if internal_run_id:
                    _cancel_background_run(internal_run_id, str(error))
                if thread_id and protocol_run_id:
                    with contextlib.suppress(Exception):
                        if not lifecycle.started:
                            lifecycle.start(thread_id, protocol_run_id)
                        lifecycle.error(thread_id, protocol_run_id, str(error))
                return
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            if headers_sent:
                if internal_run_id:
                    _cancel_background_run(internal_run_id, str(error))
                if protocol_index and protocol_run_id:
                    with contextlib.suppress(Exception):
                        protocol_index.mark_status(protocol_run_id, "failed")
                if thread_id and protocol_run_id:
                    with contextlib.suppress(Exception):
                        if not lifecycle.started:
                            lifecycle.start(thread_id, protocol_run_id)
                        lifecycle.error(thread_id, protocol_run_id, str(error))
                return
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
        finally:
            interrupt_index_stack.close()
            if (
                headers_sent
                and thread_id
                and protocol_run_id
                and not lifecycle.terminal
            ):
                with contextlib.suppress(Exception):
                    if not lifecycle.started:
                        lifecycle.start(thread_id, protocol_run_id)
                    lifecycle.error(
                        thread_id,
                        protocol_run_id,
                        "AG-UI handler exited without a terminal event",
                    )
            if worker_acquired:
                _worker_slots.release()
            if stream_acquired:
                _stream_slots.release()
            if internal_run_id and agui_stream_registered:
                _release_agui_stream(internal_run_id)

    def _resume_run(self, run_id: str) -> None:
        slot_acquired = False
        try:
            if not self._request_origin_allowed():
                self._json({"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
                return
            run_id = validate_run_id(run_id)
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0 or content_length > MAX_REQUEST_BODY:
                self._json({"error": "request body is too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            payload = json.loads(self.rfile.read(content_length) or b"{}")
            config = AppConfig.from_env()
            request_id = str(payload.get("resume_request_id", "")).strip()
            if not request_id:
                raise ResumePreparationError("resume_request_id is required")
            idempotency_key = f"manual:{run_id}:{request_id}"
            store = RunStore(config.runs_dir, run_id)
            existing_receipt = store.resume_receipt(idempotency_key)
            latest_state = store.latest()
            lease_audit = store.execution_lease_audit()
            with _jobs_lock:
                current = _jobs.get(run_id, {}).get("status")
            if (
                current
                in {
                    "starting",
                    "queued",
                    "running",
                    "perceiving",
                    "planning",
                    "drafting",
                    "cancelling",
                }
                and existing_receipt is None
                and lease_audit
                and lease_audit["active"]
            ):
                self._json({"error": "run is already active"}, HTTPStatus.CONFLICT)
                return
            if not _worker_slots.acquire(blocking=False):
                self._json({"error": "too many active research jobs"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            slot_acquired = True
            recovery_receipt = bool(
                existing_receipt
                and isinstance(existing_receipt.get("response"), dict)
                and existing_receipt["response"].get("mode")
                == "stale_worker_recovery"
            )
            if (
                latest_state is not None
                and latest_state.status in RECOVERABLE_RUN_STATES
                and (existing_receipt is None or recovery_receipt)
            ):
                ambiguous_operations = store.ambiguous_operations()
                # A stale process can leave a paid model request in an unknown
                # state.  A user-confirmed recovery is committed as a normal
                # resume receipt before the replacement worker is allowed to
                # retry it, so the UI never silently repeats a charge.
                if ambiguous_operations or recovery_receipt:
                    execution_lease = store.acquire_execution_lease(idempotency_key)
                    if execution_lease is None:
                        raise ResumePreparationError(
                            "run is already active or its execution lease is held",
                            kind="conflict",
                        )
                    try:
                        prepared = prepare_crash_recovery(
                            config,
                            run_id,
                            payload,
                            idempotency_key=idempotency_key,
                        )
                    except Exception:
                        store.release_execution_lease(
                            str(execution_lease["owner_token"]),
                            int(execution_lease["fence"]),
                        )
                        raise
                    try:
                        reserved = bool(
                            execution_lease
                            and _reserve_resume_worker(run_id, execution_lease)
                        )
                        should_start = bool(
                            execution_lease
                            and reserved
                            and _claim_resume_execution(
                                store,
                                prepared.idempotency_key,
                                execution_lease,
                            )
                        )
                    except Exception:
                        _clear_resume_worker_reservation(run_id, execution_lease)
                        store.release_execution_lease(
                            str(execution_lease["owner_token"]),
                            int(execution_lease["fence"]),
                        )
                        raise
                    if execution_lease and not should_start:
                        _clear_resume_worker_reservation(run_id, execution_lease)
                        store.release_execution_lease(
                            str(execution_lease["owner_token"]),
                            int(execution_lease["fence"]),
                        )
                    if should_start:
                        with _jobs_lock:
                            _jobs[run_id] = _job_for_lease("queued", execution_lease)
                            _cancel_events[run_id] = threading.Event()
                        worker_thread = threading.Thread(
                            target=_run_in_background,
                            args=(
                                run_id,
                                prepared.question,
                                prepared.offline,
                                prepared.budget_limits,
                                execution_lease,
                            ),
                            kwargs={"runs_dir": config.runs_dir},
                            daemon=True,
                        )
                        try:
                            worker_thread.start()
                        except Exception:
                            store.release_execution_lease(
                                str(execution_lease["owner_token"]),
                                int(execution_lease["fence"]),
                            )
                            _release_resume_execution_claim(
                                store,
                                prepared.idempotency_key,
                                execution_lease,
                            )
                            _clear_resume_worker_reservation(run_id, execution_lease)
                            raise
                        slot_acquired = False
                    else:
                        _worker_slots.release()
                        slot_acquired = False
                    response = dict(prepared.response)
                    response["worker_started"] = should_start
                    response["crash_recovered"] = True
                    self._json(response, HTTPStatus.ACCEPTED)
                    return
                disallowed = {
                    "additional_iterations",
                    "additional_search_calls",
                    "additional_pages",
                } & payload.keys()
                if disallowed:
                    raise ResumePreparationError(
                        "crash recovery cannot change the already approved budget",
                        kind="conflict",
                    )
                execution_lease = store.acquire_execution_lease(
                    f"crash-recovery:{request_id}"
                )
                reserved = bool(
                    execution_lease
                    and _reserve_resume_worker(run_id, execution_lease)
                )
                if not execution_lease or not reserved:
                    if execution_lease:
                        store.release_execution_lease(
                            str(execution_lease["owner_token"]),
                            int(execution_lease["fence"]),
                        )
                    raise ResumePreparationError(
                        "run is already active",
                        kind="conflict",
                    )
                with _jobs_lock:
                    _jobs[run_id] = _job_for_lease("queued", execution_lease)
                    _cancel_events[run_id] = threading.Event()
                worker_thread = threading.Thread(
                    target=_run_in_background,
                    args=(
                        run_id,
                        latest_state.question,
                        latest_state.methodology.get("model_provider")
                        == "MockModelProvider",
                        latest_state.budget_limits,
                        execution_lease,
                    ),
                    kwargs={"runs_dir": config.runs_dir},
                    daemon=True,
                )
                try:
                    worker_thread.start()
                except Exception:
                    store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                    _clear_resume_worker_reservation(run_id, execution_lease)
                    raise
                slot_acquired = False
                self._json(
                    {
                        "run_id": run_id,
                        "status": "queued",
                        "worker_started": True,
                        "crash_recovered": True,
                        "fence": int(execution_lease["fence"]),
                    },
                    HTTPStatus.ACCEPTED,
                )
                return
            execution_lease = store.acquire_execution_lease(idempotency_key)
            if execution_lease is None:
                raise ResumePreparationError(
                    "run is already active or its execution lease is held",
                    kind="conflict",
                )
            try:
                prepared = prepare_resume(
                    config,
                    run_id,
                    payload,
                    source="manual",
                    idempotency_key=idempotency_key,
                )
            except Exception:
                store.release_execution_lease(
                    str(execution_lease["owner_token"]),
                    int(execution_lease["fence"]),
                )
                raise
            try:
                reserved = bool(
                    execution_lease
                    and _reserve_resume_worker(run_id, execution_lease)
                )
                should_start = bool(
                    execution_lease
                    and reserved
                    and _claim_resume_execution(
                        store,
                        prepared.idempotency_key,
                        execution_lease,
                    )
                )
            except Exception:
                _clear_resume_worker_reservation(run_id, execution_lease)
                store.release_execution_lease(
                    str(execution_lease["owner_token"]),
                    int(execution_lease["fence"]),
                )
                raise
            if execution_lease and not should_start:
                _clear_resume_worker_reservation(run_id, execution_lease)
                store.release_execution_lease(
                    str(execution_lease["owner_token"]),
                    int(execution_lease["fence"]),
                )
            if should_start:
                with _jobs_lock:
                    _jobs[run_id] = _job_for_lease("queued", execution_lease)
                    _cancel_events[run_id] = threading.Event()
                worker_thread = threading.Thread(
                    target=_run_in_background,
                    args=(
                        run_id,
                        prepared.question,
                        prepared.offline,
                        prepared.budget_limits,
                        execution_lease,
                    ),
                    kwargs={"runs_dir": config.runs_dir},
                    daemon=True,
                )
                try:
                    worker_thread.start()
                except Exception:
                    store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                    _release_resume_execution_claim(
                        store,
                        prepared.idempotency_key,
                        execution_lease,
                    )
                    _clear_resume_worker_reservation(run_id, execution_lease)
                    raise
                slot_acquired = False
            else:
                _worker_slots.release()
                slot_acquired = False
            response = dict(prepared.response)
            response["worker_started"] = should_start
            self._json(response, HTTPStatus.ACCEPTED)
        except ResumePreparationError as error:
            if slot_acquired:
                _worker_slots.release()
            status = {
                "not_found": HTTPStatus.NOT_FOUND,
                "conflict": HTTPStatus.CONFLICT,
            }.get(error.kind, HTTPStatus.BAD_REQUEST)
            self._json({"error": str(error), **error.details}, status)
        except (ValueError, json.JSONDecodeError) as error:
            if slot_acquired:
                _worker_slots.release()
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            if slot_acquired:
                _worker_slots.release()
            self._json({"error": str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _request_origin_allowed(self) -> bool:
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        fetch_sites = self.headers.get_all("Sec-Fetch-Site", [])
        if len(hosts) != 1 or len(origins) > 1 or len(fetch_sites) > 1:
            return False
        return _request_metadata_allowed(
            hosts[0],
            origins[0] if origins else None,
            fetch_sites[0] if fetch_sites else None,
            int(self.server.server_port),
        )

    def do_DELETE(self) -> None:
        if not self._request_origin_allowed():
            self._json({"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
            return
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/runs/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        run_id = parsed.path.removeprefix("/api/runs/")
        with _jobs_lock:
            event = _cancel_events.get(run_id)
            job = _jobs.get(run_id)
            if event is None and job is None:
                self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
                return
            if event:
                event.set()
            _jobs[run_id] = {**(job or {}), "status": "cancelling", "error": ""}
        self._json({"run_id": run_id, "status": "cancelling"}, HTTPStatus.ACCEPTED)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _config(self) -> None:
        config = AppConfig.from_env()
        selected_model = config.model_id(config.model_choice, required=False)
        self._json(
            {
                "model_provider": config.model_provider,
                "search_provider": config.search_provider,
                "default_model": config.model_choice,
                "default_profile": config.model_profile,
                "model": selected_model,
                "models": config.model_options(),
                "profiles": config.profile_options(),
                "role_routes": config.profile_routes("team"),
                "multimodal": {
                    "supported_inputs": ["text", "document", "image", "audio"],
                    "accepted_input_kinds": ["text", "document", "image", "audio"],
                    "accepted_media_types": list(SUPPORTED_ATTACHMENT_MEDIA_TYPES),
                    "capability_source": "operator-declared-plus-exact-model-and-gateway-bound-probe-receipt",
                    "capability_binding": "model_id + SHA-256(normalized gateway base URL)",
                    "max_attachments": MAX_ATTACHMENT_COUNT,
                    "max_attachment_bytes": MAX_ATTACHMENT_BYTES,
                    "max_total_bytes": MAX_TOTAL_ATTACHMENT_BYTES,
                    "perception_route": config.profile_routes("team")["perception"],
                },
                "api_key_set": bool(config.resolved_model_api_key),
                "base_url_set": bool(config.resolved_model_base_url),
                "search_configured": config.search_provider_configured,
                "brave_api_key_set": bool(config.resolved_brave_api_key),
            }
        )

    def _methodology(self) -> None:
        self._json(methodology_contract())

    def _protocol_verification(self) -> None:
        if not PROTOCOL_VERIFICATION_PATH.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = PROTOCOL_VERIFICATION_PATH.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/markdown; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def _protocol_audit(self, run_id: str, query: str = "") -> None:
        try:
            run_id = validate_run_id(run_id)
            requested_limit, cursor = _parse_audit_query(query)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        config = AppConfig.from_env()
        store = _existing_run_store(config.runs_dir, run_id)
        if store is None:
            self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return
        events = _read_events(store.events_path)
        if store.latest() is None and not store.agui_interrupt_audit() and not events:
            self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            projection = _protocol_audit_projection(
                config,
                run_id,
                store,
                limit=requested_limit or DEFAULT_AUDIT_PAGE_LIMIT,
                cursor=cursor,
            )
        except AuditResponseTooLargeError as error:
            self._json(
                {"error": str(error), "code": "audit_response_too_large"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self._json(projection)

    def _run_state(self, run_id: str, query: str = "") -> None:
        try:
            run_id = validate_run_id(run_id)
            requested_limit, cursor = _parse_audit_query(query)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        config = AppConfig.from_env()
        with _jobs_lock:
            job = dict(_jobs.get(run_id, {}))
        store = _existing_run_store(config.runs_dir, run_id)
        state = store.latest() if store else None
        job = _durable_job_view(job, state)
        events = _read_events(config.runs_dir / run_id / "events.jsonl")
        usage = _live_usage_snapshot(store)
        if state is None and not job:
            self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return
        audit = None
        if store and state:
            try:
                audit = _run_audit_projection(
                    store,
                    limit=requested_limit or DEFAULT_AUDIT_PAGE_LIMIT,
                    cursor=cursor,
                )
            except AuditResponseTooLargeError as error:
                self._json(
                    {"error": str(error), "code": "audit_response_too_large"},
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                )
                return
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
        self._json(
            {
                "job": _public_job_view(job),
                "state": _public_state_dict(state) if state else None,
                "events": events,
                "event_window": (
                    _event_window_projection(store, events) if store else None
                ),
                "audit": audit,
                # Keep the live budget surface available even if an optional
                # audit page has to be reduced or deferred for size reasons.
                "usage": usage,
            }
        )

    def _run_usage(self, run_id: str) -> None:
        """Serve the small live-cost surface without constructing an audit page.

        A large research record can make its bounded audit projection take
        longer than a model response.  This endpoint keeps the visible budget
        current while the richer trace continues to load independently.
        """

        try:
            run_id = validate_run_id(run_id)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        config = AppConfig.from_env()
        with _jobs_lock:
            job = dict(_jobs.get(run_id, {}))
        store = _existing_run_store(config.runs_dir, run_id)
        state = store.latest() if store else None
        job = _durable_job_view(job, state)
        if state is None and not job:
            self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return
        status = str(job.get("status") or (state.status if state else "queued"))
        self._json(
            {
                "run_id": run_id,
                "status": status,
                "usage": _live_usage_snapshot(store),
            }
        )

    def _source_snapshot(
        self, run_id: str, source_id: str, query: str = ""
    ) -> None:
        try:
            run_id = validate_run_id(run_id)
            store = _existing_run_store(AppConfig.from_env().runs_dir, run_id)
            fetch_record_id = parse_qs(query).get("fetch_record_id", [None])[0]
            snapshot = (
                store.read_source_snapshot(
                    source_id,
                    fetch_record_id=fetch_record_id,
                )
                if store
                else None
            )
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if snapshot is None:
            self._json({"error": "source snapshot not found"}, HTTPStatus.NOT_FOUND)
            return
        self._json(snapshot)

    def _artifact_snapshot(self, run_id: str, artifact_id: str) -> None:
        try:
            run_id = validate_run_id(run_id)
            store = _existing_run_store(AppConfig.from_env().runs_dir, run_id)
            artifact = store.read_artifact(artifact_id) if store else None
        except (ValueError, RuntimeError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if artifact is None:
            self._json({"error": "artifact not found"}, HTTPStatus.NOT_FOUND)
            return
        self._json(artifact)

    def _input_attachment(self, run_id: str, attachment_id: str) -> None:
        try:
            config = AppConfig.from_env()
            store = _existing_run_store(config.runs_dir, validate_run_id(run_id))
            if store is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            attachment, data = store.read_input_attachment(attachment_id)
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        except (ValueError, ArtifactIntegrityError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", attachment.media_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", "inline")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.end_headers()
        self.wfile.write(data)

    def _run_list(self) -> None:
        config = AppConfig.from_env()
        entries: list[dict[str, object]] = []
        if config.runs_dir.exists():
            paths = sorted(
                (path for path in config.runs_dir.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
            for path in paths[:20]:
                try:
                    store = _existing_run_store(config.runs_dir, path.name)
                except ValueError:
                    continue
                if store is None:
                    continue
                state = store.latest()
                if state is None:
                    continue
                entries.append(
                    {
                        "run_id": state.run_id,
                        "question": state.question,
                        "status": state.status,
                        "closure_score": state.closure.score if state.closure else None,
                        "closure_score_status": (
                            "observed" if state.closure else "unavailable"
                        ),
                        "updated_at": path.stat().st_mtime,
                    }
                )
        self._json({"runs": entries})

    def _event_stream(self, run_id: str, query: str = "") -> None:
        if not self._request_origin_allowed():
            self._json({"error": "request origin is not allowed"}, HTTPStatus.FORBIDDEN)
            return
        try:
            run_id = validate_run_id(run_id)
            requested_limit, cursor = _parse_audit_query(query)
        except ValueError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        config = AppConfig.from_env()
        store = _existing_run_store(config.runs_dir, run_id)
        with _jobs_lock:
            known_job = bool(_jobs.get(run_id))
        if store is None and not known_job:
            self._json({"error": "run not found"}, HTTPStatus.NOT_FOUND)
            return
        if not _stream_slots.acquire(blocking=False):
            self._json(
                {"error": "too many active event streams"},
                HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            last_fingerprint = ""
            last_usage_fingerprint = ""
            deadline = time.monotonic() + 25
            self.wfile.write(b"retry: 1200\n\n")
            self.wfile.flush()
            while time.monotonic() < deadline:
                with _jobs_lock:
                    raw_job = dict(_jobs.get(run_id, {}))
                state = store.latest() if store else None
                job = _durable_job_view(raw_job, state)
                status = job.get("status") or (state.status if state else "queued")
                usage = _live_usage_snapshot(store)

                # Send a returned model receipt before building the larger
                # audit projection.  The latter may read several ledgers and
                # can otherwise hide a fresh cost update behind a slow cycle.
                usage_fingerprint = (
                    _usage_snapshot_fingerprint(usage)
                    if usage is not None
                    else "none"
                )
                if usage_fingerprint != last_usage_fingerprint:
                    self._write_sse(
                        "usage",
                        {
                            "protocol": "ag-ui-shaped-v0",
                            "type": "USAGE_SNAPSHOT",
                            "run_id": run_id,
                            "status": status,
                            "usage": usage,
                        },
                    )
                    last_usage_fingerprint = usage_fingerprint

                events = _read_events(config.runs_dir / run_id / "events.jsonl")
                audit = None
                if store and state:
                    try:
                        audit = _run_audit_projection(
                            store,
                            limit=requested_limit or DEFAULT_AUDIT_PAGE_LIMIT,
                            cursor=cursor,
                        )
                    except AuditResponseTooLargeError as error:
                        audit = {
                            "error": str(error),
                            "code": "audit_response_too_large",
                        }
                event_window = (
                    _event_window_projection(store, events) if store else None
                )
                audit_fingerprint = hashlib.sha256(
                    json.dumps(
                        audit,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest() if audit is not None else "none"
                fingerprint = (
                    f"{status}|{job.get('error', '')}|"
                    f"{event_window.get('returned_count') if event_window else 0}|"
                    f"{event_window.get('total_count') if event_window else 0}|"
                    f"{audit_fingerprint}"
                )
                if fingerprint != last_fingerprint:
                    payload = {
                        "protocol": "ag-ui-shaped-v0",
                        "type": "STATE_SNAPSHOT",
                        "job": _public_job_view(job),
                        "state": _public_state_dict(state) if state else None,
                        "events": events,
                        "event_window": event_window,
                        "audit": audit,
                        "usage": usage,
                    }
                    self._write_sse("snapshot", payload)
                    last_fingerprint = fingerprint
                if status in {"completed", "verification_failed", "evidence_incomplete", "failed", "cancelled"}:
                    self._write_sse(
                        "done",
                        {
                            "protocol": "ag-ui-shaped-v0",
                            "type": "RUN_ERROR" if status == "failed" else "RUN_FINISHED",
                            "run_id": run_id,
                            "status": status,
                        },
                    )
                    return
                if not raw_job and (
                    state is None or state.status in RECOVERABLE_RUN_STATES
                ):
                    self._write_sse(
                        "done",
                        {
                            "protocol": "ag-ui-shaped-v0",
                            "type": "RUN_ERROR",
                            "run_id": run_id,
                            "status": "worker_unavailable",
                        },
                    )
                    return
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(0.4)
            self._write_sse(
                "rollover",
                {
                    "protocol": "ag-ui-shaped-v0",
                    "type": "STREAM_ROLLOVER",
                    "run_id": run_id,
                    "retry_after_ms": 500,
                },
            )
        except OSError:
            return
        finally:
            _stream_slots.release()

    def _write_sse(self, event: str, payload: object) -> None:
        data = json.dumps(
            _redact_owner_tokens(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode())
        self.wfile.flush()

    def _write_agui_data(self, payload: object) -> None:
        data = json.dumps(
            _redact_owner_tokens(payload),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
        path = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in path.parents and path != WEB_ROOT.resolve():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(content)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        content = json.dumps(
            _redact_owner_tokens(value),
            ensure_ascii=False,
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The built-in web server may only bind to a loopback address")
    server = ThreadingHTTPServer((host, port), ResearchRequestHandler)
    print(f"Deep Research UI: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _run_in_background(
    run_id: str,
    question: str,
    offline: bool,
    budget_limits: dict[str, int] | None = None,
    execution_lease: dict[str, object] | None = None,
    *,
    runs_dir: Path | None = None,
    model_profile: str | None = None,
    model_choice: str | None = None,
) -> None:
    if execution_lease is None:
        raise RuntimeError("background workers require an execution lease")
    with _jobs_lock:
        if not _worker_owns_job(run_id, execution_lease):
            _worker_slots.release()
            return
        _jobs[run_id] = _job_for_lease("running", execution_lease)
    lease_stop = threading.Event()
    lease_lost = threading.Event()
    lease_thread: threading.Thread | None = None
    lease_store: RunStore | None = None
    resume_receipt_id: str | None = None
    config: AppConfig | None = None
    configured_runs_dir = Path(runs_dir) if runs_dir is not None else None
    try:
        if configured_runs_dir is not None:
            lease_store = RunStore(configured_runs_dir, run_id)
        config = AppConfig.from_env()
        if configured_runs_dir is not None:
            config.runs_dir = configured_runs_dir
        if lease_store is None:
            lease_store = RunStore(config.runs_dir, run_id)
        persisted_model_profile = _persisted_model_profile(lease_store)
        selected_model_profile = config.select_profile(
            model_profile
            or model_choice
            or persisted_model_profile
            or config.model_profile
        )
        if execution_lease:
            lease_store = RunStore(config.runs_dir, run_id)
            owner_token = str(execution_lease["owner_token"])
            fence = int(execution_lease["fence"])
            lease_store.bind_execution_fence(owner_token, fence)
            candidate_receipt_id = str(execution_lease.get("receipt_id", ""))
            if candidate_receipt_id and lease_store.resume_receipt(candidate_receipt_id):
                resume_receipt_id = candidate_receipt_id

            def heartbeat() -> None:
                while not lease_stop.wait(5):
                    try:
                        renewed = lease_store.heartbeat_execution_lease(
                            owner_token,
                            fence,
                        )
                    except Exception as error:
                        lease_lost.set()
                        with _jobs_lock:
                            event = _cancel_events.get(run_id)
                            if event and _worker_owns_job(run_id, execution_lease):
                                event.set()
                                current = _jobs.get(run_id, {})
                                _jobs[run_id] = {
                                    **current,
                                    "status": "lease_lost",
                                    "error": (
                                        "execution lease heartbeat failed: "
                                        f"{type(error).__name__}"
                                    ),
                                }
                        return
                    if not renewed:
                        lease_lost.set()
                        with _jobs_lock:
                            event = _cancel_events.get(run_id)
                            if event and _worker_owns_job(run_id, execution_lease):
                                event.set()
                                current = _jobs.get(run_id, {})
                                _jobs[run_id] = {
                                    **current,
                                    "status": "lease_lost",
                                    "error": "execution lease heartbeat was lost",
                                }
                        return

            lease_thread = threading.Thread(
                target=heartbeat,
                name=f"lease-heartbeat-{run_id}",
                daemon=True,
            )
            lease_thread.start()
        if budget_limits:
            config.budget.max_iterations = budget_limits.get("iterations", config.budget.max_iterations)
            config.budget.max_search_calls = budget_limits.get("search_calls", config.budget.max_search_calls)
            config.budget.max_pages = budget_limits.get("pages", config.budget.max_pages)
        with _jobs_lock:
            cancel_event = _cancel_events.setdefault(run_id, threading.Event())
        if offline:
            model = MockModelProvider()
            search = ReplaySearchProvider(config.replay_corpus)
        elif selected_model_profile == "team":
            model, search = build_model_team(config, selected_model_profile)
        else:
            model, search = build_providers(config, selected_model_profile)
        engine = ResearchEngine(
            config,
            model,
            search,
            cancel_check=lambda: cancel_event.is_set() or lease_lost.is_set(),
            execution_lease=execution_lease,
            lease_lost_check=lease_lost.is_set,
        )
        state = asyncio.run(engine.run(question, run_id))
        if resume_receipt_id and lease_store:
            if state.status not in TERMINAL_RUN_STATES:
                raise RuntimeError(
                    "worker returned without a durable terminal run status"
                )
            if not lease_store.finish_resume_execution(
                resume_receipt_id,
                owner_token=str(execution_lease["owner_token"]),
                fence=int(execution_lease["fence"]),
                status="completed",
                durable_run_status=state.status,
            ):
                raise RuntimeError("resume receipt could not be terminally fenced")
        with _jobs_lock:
            if _worker_owns_job(run_id, execution_lease):
                _jobs[run_id] = _job_for_lease(
                    state.status, execution_lease
                )
    except Exception as error:
        lease_lost_error = lease_lost.is_set() or isinstance(
            error, ExecutionFenceLostError
        )
        audit_written = False
        if resume_receipt_id and lease_store:
            durable_status = None
            try:
                latest_state = lease_store.latest()
            except Exception:
                latest_state = None
            if latest_state is not None and latest_state.status in TERMINAL_RUN_STATES:
                lease_store.finish_resume_execution(
                    resume_receipt_id,
                    owner_token=str(execution_lease["owner_token"]),
                    fence=int(execution_lease["fence"]),
                    status="failed",
                    durable_run_status=latest_state.status,
                    error=str(error),
                )
            else:
                lease_store.release_resume_execution_claim(
                    resume_receipt_id,
                    owner_token=str(execution_lease["owner_token"]),
                    fence=int(execution_lease["fence"]),
                    error=f"worker startup failed: {error}",
                )
        audit_runs_dir = (
            config.runs_dir if config is not None else configured_runs_dir
        )
        if audit_runs_dir is not None:
            audit_written = _record_worker_audit(
                audit_runs_dir,
                run_id,
                execution_lease,
                "worker_lease_lost" if lease_lost_error else "worker_exception",
                {
                    "exception_type": type(error).__name__,
                    "error": str(error)[:2000],
                    "external_operation_state": (
                        "unknown_or_cancelled"
                        if lease_lost_error
                        else "not_applicable"
                    ),
                },
            )
        with _jobs_lock:
            if _worker_owns_job(run_id, execution_lease):
                failed_job = _job_for_lease(
                    "lease_lost" if lease_lost_error else "failed",
                    execution_lease,
                    str(error),
                )
                failed_job["worker_audit_written"] = audit_written
                _jobs[run_id] = failed_job
    finally:
        try:
            lease_stop.set()
            if lease_thread:
                lease_thread.join(timeout=1)
            needs_exit_audit = False
            with _jobs_lock:
                current = _jobs.get(run_id, {})
                needs_exit_audit = (
                    _worker_owns_job(run_id, execution_lease)
                    and current.get("status") in ACTIVE_JOB_STATES
                )
            if needs_exit_audit and config is not None:
                audit_written = _record_worker_audit(
                    config.runs_dir,
                    run_id,
                    execution_lease,
                    "worker_exit_without_terminal",
                    {"error": "worker exited without a terminal state"},
                )
                with _jobs_lock:
                    if _worker_owns_job(run_id, execution_lease):
                        failed_job = _job_for_lease(
                            "failed",
                            execution_lease,
                            "worker exited without a terminal state",
                        )
                        failed_job["worker_audit_written"] = audit_written
                        _jobs[run_id] = failed_job
            if lease_store and execution_lease:
                lease_store.release_execution_lease(
                    str(execution_lease["owner_token"]),
                    int(execution_lease["fence"]),
                )
        finally:
            _worker_slots.release()


def _read_events(path: Path, limit: int = 100) -> list[dict[str, object]]:
    if limit <= 0 or not path.is_file():
        return []
    bounded_limit = min(limit, 100)
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - MAX_EVENT_TAIL_BYTES)
            handle.seek(start)
            content = handle.read(MAX_EVENT_TAIL_BYTES)
    except OSError:
        return []
    if start:
        first_complete_line = content.find(b"\n")
        if first_complete_line < 0:
            return []
        content = content[first_complete_line + 1 :]
    complete_end = content.rfind(b"\n") + 1
    lines = content[:complete_end].splitlines()[-bounded_limit:]
    events: list[dict[str, object]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            events.append(value)
    return events
