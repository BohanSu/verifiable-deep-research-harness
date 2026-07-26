from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from ..schemas import (
    AttachmentObservation,
    AnswerSlot,
    Evidence,
    EvidenceGap,
    GroundedObservation,
    Page,
    Query,
    ResearchPlan,
    SearchResult,
    Subgoal,
    VerificationItem,
    VerificationReport,
)
from .base import AttachmentContent
from ..methodology import source_prior
from ..verification import parse_answer_claims


class ReplaySearchProvider:
    """Deterministic search/fetch provider backed by a local JSON corpus."""

    def __init__(self, corpus_path: Path) -> None:
        self.corpus_path = corpus_path.resolve()
        raw = json.loads(corpus_path.read_text(encoding="utf-8"))
        self._documents = raw["documents"]
        self._by_url = {document["url"]: document for document in self._documents}

    async def search(self, query: Query, limit: int = 5) -> list[SearchResult]:
        terms = set(_tokens(query.text))
        ranked: list[tuple[int, dict[str, str]]] = []
        for document in self._documents:
            haystack = set(_tokens(f'{document["title"]} {document["text"]}'))
            score = len(terms & haystack)
            if score:
                ranked.append((score, document))
        ranked.sort(key=lambda item: (-item[0], item[1]["url"]))
        return [
            SearchResult(
                title=document["title"],
                url=document["url"],
                snippet=document["text"][:240],
                source_type=document.get("source_type", "web"),
            )
            for _, document in ranked[:limit]
        ]

    async def fetch(self, result: SearchResult) -> Page:
        document = self._by_url[result.url]
        content_hash = hashlib.sha256(document["text"].encode()).hexdigest()
        return Page(
            url=document["url"],
            title=document["title"],
            text=document["text"],
            source_type=document.get("source_type", "web"),
            content_hash=content_hash,
            content_hash_scope="page_text",
            fetched_at=datetime.now(UTC).isoformat(),
            http_status=200,
            content_type="text/plain",
            parser_version="replay-corpus-v1",
            bytes_read=len(document["text"].encode()),
        )


class MockModelProvider:
    """Rule-based model replacement for local development and tests."""

    model_choice = "offline"
    model = "mock-model"
    modalities = ("text", "document")
    usage_applicability = "not_applicable"

    async def perceive(
        self,
        question: str,
        attachments: list[AttachmentContent],
    ) -> list[AttachmentObservation]:
        results: list[AttachmentObservation] = []
        for content in attachments:
            text = content.extracted_text.strip()
            observations = []
            if text:
                observations.append(
                    GroundedObservation(
                        locator="extracted text",
                        text=text[:2000],
                        kind="ocr" if content.attachment.media_type == "application/pdf" else "text",
                        confidence=1.0,
                    )
                )
            results.append(
                AttachmentObservation(
                    attachment_id=content.attachment.id,
                    modality=content.attachment.modality,
                    summary=text[:500],
                    observations=observations,
                    model_choice=self.model_choice,
                    model_id=self.model,
                    parser_version=content.parser_version,
                    status="succeeded" if observations else "unsupported_offline",
                    error=(
                        None
                        if observations
                        else "Offline mode cannot perceive binary image or audio content."
                    ),
                )
            )
        return results

    async def plan(self, question: str) -> ResearchPlan:
        normalized = question.lower()
        if "python" in normalized and ("when" in normalized or "released" in normalized):
            slots = [
                AnswerSlot(id="creator", description="The creator of Python"),
                AnswerSlot(id="release", description="Python's first public release date"),
            ]
        else:
            slots = [AnswerSlot(id="answer", description=question)]
        subgoals = [
            Subgoal(
                id=f"sg-{slot.id}",
                question=slot.description,
                slot_ids=[slot.id],
                done_when="At least one reliable source supports the slot",
            )
            for slot in slots
        ]
        return ResearchPlan(answer_type="short_text", slots=slots, subgoals=subgoals)

    async def generate_queries(
        self,
        question: str,
        plan: ResearchPlan,
        gaps: list[EvidenceGap],
        history: list[Query],
    ) -> list[Query]:
        targets = {gap.slot_id for gap in gaps} if gaps else {slot.id for slot in plan.slots}
        history_text = {query.text.casefold() for query in history}
        queries: list[Query] = []
        for subgoal in plan.subgoals:
            if not targets.intersection(subgoal.slot_ids):
                continue
            gap_types = {gap.type for gap in gaps if gap.slot_id in subgoal.slot_ids}
            strategy = (
                "contradiction_check"
                if "contradiction_not_checked" in gap_types
                else "source_targeting"
            )
            suffix = "contradiction correction dispute" if strategy == "contradiction_check" else "official history"
            text = f"{question} {subgoal.question} {suffix}"
            if text.casefold() in history_text:
                text = f"{subgoal.question} independent source chronology"
            queries.append(Query(text=text, subgoal_id=subgoal.id, strategy=strategy))
        return queries[:3]

    async def extract_evidence(
        self, plan: ResearchPlan, pages: list[Page]
    ) -> list[Evidence]:
        evidence: list[Evidence] = []
        for page in pages:
            for slot in plan.slots:
                quote = _extract_slot_quote(slot.id, page.text)
                if not quote:
                    continue
                evidence_id = "E" + hashlib.sha1(
                    f"{slot.id}|{page.url}|{quote}".encode()
                ).hexdigest()[:8]
                evidence.append(
                    Evidence(
                        id=evidence_id,
                        subgoal_id=f"sg-{slot.id}",
                        slot_id=slot.id,
                        claim=quote,
                        quote=quote,
                        source_url=page.url,
                        source_title=page.title,
                        stance="supports",
                        reliability=_source_reliability(page.source_type),
                        extraction_confidence=0.95,
                        content_hash=page.content_hash,
                        source_cluster_id=_source_cluster(page.url),
                        fetch_record_id=page.fetch_record_id,
                        snapshot_sha256=page.snapshot_sha256,
                        snapshot_available=page.snapshot_available,
                        fetch_binding_status=page.fetch_binding_status,
                        fetch_binding_valid=page.fetch_binding_valid,
                        content_hash_scope=page.content_hash_scope,
                        attachment_id=page.attachment_id,
                        modality=page.modality,
                        source_locator=("" if page.attachment_id else page.source_locator),
                        perception_model=page.perception_model,
                        grounding_confidence=0.0 if page.attachment_id else 1.0,
                    )
                )
        return evidence

    async def draft(
        self, question: str, plan: ResearchPlan, evidence: list[Evidence]
    ) -> str:
        by_slot = {slot.id: [] for slot in plan.slots}
        for item in evidence:
            if item.stance == "supports":
                by_slot.setdefault(item.slot_id, []).append(item)
        sentences: list[str] = []
        for slot in plan.slots:
            candidates = by_slot.get(slot.id, [])
            if not candidates:
                continue
            best = max(candidates, key=lambda item: item.reliability)
            claim = best.claim.rstrip(".!?")
            sentences.append(f"{claim} [{best.id}].")
        return " ".join(sentences) or "Insufficient evidence to answer the question."

    async def verify(
        self, answer: str, evidence: list[Evidence]
    ) -> VerificationReport:
        by_id = {item.id: item for item in evidence}
        items: list[VerificationItem] = []
        for expected in parse_answer_claims(answer):
            ids = expected["evidence_ids"]
            cited = [by_id[item_id] for item_id in ids if item_id in by_id]
            plain = expected["claim"]
            if cited and any(item.quote.casefold() in plain.casefold() for item in cited):
                status = "entailed"
                reason = "The answer reproduces the cited evidence claim."
            elif cited:
                status = "partial"
                reason = "A citation exists but does not directly match the claim."
            else:
                status = "unsupported"
                reason = "No valid evidence identifier was cited."
            items.append(
                VerificationItem(
                    plain,
                    ids,
                    status,
                    reason,
                    claim_id=expected["claim_id"],
                    expected_evidence_ids=ids,
                    verifier_evidence_ids=ids,
                    citation_set_match=bool(ids),
                )
            )
        return VerificationReport(
            passed=bool(items) and all(item.status == "entailed" for item in items),
            items=items,
        )


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.casefold())


def _extract_slot_quote(slot_id: str, text: str) -> str | None:
    sentences = _sentences(text)
    if slot_id == "creator":
        return next((s for s in sentences if "created" in s.lower() and "python" in s.lower()), None)
    if slot_id == "release":
        return next((s for s in sentences if "first released" in s.lower()), None)
    return sentences[0] if sentences else None


def _sentences(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[.!?。！？])\s*|\n+", text)
        if item.strip()
    ]


def _source_reliability(source_type: str) -> float:
    return source_prior(source_type)


def _source_cluster(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.casefold().removeprefix("www.")
