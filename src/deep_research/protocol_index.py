from __future__ import annotations

import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ProtocolIndexConflict(ValueError):
    pass


PROTOCOL_RUN_STATUSES = frozenset(
    {
        "registered",
        "queued",
        "running",
        "client_disconnected",
        "worker_unavailable",
        "interrupt_cancelled",
        "completed",
        "verification_failed",
        "evidence_incomplete",
        "cancelled",
        "failed",
        "ambiguous_operation",
        "fetch_error",
        "runtime_error",
    }
)

_TERMINAL_PROTOCOL_STATUSES = frozenset(
    {
        "interrupt_cancelled",
        "completed",
        "verification_failed",
        "evidence_incomplete",
        "cancelled",
        "failed",
        "ambiguous_operation",
        "fetch_error",
        "runtime_error",
    }
)

# Recoverable adapter states may be re-admitted after a disconnect or a
# missing worker. Terminal states are self-loop-only, so an old receipt cannot
# silently move a completed run back into execution.
_RECOVERABLE_PROTOCOL_STATUSES = frozenset(
    {"registered", "queued", "running", "client_disconnected", "worker_unavailable"}
)
PROTOCOL_STATUS_TRANSITIONS = {
    "registered": frozenset(
        {
            "registered",
            "queued",
            "running",
            "client_disconnected",
            "worker_unavailable",
            *_TERMINAL_PROTOCOL_STATUSES,
        }
    ),
    "queued": frozenset(
        {
            "queued",
            "running",
            "client_disconnected",
            "worker_unavailable",
            *_TERMINAL_PROTOCOL_STATUSES,
        }
    ),
    "running": frozenset(
        {
            "running",
            "client_disconnected",
            "worker_unavailable",
            *_TERMINAL_PROTOCOL_STATUSES,
        }
    ),
    "client_disconnected": frozenset(
        {
            "client_disconnected",
            "queued",
            "worker_unavailable",
            *_TERMINAL_PROTOCOL_STATUSES,
        }
    ),
    "worker_unavailable": frozenset(
        {
            "worker_unavailable",
            "queued",
            "client_disconnected",
            *_TERMINAL_PROTOCOL_STATUSES,
        }
    ),
    **{
        status: frozenset({status})
        for status in _TERMINAL_PROTOCOL_STATUSES
    },
}


class AgUiProtocolIndex:
    """Thread-level AG-UI control-plane index shared by all durable runs."""

    def __init__(self, runs_dir: Path) -> None:
        root = runs_dir.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.database_path = root / "agui_protocol.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    thread_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS external_runs (
                    run_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    durable_run_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('producer', 'resume')),
                    declared_parent_run_id TEXT,
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(thread_id) REFERENCES threads(thread_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS external_runs_thread_idx
                ON external_runs(thread_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS status_transitions (
                    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES external_runs(run_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS status_transitions_run_idx
                ON status_transitions(run_id, transition_id)
                """
            )
            # Older databases predate the ledger. Backfill one creation record
            # per run without changing the run's current status.
            connection.execute(
                """
                INSERT INTO status_transitions(
                    run_id, from_status, to_status, changed_at
                )
                SELECT runs.run_id, NULL, runs.status, runs.created_at
                FROM external_runs AS runs
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM status_transitions AS transitions
                    WHERE transitions.run_id = runs.run_id
                )
                """
            )
            connection.commit()
        os.chmod(self.database_path, 0o600)

    def register_run(
        self,
        *,
        thread_id: str,
        run_id: str,
        durable_run_id: str,
        kind: str,
        parent_run_id: str | None,
        request_hash: str,
    ) -> dict[str, Any]:
        _validate_protocol_id("threadId", thread_id)
        _validate_protocol_id("runId", run_id)
        _validate_protocol_id("durableRunId", durable_run_id)
        if kind not in {"producer", "resume"}:
            raise ValueError("invalid AG-UI external run kind")
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT thread_id, durable_run_id, kind,
                       declared_parent_run_id, request_hash, status
                FROM external_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if existing:
                same_request = (
                    str(existing[0]) == thread_id
                    and str(existing[2]) == kind
                    and (existing[3] or None) == parent_run_id
                    and str(existing[4]) == request_hash
                )
                connection.commit()
                if not same_request:
                    raise ProtocolIndexConflict(
                        "AG-UI runId is globally registered to a different request"
                    )
                return {
                    "status": "replay",
                    "durable_run_id": str(existing[1]),
                    "run_status": str(existing[5]),
                }
            parent = None
            if parent_run_id:
                parent = connection.execute(
                    "SELECT thread_id FROM external_runs WHERE run_id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent and str(parent[0]) != thread_id:
                    connection.rollback()
                    raise ProtocolIndexConflict(
                        "parentRunId belongs to a different AG-UI thread"
                    )
            connection.execute(
                """
                INSERT INTO threads(thread_id, revision, created_at, updated_at)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (thread_id, now, now),
            )
            connection.execute(
                """
                INSERT INTO external_runs(
                    run_id, thread_id, durable_run_id, kind,
                    declared_parent_run_id, request_hash, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?)
                """,
                (
                    run_id,
                    thread_id,
                    durable_run_id,
                    kind,
                    parent_run_id,
                    request_hash,
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO status_transitions(
                    run_id, from_status, to_status, changed_at
                ) VALUES (?, NULL, 'registered', ?)
                """,
                (run_id, now),
            )
            connection.commit()
        return {
            "status": "registered",
            "durable_run_id": durable_run_id,
            "run_status": "registered",
            "parent_known": bool(parent),
        }

    def mark_status(self, run_id: str, status: str) -> None:
        if status not in PROTOCOL_RUN_STATUSES:
            raise ValueError(f"invalid AG-UI protocol run status: {status}")
        now = datetime.now(UTC).isoformat()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT status FROM external_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise ProtocolIndexConflict("AG-UI runId is not registered")
            current_status = str(current[0])
            allowed = PROTOCOL_STATUS_TRANSITIONS.get(current_status)
            if allowed is None or status not in allowed:
                connection.rollback()
                raise ProtocolIndexConflict(
                    f"invalid AG-UI status transition: {current_status} -> {status}"
                )
            cursor = connection.execute(
                """
                UPDATE external_runs SET status = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, now, run_id),
            )
            if status != current_status:
                connection.execute(
                    """
                    INSERT INTO status_transitions(
                        run_id, from_status, to_status, changed_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (run_id, current_status, status, now),
                )
            connection.commit()
        if cursor.rowcount != 1:
            raise ProtocolIndexConflict("AG-UI runId is not registered")

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT run_id, thread_id, durable_run_id, kind,
                       declared_parent_run_id, request_hash, status,
                       created_at, updated_at
                FROM external_runs WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
        if not row:
            return None
        keys = (
            "run_id",
            "thread_id",
            "durable_run_id",
            "kind",
            "declared_parent_run_id",
            "request_hash",
            "status",
            "created_at",
            "updated_at",
        )
        return dict(zip(keys, row, strict=True))

    def runs_for_durable(self, durable_run_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT run_id, thread_id, durable_run_id, kind,
                       declared_parent_run_id, request_hash, status,
                       created_at, updated_at
                FROM external_runs
                WHERE durable_run_id = ?
                ORDER BY created_at, run_id
                """,
                (durable_run_id,),
            ).fetchall()
        keys = (
            "run_id",
            "thread_id",
            "durable_run_id",
            "kind",
            "declared_parent_run_id",
            "request_hash",
            "status",
            "created_at",
            "updated_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def status_transitions_for_durable(
        self,
        durable_run_id: str,
    ) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT transitions.transition_id, transitions.run_id,
                       transitions.from_status, transitions.to_status,
                       transitions.changed_at
                FROM status_transitions AS transitions
                JOIN external_runs AS runs ON runs.run_id = transitions.run_id
                WHERE runs.durable_run_id = ?
                ORDER BY transitions.transition_id
                """,
                (durable_run_id,),
            ).fetchall()
        keys = (
            "transition_id",
            "run_id",
            "from_status",
            "to_status",
            "changed_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]


def _validate_protocol_id(field: str, value: str) -> None:
    if not re.fullmatch(r"[^\x00-\x1f\x7f]{1,240}", value):
        raise ValueError(f"AG-UI {field} must be 1-240 visible characters")
