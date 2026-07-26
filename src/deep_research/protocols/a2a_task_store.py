"""Durable A2A TaskStore backed by SQLite."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task
from a2a.utils.constants import (
    DEFAULT_LIST_TASKS_PAGE_SIZE,
    MAX_LIST_TASKS_PAGE_SIZE,
)
from a2a.utils.errors import InvalidParamsError
from a2a.utils.task import decode_page_token


# Kept as a compatibility name for callers that imported the old constant.
# Listing itself is cursor/SQL bounded and never uses an arbitrary scan cap.
MAX_A2A_TASK_LIST_SCAN = 1_000
_TERMINAL_TASK_STATES = {
    a2a_pb2.TASK_STATE_COMPLETED,
    a2a_pb2.TASK_STATE_CANCELED,
    a2a_pb2.TASK_STATE_FAILED,
    a2a_pb2.TASK_STATE_REJECTED,
}

_CURSOR_VERSION = 1


def _task_projection(task: Task) -> tuple[str, int, int | None]:
    timestamp = (
        task.status.timestamp.ToMicroseconds()
        if task.HasField("status") and task.status.HasField("timestamp")
        else None
    )
    return task.context_id, int(task.status.state), timestamp


def _filter_fingerprint(
    context_id: str,
    status: int,
    status_timestamp_after: int | None,
) -> str:
    payload = json.dumps(
        [context_id, status or None, status_timestamp_after],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _encode_cursor(
    *,
    task_id: str,
    status_timestamp: int | None,
    filters: str,
) -> str:
    payload = {
        "v": _CURSOR_VERSION,
        "task_id": task_id,
        "has_timestamp": status_timestamp is not None,
        "status_timestamp": status_timestamp,
        "filters": filters,
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(page_token: str) -> dict[str, object]:
    """Decode the current cursor and accept the SDK's legacy task-id token."""
    try:
        encoded = page_token.encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (UnicodeEncodeError, UnicodeDecodeError, ValueError, binascii.Error):
        payload = None
    if isinstance(payload, dict):
        if payload.get("v") != _CURSOR_VERSION:
            raise InvalidParamsError("Invalid page token version")
        task_id = payload.get("task_id")
        filters = payload.get("filters")
        has_timestamp = payload.get("has_timestamp")
        timestamp = payload.get("status_timestamp")
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(filters, str)
            or not isinstance(has_timestamp, bool)
            or (timestamp is not None and not isinstance(timestamp, int))
            or has_timestamp != (timestamp is not None)
        ):
            raise InvalidParamsError("Invalid page token payload")
        return {
            "task_id": task_id,
            "filters": filters,
            "has_timestamp": has_timestamp,
            "status_timestamp": timestamp,
        }
    try:
        task_id = decode_page_token(page_token)
    except InvalidParamsError:
        raise
    if not task_id:
        raise InvalidParamsError("Invalid page token: empty task ID")
    return {
        "task_id": task_id,
        "filters": None,
        "has_timestamp": None,
        "status_timestamp": None,
    }


class SQLiteTaskStore(TaskStore):
    """Persist owner-scoped A2A protobuf Tasks across gateway restarts."""

    def __init__(
        self,
        path: Path | str,
        owner_resolver: OwnerResolver = resolve_user_scope,
    ) -> None:
        self.path = Path(path)
        self.owner_resolver = owner_resolver
        self._lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS a2a_tasks (
                    owner TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    task_blob BLOB NOT NULL,
                    context_id TEXT NOT NULL DEFAULT '',
                    status_state INTEGER NOT NULL DEFAULT 0,
                    status_timestamp INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (owner, task_id)
                )
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(a2a_tasks)")
            }
            needs_backfill = False
            if "context_id" not in columns:
                connection.execute(
                    "ALTER TABLE a2a_tasks ADD COLUMN context_id TEXT NOT NULL DEFAULT ''"
                )
                needs_backfill = True
            if "status_state" not in columns:
                connection.execute(
                    "ALTER TABLE a2a_tasks ADD COLUMN status_state INTEGER NOT NULL DEFAULT 0"
                )
                needs_backfill = True
            if "status_timestamp" not in columns:
                connection.execute(
                    "ALTER TABLE a2a_tasks ADD COLUMN status_timestamp INTEGER"
                )
                needs_backfill = True
            if needs_backfill:
                legacy_rows = connection.execute(
                    "SELECT owner, task_id, task_blob FROM a2a_tasks"
                ).fetchall()
                for owner, task_id, payload in legacy_rows:
                    task = Task.FromString(payload)
                    context_id, status_state, status_timestamp = _task_projection(task)
                    connection.execute(
                        """
                        UPDATE a2a_tasks
                        SET context_id = ?, status_state = ?, status_timestamp = ?
                        WHERE owner = ? AND task_id = ?
                        """,
                        (
                            context_id,
                            status_state,
                            status_timestamp,
                            owner,
                            task_id,
                        ),
                    )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_a2a_tasks_owner_filters
                ON a2a_tasks(
                    owner, context_id, status_state, status_timestamp, task_id
                )
                """
            )
        os.chmod(self.path, 0o600)

    async def save(self, task: Task, context: ServerCallContext) -> None:
        owner = self.owner_resolver(context)
        payload = task.SerializeToString()
        context_id, status_state, status_timestamp = _task_projection(task)
        async with self._lock:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                existing_row = connection.execute(
                    "SELECT task_blob FROM a2a_tasks WHERE owner = ? AND task_id = ?",
                    (owner, task.id),
                ).fetchone()
                if existing_row:
                    existing = Task.FromString(existing_row[0])
                    if existing.status.state in _TERMINAL_TASK_STATES:
                        task.CopyFrom(existing)
                        return
                connection.execute(
                    """
                    INSERT INTO a2a_tasks(
                        owner, task_id, task_blob, context_id,
                        status_state, status_timestamp, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(owner, task_id) DO UPDATE SET
                        task_blob = excluded.task_blob,
                        context_id = excluded.context_id,
                        status_state = excluded.status_state,
                        status_timestamp = excluded.status_timestamp,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        owner,
                        task.id,
                        payload,
                        context_id,
                        status_state,
                        status_timestamp,
                    ),
                )

    async def get(
        self,
        task_id: str,
        context: ServerCallContext,
    ) -> Task | None:
        owner = self.owner_resolver(context)
        async with self._lock:
            with closing(self._connect()) as connection, connection:
                row = connection.execute(
                    "SELECT task_blob FROM a2a_tasks WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                ).fetchone()
        return Task.FromString(row[0]) if row else None

    async def list(
        self,
        params: a2a_pb2.ListTasksRequest,
        context: ServerCallContext,
    ) -> a2a_pb2.ListTasksResponse:
        owner = self.owner_resolver(context)
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        if page_size < 1 or page_size > MAX_LIST_TASKS_PAGE_SIZE:
            raise InvalidParamsError(
                f"page_size must be between 1 and {MAX_LIST_TASKS_PAGE_SIZE}"
            )
        status_timestamp_after = (
            params.status_timestamp_after.ToMicroseconds()
            if params.HasField("status_timestamp_after")
            else None
        )
        filters = _filter_fingerprint(
            params.context_id,
            int(params.status),
            status_timestamp_after,
        )
        where = ["owner = ?"]
        arguments: list[object] = [owner]
        if params.context_id:
            where.append("context_id = ?")
            arguments.append(params.context_id)
        if params.status:
            where.append("status_state = ?")
            arguments.append(int(params.status))
        if status_timestamp_after is not None:
            where.append("status_timestamp >= ?")
            arguments.append(status_timestamp_after)
        base_where = list(where)
        base_arguments = list(arguments)

        cursor = None
        legacy_cursor = False
        if params.page_token:
            cursor = _decode_cursor(params.page_token)
            cursor_filters = cursor["filters"]
            if cursor_filters is not None and cursor_filters != filters:
                raise InvalidParamsError(
                    "page token does not belong to the requested task filters"
                )
            cursor_task_id = str(cursor["task_id"])
            if cursor_filters is None:
                legacy_cursor = True
                # Tokens generated by a previous implementation contained only
                # a task ID. Resolve its sort key under the current owner and
                # filters before applying the SQL keyset predicate.
                legacy_where = [*where, "task_id = ?"]
                legacy_arguments = [*arguments, cursor_task_id]
                with closing(self._connect()) as connection, connection:
                    legacy_row = connection.execute(
                        """
                        SELECT status_timestamp
                        FROM a2a_tasks
                        WHERE """ + " AND ".join(legacy_where),
                        legacy_arguments,
                    ).fetchone()
                if legacy_row is None:
                    raise InvalidParamsError(
                        f"Invalid page token: {params.page_token}"
                    )
                cursor["status_timestamp"] = legacy_row[0]
                cursor["has_timestamp"] = legacy_row[0] is not None
            if not isinstance(cursor["has_timestamp"], bool):
                raise InvalidParamsError("Invalid page token cursor state")
            if cursor["has_timestamp"]:
                cursor_timestamp = cursor["status_timestamp"]
                if not isinstance(cursor_timestamp, int):
                    raise InvalidParamsError("Invalid page token timestamp")
                same_timestamp_operator = "<=" if legacy_cursor else "<"
                where.append(
                    "((status_timestamp IS NOT NULL AND status_timestamp < ?) "
                    f"OR (status_timestamp = ? AND task_id {same_timestamp_operator} ?) "
                    "OR status_timestamp IS NULL)"
                )
                arguments.extend(
                    [cursor_timestamp, cursor_timestamp, cursor_task_id]
                )
            else:
                operator = "<=" if legacy_cursor else "<"
                where.append(f"status_timestamp IS NULL AND task_id {operator} ?")
                arguments.append(cursor_task_id)

        where_sql = " AND ".join(where)
        async with self._lock:
            with closing(self._connect()) as connection, connection:
                total_size = connection.execute(
                    "SELECT COUNT(*) FROM a2a_tasks WHERE "
                    + " AND ".join(base_where),
                    base_arguments,
                ).fetchone()[0]
                rows = connection.execute(
                    """
                    SELECT task_id, status_timestamp, task_blob
                    FROM a2a_tasks
                    WHERE """ + where_sql + """
                    ORDER BY
                        (status_timestamp IS NOT NULL) DESC,
                        status_timestamp DESC,
                        task_id DESC
                    LIMIT ?
                    """,
                    [*arguments, page_size + 1],
                ).fetchall()
        page = [Task.FromString(row[2]) for row in rows[:page_size]]
        next_page_token = None
        if len(rows) > page_size:
            last_task_id, last_timestamp, _ = rows[page_size - 1]
            next_page_token = _encode_cursor(
                task_id=last_task_id,
                status_timestamp=last_timestamp,
                filters=filters,
            )
        return a2a_pb2.ListTasksResponse(
            next_page_token=next_page_token,
            tasks=page,
            total_size=total_size,
            page_size=page_size,
        )

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        owner = self.owner_resolver(context)
        async with self._lock:
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "DELETE FROM a2a_tasks WHERE owner = ? AND task_id = ?",
                    (owner, task_id),
                )
