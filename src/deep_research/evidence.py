from __future__ import annotations

from collections import defaultdict
import re

from .config import ClosureConfig
from .schemas import ClosureReport, Evidence, EvidenceGap, ResearchPlan, SlotGateAudit


MIN_CLAIM_QUOTE_CONSISTENCY = 0.6


def claim_quote_consistency(claim: str, quote: str) -> tuple[float, list[str]]:
    """Score whether a curator claim stays inside its verbatim source quote.

    This is deliberately a lexical, auditable check rather than a learned
    entailment score.  A language/script mismatch is rejected before lexical
    overlap so a translated plan summary cannot be counted as a source claim.
    """
    claim_folded, quote_folded = claim.casefold(), quote.casefold()
    claim_language = _dominant_writing_system(claim_folded)
    quote_language = _dominant_writing_system(quote_folded)
    if (
        claim_language != "neutral"
        and quote_language != "neutral"
        and claim_language != quote_language
    ):
        return 0.0, [
            "claim and quote use incompatible dominant writing systems: "
            f"{claim_language} vs {quote_language}"
        ]

    claim_numbers = set(re.findall(r"\d+(?:\.\d+)?", claim_folded))
    quote_numbers = set(re.findall(r"\d+(?:\.\d+)?", quote_folded))
    missing_numbers = sorted(claim_numbers - quote_numbers)
    negations = {"no", "not", "never", "without", "不", "未", "无", "没有"}
    claim_negative = any(token in claim_folded for token in negations)
    quote_negative = any(token in quote_folded for token in negations)
    cjk_claim = "".join(re.findall(r"[\u4e00-\u9fff]", claim_folded))
    cjk_quote = "".join(re.findall(r"[\u4e00-\u9fff]", quote_folded))
    if len(cjk_claim) >= 2:
        claim_units = {cjk_claim[index:index + 2] for index in range(len(cjk_claim) - 1)}
        quote_units = {
            cjk_quote[index:index + 2]
            for index in range(max(0, len(cjk_quote) - 1))
        }
    else:
        claim_units = {
            token for token in re.findall(r"[a-z0-9]+", claim_folded) if len(token) > 1
        }
        quote_units = {
            token for token in re.findall(r"[a-z0-9]+", quote_folded) if len(token) > 1
        }
    coverage = len(claim_units & quote_units) / max(1, len(claim_units))
    reasons = [f"claim units covered by quote: {coverage:.1%}"]
    if missing_numbers:
        reasons.append(f"numbers absent from quote: {', '.join(missing_numbers)}")
    if claim_negative != quote_negative:
        reasons.append("claim and quote use different negation polarity")
    score = coverage if not missing_numbers and claim_negative == quote_negative else 0.0
    return round(score, 4), reasons


def claim_quote_admissible(claim: str, quote: str) -> tuple[bool, list[str]]:
    """Apply the pre-ledger contract shared by every online curator."""
    consistency, reasons = claim_quote_consistency(claim, quote)
    if not _has_minimum_factual_content(claim):
        return False, [
            *reasons,
            "claim is too short to identify a minimal factual proposition",
        ]
    if consistency < MIN_CLAIM_QUOTE_CONSISTENCY:
        return False, [
            *reasons,
            "claim does not meet the minimum quote-consistency admission threshold",
        ]
    return True, reasons


def _dominant_writing_system(text: str) -> str:
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin_count = len(re.findall(r"[a-z]", text))
    if cjk_count >= 4 and cjk_count >= latin_count:
        return "cjk"
    if latin_count >= 6 and latin_count > cjk_count:
        return "latin"
    return "neutral"


def _has_minimum_factual_content(claim: str) -> bool:
    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", claim))
    if cjk_count >= 4:
        return True
    lexical_units = [token for token in re.findall(r"[a-z0-9]+", claim.casefold()) if len(token) > 1]
    return len(lexical_units) >= 2 and sum(len(token) for token in lexical_units) >= 6


class EvidenceLedger:
    def merge(self, existing: list[Evidence], incoming: list[Evidence]) -> list[Evidence]:
        by_fingerprint = {
            (item.slot_id, item.content_hash, item.quote.casefold()): item
            for item in existing
        }
        for item in incoming:
            fingerprint = (item.slot_id, item.content_hash, item.quote.casefold())
            current = by_fingerprint.get(fingerprint)
            if current is None or item.reliability > current.reliability:
                by_fingerprint[fingerprint] = item
        return list(by_fingerprint.values())


def _provenance_gate_passed(item: Evidence) -> bool:
    """Require an auditable fetch and immutable snapshot for delivered evidence."""
    snapshot_sha256 = str(item.snapshot_sha256 or "").strip()
    return bool(
        str(item.source_id or "").strip()
        and str(item.fetch_record_id or "").strip()
        and item.snapshot_available is True
        and re.fullmatch(r"[0-9a-fA-F]{64}", snapshot_sha256)
        and str(item.fetch_binding_status or "").strip() == "server_bound"
        and item.fetch_binding_valid is True
        and str(item.content_hash or "").strip()
        and str(item.content_hash_scope or "").strip() not in {"", "unknown"}
    )


class ClosureEngine:
    def __init__(self, config: ClosureConfig) -> None:
        self.config = config

    def evaluate(
        self,
        plan: ResearchPlan,
        evidence: list[Evidence],
        contradiction_checked_slots: set[str] | None = None,
    ) -> ClosureReport:
        contradiction_checked_slots = contradiction_checked_slots or set()
        by_slot: dict[str, list[Evidence]] = defaultdict(list)
        for item in evidence:
            by_slot[item.slot_id].append(item)

        required = [slot for slot in plan.slots if slot.required]
        if not required:
            invalid_plan = EvidenceGap(
                "invalid_plan",
                "__plan__",
                "研究计划没有必需回答目标，不能进入证据闭包或写作。",
                preferred_source="planner_repair",
            )
            return ClosureReport(
                closed=False,
                score=None,
                slot_coverage=None,
                source_independence=None,
                evidence_entailment=None,
                source_reliability=None,
                conflict_resolution=None,
                score_status="invalid",
                gaps=[invalid_plan],
                required_slots=0,
                passed_slots=0,
                hard_gate_passed=False,
                gate_failures=["__plan__"],
                slot_audits=[],
            )
        covered = 0
        gaps: list[EvidenceGap] = []
        independent_scores: list[float] = []
        reliability_scores: list[float] = []
        entailment_scores: list[float] = []
        conflict_scores: list[float] = []
        passed_slots = 0
        gate_failures: list[str] = []
        slot_audits: list[SlotGateAudit] = []

        for slot in required:
            items = by_slot.get(slot.id, [])
            admitted_items = [
                item
                for item in items
                if item.slot_relevance_score >= self.config.min_slot_relevance
            ]
            supporting, contradicting, conflict_resolved = _select_consensus(
                admitted_items,
                slot.description,
            )
            selected_ids = {item.id for item in supporting + contradicting}
            consensus_excluded = [
                item for item in admitted_items if item.id not in selected_ids
            ]
            if not contradicting:
                conflict_resolved = True
            countable_supporting = [
                item for item in supporting if item.independence_status != "dependent"
            ]
            clusters = {_origin_cluster(item) for item in countable_supporting}
            authoritative = any(
                item.reliability >= 0.9
                and item.source_role == "primary"
                and item.independence_status == "verified"
                and _scope_matches_target(item.authority_scope, slot.description)
                for item in supporting
            )
            contested = bool(contradicting)
            required_source_count = (
                self.config.min_independent_sources_for_contested_claim
                if contested
                else self.config.min_sources_per_required_slot
            )
            authoritative_exception_used = (
                not contested
                and self.config.allow_single_authoritative_source
                and authoritative
                and len(clusters) < required_source_count
            )
            source_gate = (
                len(clusters) >= required_source_count
                or authoritative_exception_used
            )
            grounding_gate = bool(supporting) and all(
                item.extraction_confidence >= 0.9
                and item.claim_quote_consistency >= MIN_CLAIM_QUOTE_CONSISTENCY
                and item.slot_relevance_score >= self.config.min_slot_relevance
                and (
                    not item.attachment_id
                    or (
                        item.grounding_confidence >= 0.8
                        and bool(item.source_locator)
                        and bool(item.perception_model)
                    )
                )
                for item in supporting
            )
            provenance_gate = bool(supporting) and all(
                _provenance_gate_passed(item) for item in supporting
            )
            contradiction_checked = slot.id in contradiction_checked_slots
            conflict_gate = conflict_resolved and contradiction_checked
            if supporting:
                covered += 1
                slot.supporting_evidence = [item.id for item in supporting]
                slot.value = max(supporting, key=lambda item: item.reliability).claim
                corroboration = min(1.0, len(clusters) / required_source_count)
                slot.confidence = round(
                    0.35 * max(item.reliability for item in supporting)
                    + 0.30 * corroboration
                    + 0.20 * max(item.extraction_confidence for item in supporting)
                    + 0.15 * (1.0 if conflict_resolved else 0.0),
                    4,
                )
            else:
                gaps.append(EvidenceGap("missing_evidence", slot.id, slot.description))

            if supporting and not source_gate:
                gaps.append(
                    EvidenceGap(
                        "missing_independent_source",
                        slot.id,
                        slot.description,
                        preferred_source="independent_source",
                    )
                )
            if supporting and not grounding_gate:
                gaps.append(EvidenceGap("ungrounded_evidence", slot.id, slot.description))
            if supporting and not provenance_gate:
                gaps.append(
                    EvidenceGap(
                        "unverified_provenance",
                        slot.id,
                        slot.description,
                        preferred_source="fetch_snapshot_audit",
                    )
                )
            if not contradiction_checked:
                gaps.append(
                    EvidenceGap(
                        "contradiction_not_checked",
                        slot.id,
                        slot.description,
                        preferred_source="contradiction_search",
                    )
                )

            independent_scores.append(min(1.0, len(clusters) / required_source_count))
            reliability_scores.append(
                max((item.reliability for item in supporting), default=0.0)
            )
            entailment_scores.append(
                max(
                    (
                        min(
                            item.extraction_confidence,
                            item.claim_quote_consistency,
                            item.slot_relevance_score,
                            (
                                item.grounding_confidence
                                if item.attachment_id
                                else 1.0
                            ),
                        )
                        for item in supporting
                    ),
                    default=0.0,
                )
            )
            conflict_scores.append(1.0 if conflict_gate else 0.0)
            if contradicting and not conflict_resolved:
                gaps.append(EvidenceGap("unresolved_conflict", slot.id, slot.description))
            slot_passed = (
                bool(supporting)
                and source_gate
                and grounding_gate
                and provenance_gate
                and conflict_gate
            )
            if slot_passed:
                passed_slots += 1
            else:
                gate_failures.append(slot.id)
            failure_reasons: list[str] = []
            if not supporting:
                failure_reasons.append("没有支持该目标的证据")
            if supporting and not source_gate:
                failure_reasons.append(
                    f"有效来源簇 {len(clusters)}/{required_source_count}，未达到来源门"
                )
            if supporting and not grounding_gate:
                failure_reasons.append(
                    "至少一条证据未通过原文定位、claim-quote 一致性、目标相关性或多模态定位门"
                )
            if supporting and not provenance_gate:
                failure_reasons.append(
                    "至少一条证据缺少精确 Fetch、server-bound 校验、快照 SHA-256 或正文 hash 作用域"
                )
            if not contradiction_checked:
                failure_reasons.append("尚未完成可验证的反证搜索与页面检查")
            if contradicting and not conflict_resolved:
                failure_reasons.append("存在尚未裁决的反证或候选值")
            slot_audits.append(
                SlotGateAudit(
                    slot_id=slot.id,
                    description=slot.description,
                    passed=slot_passed,
                    supporting_evidence_ids=[item.id for item in supporting],
                    contradicting_evidence_ids=[item.id for item in contradicting],
                    source_clusters=sorted(clusters),
                    required_source_count=required_source_count,
                    effective_source_count=len(clusters),
                    source_gate_passed=source_gate,
                    authoritative_exception_used=authoritative_exception_used,
                    # The existing field is the delivery gate exposed to the
                    # frontend: quote grounding also requires a verifiable
                    # fetch/snapshot chain.
                    exact_quote_gate_passed=grounding_gate and provenance_gate,
                    contradiction_checked=contradiction_checked,
                    conflict_gate_passed=conflict_gate,
                    candidate_values=list(
                        dict.fromkeys(item.claim for item in supporting + contradicting)
                    ),
                    failure_reasons=failure_reasons,
                    origin_clusters=sorted(clusters),
                    weak_provenance_evidence_ids=[
                        item.id
                        for item in supporting
                        if item.independence_status in {
                            "unknown",
                            "weak_host_fallback",
                            "declared_publisher",
                            "declared_upstream",
                        }
                    ],
                    dependent_evidence_ids=[
                        item.id
                        for item in supporting
                        if item.independence_status == "dependent"
                    ],
                    consensus_excluded_evidence_ids=[
                        item.id for item in consensus_excluded
                    ],
                    source_counting_reasons=[
                        f"{item.id}: "
                        f"{'dependent，不计入独立来源数量' if item.independence_status == 'dependent' else f'计入 {_origin_cluster(item)}'}"
                        f" · {item.independence_basis or 'legacy source cluster; provenance not recorded'}"
                        for item in supporting
                    ],
                )
            )

        denominator = max(1, len(required))
        slot_coverage = covered / denominator
        source_independence = sum(independent_scores) / denominator
        evidence_entailment = sum(entailment_scores) / denominator
        source_reliability = sum(reliability_scores) / denominator
        conflict_resolution = sum(conflict_scores) / denominator
        score = (
            0.35 * slot_coverage
            + 0.25 * source_independence
            + 0.20 * evidence_entailment
            + 0.10 * source_reliability
            + 0.10 * conflict_resolution
        )
        return ClosureReport(
            closed=passed_slots == len(required) and not gaps,
            score=round(score, 4),
            slot_coverage=round(slot_coverage, 4),
            source_independence=round(source_independence, 4),
            evidence_entailment=round(evidence_entailment, 4),
            source_reliability=round(source_reliability, 4),
            conflict_resolution=round(conflict_resolution, 4),
            gaps=gaps,
            required_slots=len(required),
            passed_slots=passed_slots,
            hard_gate_passed=passed_slots == len(required) and not gaps,
            gate_failures=gate_failures,
            slot_audits=slot_audits,
        )


def _select_consensus(
    items: list[Evidence],
    target: str = "",
) -> tuple[list[Evidence], list[Evidence], bool]:
    explicit_support = [item for item in items if item.stance == "supports"]
    explicit_contradictions = [item for item in items if item.stance == "contradicts"]

    # Numeric values are competing candidates only when the answer target asks
    # for one numeric fact.  In a synthesis target, years, dataset sizes, and
    # benchmark scores normally describe complementary findings, not a dispute.
    if not _target_requires_numeric_answer(target):
        identity_groups: dict[str, list[Evidence]] = defaultdict(list)
        for item in explicit_support:
            signature = _person_answer_signature(item.claim, target)
            if signature:
                identity_groups[signature].append(item)
        if not identity_groups:
            return explicit_support, explicit_contradictions, not explicit_contradictions
        if len(identity_groups) == 1:
            identity_support = next(iter(identity_groups.values()))
            return (
                identity_support,
                explicit_contradictions,
                not explicit_contradictions,
            )
        ranked_identities = sorted(
            identity_groups.values(),
            key=_consensus_rank,
            reverse=True,
        )
        winner = ranked_identities[0]
        runner_up = ranked_identities[1]
        winner_sources = _consensus_source_count(winner)
        runner_sources = _consensus_source_count(runner_up)
        resolved = winner_sources >= 2 and winner_sources > runner_sources
        contradictions = explicit_contradictions + [
            item for group in ranked_identities[1:] for item in group
        ]
        return winner, contradictions, resolved

    numeric_groups: dict[tuple[str, ...], list[Evidence]] = defaultdict(list)
    for item in explicit_support:
        numbers = tuple(re.findall(r"\b\d{4}\b|\b\d+(?:\.\d+)?\b", item.claim))
        if numbers:
            numeric_groups[numbers].append(item)

    if len(numeric_groups) <= 1:
        if numeric_groups:
            numeric_support = next(iter(numeric_groups.values()))
            return (
                numeric_support,
                explicit_contradictions,
                not explicit_contradictions,
            )
        return explicit_support, explicit_contradictions, not explicit_contradictions

    ranked = sorted(
        numeric_groups.values(),
        key=_consensus_rank,
        reverse=True,
    )
    winner = ranked[0]
    runner_up = ranked[1]
    winner_sources = _consensus_source_count(winner)
    runner_sources = _consensus_source_count(runner_up)
    resolved = winner_sources >= 2 and winner_sources > runner_sources
    contradictions = explicit_contradictions + [item for group in ranked[1:] for item in group]
    return winner, contradictions, resolved


def _origin_cluster(item: Evidence) -> str:
    return item.origin_cluster_id or item.source_cluster_id or "unknown:unresolved-origin"


def _consensus_source_count(group: list[Evidence]) -> int:
    return len(
        {
            _origin_cluster(item)
            for item in group
            if item.independence_status != "dependent"
        }
    )


def _consensus_rank(group: list[Evidence]) -> tuple[int, float]:
    reliability_by_origin: dict[str, float] = {}
    for item in group:
        if item.independence_status == "dependent":
            continue
        origin = _origin_cluster(item)
        reliability_by_origin[origin] = max(
            reliability_by_origin.get(origin, 0.0), item.reliability
        )
    if not reliability_by_origin:
        return 0, 0.0
    return (
        len(reliability_by_origin),
        sum(reliability_by_origin.values()) / len(reliability_by_origin),
    )


def _person_answer_signature(claim: str, target: str) -> str:
    target_folded = target.casefold()
    identity_target = bool(
        re.search(
            r"\bwho\b|\bcreator\b|\bfounder\b|\bauthor\b|\binventor\b|"
            r"\bdeveloper\b|\bdesigner\b|谁|创建者|创始人|作者|发明者|开发者|设计者",
            target_folded,
        )
    )
    if not identity_target:
        return ""
    patterns = (
        r"\b(?:created|founded|authored|invented|developed|designed|written)\s+by\s+([^,.;]+)",
        r"\b(?:creator|founder|author|inventor|developer|designer)\b[^,.;:]{0,40}?\b(?:is|was|:)\s+([^,.;]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, claim, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = re.split(r"\s+(?:in|at|during|on)\s+", match.group(1), maxsplit=1)[0]
        tokens = re.findall(r"[a-z][a-z'-]+", candidate.casefold())
        if 1 <= len(tokens) <= 6:
            return " ".join(tokens)
    return ""


def _target_requires_numeric_answer(target: str) -> bool:
    """Return true only when a slot asks for one numeric fact.

    A broad synthesis often mentions dates, costs, or real-time constraints as
    fields to cover.  Those terms do not make independently sourced papers,
    deployment constraints, or benchmark results mutually contradictory.  The
    previous keyword-only check collapsed such lists to a single winner.
    """

    text = " ".join(target.casefold().split())
    collection_markers = (
        "including",
        "such as",
        "representative",
        "summarize",
        "summary",
        "list ",
        "papers",
        "surveys",
        "benchmarks",
        "leaderboards",
        "challenges",
        "limitations",
        "concerns",
        "progress",
        "metrics",
        "datasets",
        "including",
        "包括",
        "列出",
        "总结",
        "进展",
        "论文",
        "综述",
        "基准",
        "挑战",
        "局限",
        "问题",
    )
    if any(marker in text for marker in collection_markers):
        return False

    return bool(
        re.search(
            r"^\s*(?:when\b|what\s+(?:is\s+the\s+)?(?:year|date|number|count|"
            r"amount|price|cost|rate|percentage|version|age|duration)\b|"
            r"how\s+(?:many|much)\b|which\s+(?:year|date|version)\b)|"
            r"\b(?:release|launch|publication)\s+(?:year|date)\b|"
            r"何时|哪年|发布时间|发布年份|多少|数量|价格|比例|版本|年龄|时长",
            text,
        )
    )


def _scope_matches_target(scope: str, target: str) -> bool:
    if not scope.strip() or not target.strip():
        return False
    stopwords = {
        "the", "a", "an", "is", "was", "were", "of", "to", "in", "on",
        "and", "or", "what", "when", "who", "which", "for", "by", "its",
    }
    target_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", target.casefold())
        if len(token) > 2 and token not in stopwords
    }
    scope_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", scope.casefold())
        if len(token) > 2 and token not in stopwords
    }
    cjk_target = set(re.findall(r"[\u4e00-\u9fff]{2,}", target))
    cjk_scope = set(re.findall(r"[\u4e00-\u9fff]{2,}", scope))
    if cjk_target:
        return bool(cjk_target & cjk_scope)
    if not target_tokens:
        return False
    return len(target_tokens & scope_tokens) / len(target_tokens) >= 0.5
