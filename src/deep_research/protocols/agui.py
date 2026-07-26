from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from ag_ui.core import (
    CustomEvent,
    Interrupt,
    MessagesSnapshotEvent,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedInterruptOutcome,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
)


@dataclass(slots=True)
class ParsedRunAgentInput:
    thread_id: str
    requested_run_id: str | None
    question: str
    messages: list[dict[str, Any]]


def parse_run_agent_input_detailed(payload: dict[str, Any]) -> ParsedRunAgentInput:
    requested_run_id = str(payload.get("runId", "")).strip() or None
    if not requested_run_id:
        raise ValueError("AG-UI RunAgentInput.runId is required; legacy requests must use a generated client runId")
    normalized = dict(payload)
    normalized.setdefault("state", {})
    normalized.setdefault("tools", [])
    normalized.setdefault("context", [])
    normalized.setdefault("forwardedProps", {})
    raw_messages = normalized.get("messages", [])
    if isinstance(raw_messages, list):
        messages = []
        for index, message in enumerate(raw_messages):
            if isinstance(message, dict):
                message = dict(message)
                message.setdefault(
                    "id", f"legacy-message-{index}-{uuid.uuid4().hex[:8]}"
                )
            messages.append(message)
        normalized["messages"] = messages
    try:
        official_input = RunAgentInput.model_validate(normalized)
    except Exception as error:
        raise ValueError(f"invalid AG-UI RunAgentInput: {error}") from error
    thread_id = official_input.thread_id.strip()
    if not thread_id:
        raise ValueError("AG-UI RunAgentInput.threadId is required")
    question = _last_user_message(normalized.get("messages", []))
    if not question:
        forwarded = official_input.forwarded_props
        question = str(forwarded.get("question", "") if isinstance(forwarded, dict) else "").strip()
    if not question and not official_input.resume:
        raise ValueError("AG-UI input requires a user message or forwardedProps.question")
    messages = [
        message.model_dump(by_alias=True, exclude_none=True, mode="json")
        for message in official_input.messages
    ]
    return ParsedRunAgentInput(
        thread_id=thread_id,
        requested_run_id=requested_run_id,
        question=question,
        messages=messages,
    )


def parse_run_agent_input(payload: dict[str, Any]) -> tuple[str, str | None, str]:
    parsed = parse_run_agent_input_detailed(payload)
    return parsed.thread_id, parsed.requested_run_id, parsed.question


def _dump_event(event: Any) -> dict[str, Any]:
    return event.model_dump(by_alias=True, exclude_none=True, mode="json")


def run_started(thread_id: str, run_id: str) -> dict[str, Any]:
    return _dump_event(RunStartedEvent(thread_id=thread_id, run_id=run_id))


def state_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    return _dump_event(StateSnapshotEvent(snapshot=snapshot))


def messages_snapshot(messages: list[dict[str, Any]]) -> dict[str, Any]:
    return _dump_event(MessagesSnapshotEvent(messages=messages))


def interrupt_response_schema(reason: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "action": {"type": "string", "const": "continue_research"},
        "additionalIterations": {"type": "integer", "minimum": 0, "maximum": 5},
        "additionalSearchCalls": {
            "type": "integer",
            "minimum": 0,
            "maximum": 20,
        },
        "additionalPages": {"type": "integer", "minimum": 0, "maximum": 30},
    }
    required = ["action"]
    if reason == "ambiguous_operation":
        properties["action"] = {
            "type": "string",
            "const": "retry_ambiguous_operation",
        }
        properties["confirmAmbiguousRetry"] = {"type": "boolean", "const": True}
        required.append("confirmAmbiguousRetry")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def custom_audit(value: dict[str, Any]) -> dict[str, Any]:
    return _dump_event(CustomEvent(name="deep_research_audit", value=value))


def run_finished(
    thread_id: str,
    run_id: str,
    result: dict[str, Any] | None = None,
    *,
    success: bool = False,
    interrupt_reason: str | None = None,
    interrupt_message: str | None = None,
    interrupt_id: str | None = None,
    response_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    outcome: RunFinishedSuccessOutcome | RunFinishedInterruptOutcome | None
    if success:
        outcome = RunFinishedSuccessOutcome()
    elif interrupt_reason:
        outcome = RunFinishedInterruptOutcome(
            interrupts=[
                Interrupt(
                    id=interrupt_id
                    or f"int:v1:{uuid.uuid4().hex}",
                    reason=interrupt_reason,
                    message=interrupt_message,
                    response_schema=response_schema
                    or interrupt_response_schema(interrupt_reason),
                    metadata=result,
                )
            ]
        )
    else:
        outcome = None
    return _dump_event(
        RunFinishedEvent(
            thread_id=thread_id,
            run_id=run_id,
            result=result,
            outcome=outcome,
        )
    )


def run_error(thread_id: str, run_id: str, message: str) -> dict[str, Any]:
    return _dump_event(
        RunErrorEvent(
            message=message[:2000],
            code="DEEP_RESEARCH_RUN_FAILED",
            threadId=thread_id,
            runId=run_id,
        )
    )


def _last_user_message(messages: object) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") in {"text", "input_text"}
            ]
            return "\n".join(part for part in parts if part)
    return ""
