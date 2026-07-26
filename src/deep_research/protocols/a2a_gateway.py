"""Limited A2A 1.0 JSON-RPC 2.0 boundary for one ResearchEngine Agent.

The six internal roles remain private.  A2A exposes one remote Agent whose
Task is mapped to one durable deep-research run. Streaming, push delivery, and
same-Task input-required continuation are intentionally not implemented.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlsplit

from starlette.authentication import SimpleUser
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import TaskUpdater
from a2a.server.tasks.task_store import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    Part,
    SecurityScheme,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.utils.constants import TransportProtocol
from a2a.utils.errors import TaskNotCancelableError, UnsupportedOperationError

from ..config import AppConfig
from ..engine import ResearchEngine
from ..providers import MockModelProvider, ReplaySearchProvider, build_providers
from ..storage import RunStore
from .a2a_task_store import SQLiteTaskStore


_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()
MAX_A2A_QUESTION_LENGTH = 4_000
A2A_INPUT_REQUIRED_RESUME_SUPPORTED = False
_A2A_INPUT_REQUIRED_OUTCOMES = {
    "evidence_incomplete",
    "verification_failed",
}
_A2A_LEASE_HEARTBEAT_SECONDS = 5


def _owner_scope(context: RequestContext) -> str:
    return (
        f"{context.call_context.tenant}\0"
        f"{context.call_context.user.user_name}"
    )


def run_id_for_task(task_id: str, owner_scope: str = "") -> str:
    """Map A2A IDs into a dedicated durable-run namespace."""
    material = f"{owner_scope}\0{task_id}" if owner_scope else task_id
    return "a2a-" + hashlib.sha256(material.encode()).hexdigest()[:48]


@dataclass
class _TaskControl:
    cancel_event: threading.Event = field(default_factory=threading.Event)
    terminal_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    running: bool = False
    terminal_state: TaskState | None = None


def _normalize_a2a_origin(
    base_url: str,
) -> tuple[str, tuple[str, str, int]]:
    if not isinstance(base_url, str) or base_url != base_url.strip():
        raise ValueError("A2A base_url must be an HTTP(S) origin")
    if any(ord(character) <= 32 for character in base_url):
        raise ValueError("A2A base_url contains invalid characters")
    parsed = urlsplit(base_url)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("A2A base_url has an invalid port") from error
    scheme = parsed.scheme.casefold()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("A2A base_url must be a credential-free HTTP(S) origin")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError as error:
        raise ValueError("A2A base_url hostname is invalid") from error
    effective_port = port or (443 if scheme == "https" else 80)
    authority_host = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if scheme == "https" else 80
    authority = (
        authority_host
        if effective_port == default_port
        else f"{authority_host}:{effective_port}"
    )
    return f"{scheme}://{authority}", (scheme, hostname, effective_port)


def _strict_authority(
    value: str,
    default_port: int,
) -> tuple[str, int] | None:
    if not value or any(ord(character) <= 32 for character in value):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        port = parsed.port or default_port
    except ValueError:
        return None
    if (
        parsed.scheme
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return None
    return hostname, port


def _a2a_request_metadata_allowed(
    host: str,
    origin: str | None,
    sec_fetch_site: str | None,
    expected_origin: tuple[str, str, int],
) -> bool:
    scheme, expected_host, expected_port = expected_origin
    default_port = 443 if scheme == "https" else 80
    if _strict_authority(host, default_port) != (expected_host, expected_port):
        return False
    if (sec_fetch_site or "").casefold() not in {"", "none", "same-origin"}:
        return False
    if origin is None:
        return True
    if any(ord(character) <= 32 for character in origin):
        return False
    parsed = urlsplit(origin)
    try:
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except ValueError:
        return False
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        return False
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
    except UnicodeError:
        return False
    return (parsed.scheme.casefold(), hostname, port) == expected_origin


def _scope_header_values(scope: dict[str, Any], name: bytes) -> list[str]:
    return [
        value.decode("latin-1")
        for key, value in scope.get("headers", [])
        if key.lower() == name
    ]


class _A2ABoundaryMiddleware:
    def __init__(
        self,
        app,
        *,
        expected_origin: tuple[str, str, int],
        auth_token: str | None,
    ) -> None:
        self.app = app
        self.expected_origin = expected_origin
        self.auth_token = auth_token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        hosts = _scope_header_values(scope, b"host")
        origins = _scope_header_values(scope, b"origin")
        fetch_sites = _scope_header_values(scope, b"sec-fetch-site")
        if (
            len(hosts) != 1
            or len(origins) > 1
            or len(fetch_sites) > 1
            or not _a2a_request_metadata_allowed(
                hosts[0] if hosts else "",
                origins[0] if origins else None,
                fetch_sites[0] if fetch_sites else None,
                self.expected_origin,
            )
        ):
            await JSONResponse(
                {"error": "request origin is not allowed"},
                status_code=403,
            )(scope, receive, send)
            return
        if scope.get("path") == "/a2a" and self.auth_token:
            credentials = _scope_header_values(scope, b"authorization")
            expected = f"Bearer {self.auth_token}"
            if len(credentials) != 1 or not hmac.compare_digest(
                credentials[0], expected
            ):
                await JSONResponse(
                    {"error": "authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )(scope, receive, send)
                return
            scope["user"] = SimpleUser("a2a-bearer")
        await self.app(scope, receive, send)


def _input_message(task_id: str, context_id: str) -> Task:
    return Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
    )


class ResearchAgentExecutor(AgentExecutor):
    """Runs one fenced durable engine execution behind one A2A Task.

    A2A input-required is used to preserve the distinction between an
    evidence/verification boundary and an execution failure.  This adapter
    does not implement continuation of that same Task, so the boundary is
    reported explicitly instead of being silently retried or marked failed.
    """

    def __init__(self, *, offline: bool = False) -> None:
        self.offline = offline
        self._task_controls: dict[str, _TaskControl] = {}
        self._task_controls_lock = threading.Lock()

    def _task_control(self, control_key: str) -> _TaskControl:
        with self._task_controls_lock:
            return self._task_controls.setdefault(control_key, _TaskControl())

    def _retire_task_control(self, control_key: str, control: _TaskControl) -> bool:
        with self._task_controls_lock:
            if control.running or self._task_controls.get(control_key) is not control:
                return False
            self._task_controls.pop(control_key, None)
            return True

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A task and context IDs are required")

        if (
            context.current_task is not None
            and context.current_task.status.state
            == TaskState.TASK_STATE_INPUT_REQUIRED
            and not A2A_INPUT_REQUIRED_RESUME_SUPPORTED
        ):
            boundary_updater = TaskUpdater(
                event_queue, context.task_id, context.context_id
            )
            boundary_message = boundary_updater.new_agent_message(
                parts=[
                    Part(
                        text=(
                            "该 Task 需要补充输入，但本 A2A 适配器不支持 "
                            "input-required 的同 Task 恢复；请提交新的 Task。"
                        )
                    )
                ],
                metadata={
                    "outcome": "input_required",
                    "requires_input": True,
                    "unsupported_operation": "same_task_input_required_resume",
                    "same_task_resume_supported": False,
                },
            )
            await boundary_updater.requires_input(boundary_message)
            return

        question = context.get_user_input().strip()
        if not question:
            raise ValueError("A2A message must contain text input")
        if len(question) > MAX_A2A_QUESTION_LENGTH:
            raise ValueError("A2A message text is too long")

        task_id = context.task_id
        owner_scope = _owner_scope(context)
        run_id = run_id_for_task(task_id, owner_scope)
        control_key = f"{owner_scope}\0{task_id}"
        control = self._task_control(control_key)
        with self._task_controls_lock:
            if control.running:
                raise UnsupportedOperationError(
                    "A2A task is already executing",
                    data={"reason": "execution_in_progress", "task_id": task_id},
                )
            if control.terminal_state is not None:
                raise UnsupportedOperationError(
                    "A2A task already reached a terminal state",
                    data={"reason": "task_terminal", "task_id": task_id},
                )
            control.running = True
        with _cancel_lock:
            _cancel_events[control_key] = control.cancel_event
        updater = TaskUpdater(event_queue, task_id, context.context_id)
        config: AppConfig | None = None
        run_store: RunStore | None = None
        execution_lease: dict[str, object] | None = None
        heartbeat_stop = asyncio.Event()
        lease_lost = asyncio.Event()
        heartbeat_task: asyncio.Task[None] | None = None

        async def heartbeat() -> None:
            while True:
                try:
                    await asyncio.wait_for(
                        heartbeat_stop.wait(), timeout=_A2A_LEASE_HEARTBEAT_SECONDS
                    )
                    return
                except TimeoutError:
                    pass
                if run_store is None or execution_lease is None:
                    return
                try:
                    renewed = await asyncio.to_thread(
                        run_store.heartbeat_execution_lease,
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
                except Exception:
                    renewed = False
                if not renewed:
                    lease_lost.set()
                    control.cancel_event.set()
                    return

        def record_exception_audit(error: BaseException) -> None:
            if run_store is None or execution_lease is None:
                return
            with contextlib.suppress(Exception):
                run_store.event(
                    "a2a_executor_exception",
                    "a2a_gateway",
                    {
                        "task_id": task_id,
                        "run_id": run_id,
                        "owner_scope": owner_scope,
                        "fence": int(execution_lease["fence"]),
                        "receipt_id": str(execution_lease["receipt_id"]),
                        "exception_type": type(error).__name__,
                        "error": str(error)[:2000],
                    },
                )

        try:
            config = AppConfig.from_env()
            run_store = RunStore(config.runs_dir, run_id)
            lease_receipt_id = "a2a:" + hashlib.sha256(
                control_key.encode()
            ).hexdigest()
            execution_lease = run_store.acquire_execution_lease(lease_receipt_id)
            if execution_lease is None:
                raise UnsupportedOperationError(
                    "A2A task execution is currently leased by another worker",
                    data={
                        "reason": "execution_lease_conflict",
                        "task_id": task_id,
                        "retryable": True,
                    },
                )
            run_store.bind_execution_fence(
                str(execution_lease["owner_token"]),
                int(execution_lease["fence"]),
            )
            heartbeat_task = asyncio.create_task(heartbeat())
            if context.current_task is None:
                initial = _input_message(task_id, context.context_id)
                initial.history.extend([context.message] if context.message else [])
                await event_queue.enqueue_event(initial)
            async with control.terminal_lock:
                if control.cancel_event.is_set():
                    await updater.cancel()
                    control.terminal_state = TaskState.TASK_STATE_CANCELED
                    return
                await updater.start_work()
            config = AppConfig.from_env()
            if self.offline:
                model = MockModelProvider()
                search = ReplaySearchProvider(config.replay_corpus)
            else:
                model, search = build_providers(config)
            engine = ResearchEngine(
                config,
                model,
                search,
                cancel_check=lambda: control.cancel_event.is_set()
                or lease_lost.is_set(),
                execution_lease=execution_lease,
            )
            state = await engine.run(question, run_id)
            answer = state.draft_answer or (
                "研究未形成可交付回答："
                + "; ".join(
                    gap.description
                    for gap in (state.closure.gaps if state.closure else [])
                )
            )
            metadata: dict[str, Any] = {
                "deep_research_run_id": state.run_id,
                "outcome": state.status,
                "closure_score_is_probability": False,
                "source_count": len(state.sources),
                "evidence_count": len(state.evidence),
                "verification_passed": (
                    state.verification.passed if state.verification is not None else None
                ),
                "verification_status": (
                    "observed" if state.verification is not None else "unavailable"
                ),
                "same_task_resume_supported": A2A_INPUT_REQUIRED_RESUME_SUPPORTED,
            }
            if state.status in _A2A_INPUT_REQUIRED_OUTCOMES:
                metadata.update(
                    {
                        "requires_input": True,
                        "input_required_reason": state.status,
                        "resume_boundary": (
                            "A new A2A Task is required; this gateway does not "
                            "continue an input-required Task."
                        ),
                    }
                )
            status_message = updater.new_agent_message(
                parts=[Part(text=answer)], metadata=metadata
            )
            async with control.terminal_lock:
                if control.terminal_state is not None:
                    return
                if control.cancel_event.is_set() or state.status == "cancelled":
                    await updater.cancel(status_message)
                    control.terminal_state = TaskState.TASK_STATE_CANCELED
                    return
                if state.status == "completed":
                    await updater.add_artifact(
                        parts=[Part(text=answer)],
                        name="deep-research-answer",
                        metadata=metadata,
                        last_chunk=True,
                    )
                    await updater.complete(status_message)
                    control.terminal_state = TaskState.TASK_STATE_COMPLETED
                elif state.status in _A2A_INPUT_REQUIRED_OUTCOMES:
                    await updater.requires_input(status_message)
                    control.terminal_state = TaskState.TASK_STATE_INPUT_REQUIRED
                else:
                    metadata["outcome"] = state.status
                    await updater.failed(status_message)
                    control.terminal_state = TaskState.TASK_STATE_FAILED
        except UnsupportedOperationError:
            raise
        except asyncio.CancelledError:
            control.cancel_event.set()
            raise
        except Exception as error:
            record_exception_audit(error)
            durable_state = run_store.latest() if run_store is not None else None
            latest_failure = (
                durable_state.failures[-1]
                if durable_state is not None and durable_state.failures
                else {}
            )
            failure_metadata = {
                "deep_research_run_id": run_id,
                "outcome": "failed",
                "failure_type": str(latest_failure.get("type") or type(error).__name__),
                "retryable": bool(latest_failure.get("retryable", False)),
                "same_task_resume_supported": A2A_INPUT_REQUIRED_RESUME_SUPPORTED,
            }
            failure_message = updater.new_agent_message(
                parts=[Part(text="研究运行失败，已保留可用的持久化现场；请由服务端运维人员检查运行记录。")],
                metadata=failure_metadata,
            )
            async with control.terminal_lock:
                if control.terminal_state is None:
                    if control.cancel_event.is_set():
                        with contextlib.suppress(RuntimeError):
                            await updater.cancel(failure_message)
                        control.terminal_state = TaskState.TASK_STATE_CANCELED
                    else:
                        with contextlib.suppress(RuntimeError):
                            await updater.failed(failure_message)
                        control.terminal_state = TaskState.TASK_STATE_FAILED
        finally:
            heartbeat_stop.set()
            if heartbeat_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            if run_store is not None and execution_lease is not None:
                with contextlib.suppress(Exception):
                    run_store.release_execution_lease(
                        str(execution_lease["owner_token"]),
                        int(execution_lease["fence"]),
                    )
            with self._task_controls_lock:
                control.running = False
            with _cancel_lock:
                if _cancel_events.get(control_key) is control.cancel_event:
                    _cancel_events.pop(control_key, None)
            self._retire_task_control(control_key, control)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        if not context.task_id or not context.context_id:
            raise ValueError("A2A task and context IDs are required")
        owner_scope = _owner_scope(context)
        control_key = f"{owner_scope}\0{context.task_id}"
        control = self._task_control(control_key)
        control.cancel_event.set()
        with _cancel_lock:
            _cancel_events[control_key] = control.cancel_event
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            async with control.terminal_lock:
                if control.terminal_state in {
                    TaskState.TASK_STATE_COMPLETED,
                    TaskState.TASK_STATE_FAILED,
                    TaskState.TASK_STATE_REJECTED,
                }:
                    raise TaskNotCancelableError(
                        "A2A task reached a terminal state before cancellation won"
                    )
                await updater.cancel()
                control.terminal_state = TaskState.TASK_STATE_CANCELED
        finally:
            if self._retire_task_control(control_key, control):
                with _cancel_lock:
                    if _cancel_events.get(control_key) is control.cancel_event:
                        _cancel_events.pop(control_key, None)


def build_agent_card(
    base_url: str = "http://127.0.0.1:8010",
    *,
    bearer_auth: bool = False,
) -> AgentCard:
    normalized_base_url, _origin = _normalize_a2a_origin(base_url)
    card = AgentCard(
        name="Fieldnote Deep Research Agent",
        description=(
            "A verification-centric deep research Agent backed by a private "
            "six-role orchestration system. Evidence or verification gaps are "
            "returned as TASK_STATE_INPUT_REQUIRED; this JSON-RPC adapter does "
            "not support same-Task continuation, so clients must submit a new Task."
        ),
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=f"{normalized_base_url}/a2a",
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version="1.0",
            )
        ],
        provider=AgentProvider(
            organization="Fieldnote Research",
            url=normalized_base_url,
        ),
        capabilities=AgentCapabilities(
            streaming=False,
            push_notifications=False,
            extended_agent_card=False,
        ),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="deep-research",
                name="Verifiable deep research",
                description="Researches a question, preserves source evidence, and returns a cited answer or an explicit evidence failure.",
                tags=["research", "evidence", "citations", "verification"],
                examples=["Compare the evidence for two competing technical approaches."],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            )
        ],
    )
    if bearer_auth:
        card.security_schemes["bearer"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    description="Bearer token required for A2A RPC",
                    scheme="bearer",
                )
            )
        )
        card.security_requirements.add().schemes["bearer"].list.extend([])
    return card


def create_app(
    *,
    base_url: str = "http://127.0.0.1:8010",
    offline: bool = False,
    task_store: TaskStore | None = None,
    task_store_path: Path | str | None = None,
    auth_token: str | None = None,
) -> Starlette:
    normalized_base_url, expected_origin = _normalize_a2a_origin(base_url)
    resolved_auth_token = (
        os.environ.get("DR_A2A_AUTH_TOKEN", "")
        if auth_token is None
        else auth_token
    )
    resolved_auth_token = resolved_auth_token or None
    if resolved_auth_token and any(
        ord(character) < 32 or ord(character) == 127
        for character in resolved_auth_token
    ):
        raise ValueError("A2A auth token contains control characters")
    scheme, hostname, _port = expected_origin
    local_or_test_origin = hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
        "testserver",
    }
    if not local_or_test_origin and not resolved_auth_token:
        raise ValueError("Non-loopback A2A origins require Bearer authentication")
    if not local_or_test_origin and scheme != "https":
        raise ValueError("Non-loopback A2A origins require HTTPS")
    card = build_agent_card(
        normalized_base_url,
        bearer_auth=bool(resolved_auth_token),
    )
    if task_store is not None and task_store_path is not None:
        raise ValueError("Pass task_store or task_store_path, not both")
    if task_store is None:
        config = AppConfig.from_env()
        path = Path(task_store_path) if task_store_path else config.runs_dir / "a2a_tasks.sqlite3"
        task_store = SQLiteTaskStore(path)
    handler = DefaultRequestHandler(
        agent_executor=ResearchAgentExecutor(offline=offline),
        task_store=task_store,
        agent_card=card,
    )
    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/a2a"),
        ],
        middleware=[
            Middleware(
                _A2ABoundaryMiddleware,
                expected_origin=expected_origin,
                auth_token=resolved_auth_token,
            )
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the official A2A 1.0 gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument(
        "--task-store",
        type=Path,
        help="SQLite path for durable A2A Task records (default: DR_RUNS_DIR/a2a_tasks.sqlite3)",
    )
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The built-in A2A gateway may only bind to a loopback address")
    import uvicorn

    uvicorn.run(
        create_app(
            base_url=f"http://{args.host}:{args.port}",
            offline=args.offline,
            task_store_path=args.task_store,
        ),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
