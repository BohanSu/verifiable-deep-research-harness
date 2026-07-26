from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .engine import ResearchEngine


EVALUATION_SCHEMA_VERSION = "deep-research-evaluation/2.0"
_CITATION_PATTERN = re.compile(r"\[([EI][A-Za-z0-9_-]+)\]")


@dataclass(slots=True)
class EvaluationRecord:
    schema_version: str
    dataset_sha256: str
    task_id: str
    run_id: str
    run_reused: bool
    status: str
    input_task: dict[str, Any]
    reference_answer_count: int
    final_answer: str
    answer_delivery: str
    task_completed: bool
    exact_match: bool | None
    exact_match_status: str
    closure_score: float | None
    closure_score_status: str
    citation_passed: bool | None
    citation_status: str
    verification_item_count: int
    evidence_count: int
    cited_evidence_ids: list[str] = field(default_factory=list)
    iterations: int = 0
    search_calls: int = 0
    pages_fetched: int = 0
    model_calls: int | None = None
    model_cache_hits: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_status: str = "unavailable"
    usage_scope: str = "cumulative_run"
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0
    latency_status: str = "observed"
    latency_scope: str = "fresh_end_to_end"
    event_count: int | None = None
    tool_event_count: int | None = None
    failure_types: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    artifact_status: str = "unavailable"
    artifacts: dict[str, str] = field(default_factory=dict)


async def evaluate_jsonl(
    engine: ResearchEngine,
    dataset_path: Path,
    output_path: Path,
) -> list[EvaluationRecord]:
    dataset_bytes = dataset_path.read_bytes()
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    records: list[EvaluationRecord] = []
    seen_task_ids: set[str] = set()

    for line_number, line in enumerate(
        dataset_bytes.decode("utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        task = json.loads(line)
        task_id = str(task.get("id") or "").strip()
        question = str(task.get("question") or "").strip()
        if not task_id or not question:
            raise ValueError(
                f"Dataset line {line_number} requires non-empty id and question"
            )
        if task_id in seen_task_ids:
            raise ValueError(f"Duplicate task id at line {line_number}: {task_id}")
        seen_task_ids.add(task_id)

        run_id = _evaluation_run_id(task_id)
        run_dir = _run_directory(engine, output_path, run_id)
        run_reused = run_dir.exists()
        started = datetime.now(timezone.utc)
        start_clock = time.perf_counter()
        state: object | None = None
        error: str | None = None
        try:
            state = await engine.run(question, run_id=run_id)
        except Exception as exc:  # Keep the remaining dataset runnable.
            error = f"{type(exc).__name__}: {exc}"[:2000]
        duration_seconds = round(max(0.0, time.perf_counter() - start_clock), 6)
        finished = datetime.now(timezone.utc)

        records.append(
            _build_record(
                engine=engine,
                output_path=output_path,
                dataset_sha256=dataset_sha256,
                task=task,
                task_id=task_id,
                run_id=run_id,
                run_reused=run_reused,
                state=state,
                error=error,
                started_at=started.isoformat(),
                finished_at=finished.isoformat(),
                duration_seconds=duration_seconds,
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(f"{output_path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            [asdict(record) for record in records],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(output_path)
    return records


def _build_record(
    *,
    engine: object,
    output_path: Path,
    dataset_sha256: str,
    task: dict[str, Any],
    task_id: str,
    run_id: str,
    run_reused: bool,
    state: object | None,
    error: str | None,
    started_at: str,
    finished_at: str,
    duration_seconds: float,
) -> EvaluationRecord:
    payload = _state_payload(state)
    final_answer = str(payload.get("draft_answer") or "")
    expected_answers = [
        str(item) for item in task.get("answers", []) if str(item).strip()
    ]
    exact_match = (
        all(item.casefold() in final_answer.casefold() for item in expected_answers)
        if expected_answers
        else None
    )
    closure = payload.get("closure") if isinstance(payload.get("closure"), dict) else None
    verification = (
        payload.get("verification")
        if isinstance(payload.get("verification"), dict)
        else None
    )
    verification_items = (
        verification.get("items", [])
        if verification and isinstance(verification.get("items"), list)
        else []
    )
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    counters = payload.get("counters") if isinstance(payload.get("counters"), dict) else {}
    delivery = (
        payload.get("answer_delivery")
        if isinstance(payload.get("answer_delivery"), dict)
        else {}
    )
    cited_evidence_ids = sorted(set(_CITATION_PATTERN.findall(final_answer)))
    failure_types = _failure_type_counts(payload.get("failures"))
    run_dir = _run_directory(engine, output_path, run_id)
    artifacts, artifact_status = _artifact_manifest(run_dir, output_path.parent)
    event_count, tool_event_count = _event_counts(run_dir / "events.jsonl")

    model_calls = _optional_int(counters.get("model_calls"))
    estimated_cost = _optional_number(counters.get("estimated_cost_usd"))
    return EvaluationRecord(
        schema_version=EVALUATION_SCHEMA_VERSION,
        dataset_sha256=dataset_sha256,
        task_id=task_id,
        run_id=run_id,
        run_reused=run_reused,
        status=str(payload.get("status") or ("failed" if error else "unknown")),
        input_task={key: value for key, value in task.items() if key != "answers"},
        reference_answer_count=len(expected_answers),
        final_answer=final_answer,
        answer_delivery=str(delivery.get("label") or "not_recorded"),
        task_completed=payload.get("status") == "completed",
        exact_match=exact_match,
        exact_match_status="observed" if expected_answers else "not_configured",
        closure_score=(
            _optional_number(closure.get("score")) if closure is not None else None
        ),
        closure_score_status=(
            str(closure.get("score_status") or "observed")
            if closure is not None
            else "unavailable"
        ),
        citation_passed=(
            verification.get("passed")
            if verification is not None
            and isinstance(verification.get("passed"), bool)
            else None
        ),
        citation_status="observed" if verification is not None else "not_run",
        verification_item_count=len(verification_items),
        evidence_count=len(evidence),
        cited_evidence_ids=cited_evidence_ids,
        iterations=_optional_int(counters.get("iterations")) or 0,
        search_calls=_optional_int(counters.get("search_calls")) or 0,
        pages_fetched=_optional_int(counters.get("pages_fetched")) or 0,
        model_calls=model_calls,
        model_cache_hits=_optional_int(counters.get("model_cache_hits")),
        input_tokens=_optional_int(counters.get("input_tokens")),
        output_tokens=_optional_int(counters.get("output_tokens")),
        estimated_cost_usd=estimated_cost,
        cost_status=(
            "estimated" if estimated_cost is not None and model_calls is not None else "unavailable"
        ),
        usage_scope="cumulative_run",
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=duration_seconds,
        latency_status="observed",
        latency_scope=(
            "resume_or_replay_invocation" if run_reused else "fresh_end_to_end"
        ),
        event_count=event_count,
        tool_event_count=tool_event_count,
        failure_types=failure_types,
        error=error,
        artifact_status=artifact_status,
        artifacts=artifacts,
    )


def _state_payload(state: object | None) -> dict[str, Any]:
    if state is None:
        return {}
    as_dict_method = getattr(state, "as_dict", None)
    if callable(as_dict_method):
        payload = as_dict_method()
        return payload if isinstance(payload, dict) else {}

    payload: dict[str, Any] = {}
    for name in (
        "status",
        "draft_answer",
        "answer_delivery",
        "closure",
        "verification",
        "evidence",
        "failures",
        "counters",
    ):
        value = getattr(state, name, None)
        if hasattr(value, "__dataclass_fields__"):
            value = asdict(value)
        elif hasattr(value, "__dict__"):
            value = vars(value)
        payload[name] = value
    if hasattr(payload.get("counters"), "__dict__"):
        payload["counters"] = vars(payload["counters"])
    if hasattr(payload.get("closure"), "__dict__"):
        payload["closure"] = vars(payload["closure"])
    if hasattr(payload.get("verification"), "__dict__"):
        payload["verification"] = vars(payload["verification"])
    return payload


def _evaluation_run_id(task_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", task_id).strip(".-")[:80]
    if safe_id == task_id and safe_id:
        return f"eval-{safe_id}"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
    return f"eval-{safe_id or 'task'}-{digest}"


def _run_directory(engine: object, output_path: Path, run_id: str) -> Path:
    config = getattr(engine, "config", None)
    runs_dir = getattr(config, "runs_dir", None)
    root = Path(runs_dir) if runs_dir is not None else output_path.parent
    return root / run_id


def _artifact_manifest(run_dir: Path, report_dir: Path) -> tuple[dict[str, str], str]:
    paths = {
        "run_directory": run_dir,
        "final_state": run_dir / "final.json",
        "event_trace": run_dir / "events.jsonl",
        "checkpoint_database": run_dir / "checkpoints.sqlite",
        "evidence_artifacts": run_dir / "artifacts",
        "source_snapshots": run_dir / "sources",
    }
    manifest = {
        name: _portable_path(path, report_dir) for name, path in paths.items()
    }
    core_paths = [
        paths["final_state"],
        paths["event_trace"],
        paths["checkpoint_database"],
    ]
    available = sum(path.exists() for path in core_paths)
    status = "complete" if available == len(core_paths) else "partial" if available else "unavailable"
    return manifest, status


def _portable_path(path: Path, report_dir: Path) -> str:
    try:
        return path.resolve().relative_to(report_dir.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _event_counts(events_path: Path) -> tuple[int | None, int | None]:
    if not events_path.is_file():
        return None, None
    event_count = 0
    tool_event_count = 0
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        if str(event.get("event_type") or "").startswith("tool_"):
            tool_event_count += 1
    return event_count, tool_event_count


def _failure_type_counts(value: object) -> dict[str, int]:
    if not isinstance(value, list):
        return {}
    counts: dict[str, int] = {}
    for item in value:
        failure_type = (
            str(item.get("type") or "unclassified")
            if isinstance(item, dict)
            else "unclassified"
        )
        counts[failure_type] = counts.get(failure_type, 0) + 1
    return dict(sorted(counts.items()))


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _optional_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
