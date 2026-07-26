from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..schemas import (
    ClosureReport,
    AttachmentObservation,
    Evidence,
    EvidenceGap,
    Page,
    Query,
    ResearchPlan,
    SearchResult,
    VerificationReport,
    InputAttachment,
)


@dataclass(slots=True)
class RenderedPage:
    page: int
    media_type: str
    data: bytes


@dataclass(slots=True)
class AttachmentContent:
    attachment: InputAttachment
    data: bytes
    extracted_text: str = ""
    parser_version: str = ""
    rendered_pages: list[RenderedPage] = field(default_factory=list)


class ProviderOutcomeUncertain(RuntimeError):
    """The request may have reached the provider, but no response was recorded."""


class ProviderRequestNotSent(RuntimeError):
    """Transport failed before the model HTTP request body could be sent."""


class ResourceLimitExceededError(RuntimeError):
    """A provider or parser exceeded a hard local resource boundary."""


class ModelProvider(Protocol):
    async def perceive(
        self,
        question: str,
        attachments: list[AttachmentContent],
    ) -> list[AttachmentObservation]: ...

    async def plan(self, question: str) -> ResearchPlan: ...

    async def generate_queries(
        self,
        question: str,
        plan: ResearchPlan,
        gaps: list[EvidenceGap],
        history: list[Query],
    ) -> list[Query]: ...

    async def extract_evidence(
        self, plan: ResearchPlan, pages: list[Page]
    ) -> list[Evidence]: ...

    async def draft(
        self, question: str, plan: ResearchPlan, evidence: list[Evidence]
    ) -> str: ...

    async def verify(
        self, answer: str, evidence: list[Evidence]
    ) -> VerificationReport: ...


class SearchProvider(Protocol):
    async def search(self, query: Query, limit: int = 5) -> list[SearchResult]: ...

    async def fetch(self, result: SearchResult) -> Page: ...
