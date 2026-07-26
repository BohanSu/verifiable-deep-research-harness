from __future__ import annotations

import uuid
from dataclasses import is_dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable, TypeVar

from .contracts import AgentInvocation
from .evidence import ClosureEngine
from .providers.base import AttachmentContent, ModelProvider, ProviderRequestNotSent
from .schemas import (
    Evidence,
    AttachmentObservation,
    EvidenceGap,
    Page,
    Query,
    ResearchPlan,
    VerificationItem,
    VerificationReport,
)
from .state import ResearchState
from .verification import parse_answer_claims


T = TypeVar("T")
InvocationRecorder = Callable[[AgentInvocation], None]


class RoleAgent:
    def __init__(
        self,
        agent_id: str,
        role: str,
        model: ModelProvider | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.model = model

    async def invoke(
        self,
        state: ResearchState,
        operation: str,
        input_type: str,
        output_type: str,
        function: Callable[[], Awaitable[T]],
        input_summary: str = "",
        consumed_handoff_message_ids: list[str] | None = None,
        provider_call_count: int = 1,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
        input_modalities: list[str] | None = None,
    ) -> T:
        previous = state.agent_invocations[-1] if state.agent_invocations else None
        invocation = AgentInvocation(
            invocation_id=str(uuid.uuid4()),
            agent_id=self.agent_id,
            role=self.role,
            operation=operation,
            attempt=1 + sum(
                item.operation == operation for item in state.agent_invocations
            ),
            started_at=datetime.now(UTC).isoformat(),
            ended_at=None,
            status="running",
            input_type=input_type,
            input_summary=input_summary,
            execution_mode="executed",
            provider_call_count=provider_call_count,
            previous_in_log_id=(previous.invocation_id if previous is not None else None),
            consumed_handoff_message_ids=list(consumed_handoff_message_ids or []),
            run_id=state.run_id,
            trace_id=state.run_id,
            operation_key=operation_key,
            side_effect_status=(
                "unknown" if provider_call_count > 0 else "not_applicable"
            ),
            model_provider=(
                type(self.model).__name__ if self.model is not None else "local"
            ),
            model_choice=str(
                getattr(self.model, "model_choice", "local")
                if self.model is not None
                else "local"
            ),
            model_id=str(
                getattr(self.model, "model", "built-in")
                if self.model is not None
                else "built-in"
            ),
            input_modalities=list(
                input_modalities
                if input_modalities is not None
                else (["text"] if provider_call_count > 0 else [])
            ),
        )
        state.agent_invocations.append(invocation)
        if invocation_recorder is not None:
            invocation_recorder(invocation)
        try:
            result = await function()
        except Exception as error:
            invocation.status = "failed"
            invocation.error = str(error)[:1000]
            invocation.ended_at = datetime.now(UTC).isoformat()
            invocation.side_effect_status = (
                "not_committed"
                if isinstance(error, ProviderRequestNotSent)
                else ("unknown" if provider_call_count > 0 else "not_committed")
            )
            if invocation_recorder is not None:
                invocation_recorder(invocation)
            raise
        invocation.status = "succeeded"
        invocation.output_type = output_type
        invocation.output_summary = _summarize_output(result)
        invocation.ended_at = datetime.now(UTC).isoformat()
        invocation.side_effect_status = (
            "committed" if provider_call_count > 0 else "not_applicable"
        )
        if invocation_recorder is not None:
            invocation_recorder(invocation)
        return result


class PerceptionAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("perception", "multimodal_perception", model)
        self.model = model

    async def perceive(
        self,
        state: ResearchState,
        attachments: list[AttachmentContent],
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ) -> list[AttachmentObservation]:
        modalities = sorted(
            {"text", *(item.attachment.modality for item in attachments)}
        )
        return await self.invoke(
            state,
            "perceive_inputs",
            "ContentAddressedAttachments",
            "GroundedMultimodalObservations",
            lambda: self.model.perceive(state.question, attachments),
            input_summary=(
                f"{len(attachments)} attachments; attachment_ids "
                f"{', '.join(item.attachment.id for item in attachments)}; "
                f"modalities {', '.join(modalities)}"
            ),
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            provider_call_count=len(attachments),
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
            input_modalities=modalities,
        )


class PlannerAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("planner", "research_planner", model)
        self.model = model

    async def plan(
        self,
        state: ResearchState,
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
        question_context: str = "",
    ) -> ResearchPlan:
        effective_question = state.question
        if question_context:
            effective_question = (
                f"{state.question}\n\nGrounded context from user attachments:\n"
                f"{question_context}"
            )
        return await self.invoke(
            state,
            "plan",
            "ResearchQuestion",
            "ResearchPlan",
            lambda: self.model.plan(effective_question),
            input_summary=(
                f"{state.question[:180]}"
                + (f"; {len(question_context)} attachment-context characters" if question_context else "")
            ),
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


class ScoutAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("scout", "retrieval_strategist", model)
        self.model = model

    async def queries(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        gaps: list[EvidenceGap],
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ) -> list[Query]:
        return await self.invoke(
            state,
            "generate_queries",
            "EvidenceGaps",
            "QueryBatch",
            lambda: self.model.generate_queries(state.question, plan, gaps, state.queries),
            input_summary=f"{len(gaps)} evidence gaps; {len(state.queries)} prior queries",
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


class CuratorAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("curator", "evidence_curator", model)
        self.model = model

    async def extract(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        pages: list[Page],
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ) -> list[Evidence]:
        return await self.invoke(
            state,
            "extract_evidence",
            "SourcePages",
            "EvidenceBatch",
            lambda: self.model.extract_evidence(plan, pages),
            input_summary=f"{len(pages)} fetched source pages",
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


class CriticAgent(RoleAgent):
    def __init__(self, closure: ClosureEngine) -> None:
        super().__init__("critic", "evidence_critic")
        self.closure = closure

    async def assess(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ):
        async def evaluate():
            return self.closure.evaluate(
                plan, state.evidence, set(state.contradiction_checked_slots)
            )

        return await self.invoke(
            state,
            "assess_closure",
            "EvidenceLedger",
            "ClosureReport",
            evaluate,
            input_summary=f"{len(state.evidence)} ledger entries",
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            provider_call_count=0,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


class WriterAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("writer", "evidence_writer", model)
        self.model = model

    async def draft(
        self,
        state: ResearchState,
        plan: ResearchPlan,
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ) -> str:
        evidence = closure_supporting_evidence(state)
        return await self.invoke(
            state,
            "draft",
            "ClosedEvidenceLedger",
            "CitedAnswer",
            lambda: self.model.draft(state.question, plan, evidence),
            input_summary=f"{len(evidence)} closure-admitted supporting evidence entries",
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


class VerifierAgent(RoleAgent):
    def __init__(self, model: ModelProvider) -> None:
        super().__init__("verifier", "citation_verifier", model)
        self.model = model

    async def verify(
        self,
        state: ResearchState,
        answer: str,
        consumed_handoff_message_ids: list[str] | None = None,
        invocation_recorder: InvocationRecorder | None = None,
        operation_key: str | None = None,
    ) -> VerificationReport:
        evidence = closure_supporting_evidence(state)
        return await self.invoke(
            state,
            "verify",
            "CitedAnswerAndEvidence",
            "VerificationReport",
            lambda: self.model.verify(answer, evidence),
            input_summary=f"{len(answer)} answer characters; {len(evidence)} closure-admitted evidence entries",
            consumed_handoff_message_ids=consumed_handoff_message_ids,
            invocation_recorder=invocation_recorder,
            operation_key=operation_key,
        )


def enforce_verification_contract(
    answer: str,
    evidence: list[Evidence],
    report: VerificationReport,
    *,
    allowed_evidence_ids: set[str] | None = None,
) -> VerificationReport:
    evidence_ids = {item.id for item in evidence}
    allowed_ids = evidence_ids if allowed_evidence_ids is None else allowed_evidence_ids
    expected_claims = parse_answer_claims(answer)
    by_claim_id = {
        item.claim_id: item for item in report.items if item.claim_id
    }
    count_matches = len(report.items) == len(expected_claims)
    enforced: list[VerificationItem] = []
    for index, expected in enumerate(expected_claims, start=1):
        claim_id = expected["claim_id"]
        expected_ids = expected["evidence_ids"]
        claim = expected["claim"]
        raw = by_claim_id.get(claim_id)
        if raw is None and index <= len(report.items):
            raw = report.items[index - 1]
        failures: list[str] = []
        if not count_matches:
            failures.append(
                f"verifier returned {len(report.items)} items for {len(expected_claims)} answer claims"
            )
        if not expected_ids:
            failures.append("answer sentence has no citation")
        unknown_ids = [value for value in expected_ids if value not in evidence_ids]
        if unknown_ids:
            failures.append(f"answer cites unknown evidence: {', '.join(unknown_ids)}")
        disallowed_ids = [value for value in expected_ids if value not in allowed_ids]
        if disallowed_ids:
            failures.append(
                "answer cites evidence outside the closure supporting set: "
                + ", ".join(disallowed_ids)
            )
        if raw is None:
            failures.append("verifier omitted this answer sentence")
            reported_ids: list[str] = []
        else:
            reported_ids = raw.verifier_evidence_ids or raw.evidence_ids
            if _normalized_claim(raw.claim) != _normalized_claim(claim):
                failures.append("verifier claim text does not match the answer sentence")
            if set(reported_ids) != set(expected_ids):
                failures.append("verifier citation set differs from the answer")
            if not raw.citation_set_match:
                failures.append("verifier did not confirm exact citation-set equality")
            if raw.status != "entailed":
                failures.append(f"verifier status is {raw.status}, not entailed")
        enforced.append(
            VerificationItem(
                claim=claim,
                evidence_ids=[value for value in expected_ids if value in evidence_ids],
                status="entailed" if not failures else "unsupported",
                reason=(raw.reason if raw and not failures else "; ".join(failures)),
                claim_id=claim_id,
                expected_evidence_ids=expected_ids,
                verifier_evidence_ids=reported_ids,
                citation_set_match=not failures,
            )
        )
    return VerificationReport(
        passed=bool(enforced) and all(item.status == "entailed" for item in enforced),
        items=enforced,
        provider_passed=report.passed,
        expected_item_count=len(expected_claims),
        provider_item_count=len(report.items),
        contract_version="engine-verification-contract-v6",
    )


def _normalized_claim(value: str) -> str:
    return " ".join(value.casefold().split())


def closure_supporting_evidence(state: ResearchState) -> list[Evidence]:
    allowed_ids = {
        evidence_id
        for audit in (state.closure.slot_audits if state.closure else [])
        for evidence_id in audit.supporting_evidence_ids
    }
    return [item for item in state.evidence if item.id in allowed_ids]


class AgentTeam:
    def __init__(self, model: ModelProvider, closure: ClosureEngine) -> None:
        resolve = getattr(model, "provider_for", None)

        def provider(role: str) -> ModelProvider:
            return resolve(role) if callable(resolve) else model

        self.planner = PlannerAgent(provider("planner"))
        self.perception = PerceptionAgent(provider("perception"))
        self.scout = ScoutAgent(provider("scout"))
        self.curator = CuratorAgent(provider("curator"))
        self.critic = CriticAgent(closure)
        self.writer = WriterAgent(provider("writer"))
        self.verifier = VerifierAgent(provider("verifier"))


def _summarize_output(value: object) -> str:
    if isinstance(value, list):
        return f"{len(value)} items"
    if isinstance(value, str):
        return f"{len(value)} characters"
    if is_dataclass(value):
        return type(value).__name__
    return type(value).__name__
