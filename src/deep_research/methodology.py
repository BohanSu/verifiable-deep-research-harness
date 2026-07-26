from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import ClosureConfig


SOURCE_PRIORS = {
    "official": 0.95,
    "paper": 0.90,
    "reference": 0.82,
    "web": 0.65,
    "user_attachment": 0.70,
}
UNKNOWN_SOURCE_PRIOR = 0.60


def source_prior(source_type: str | None) -> float:
    """Return the uncalibrated policy prior used by every provider.

    This is deliberately a shared policy lookup rather than provider-local
    scoring.  A missing or newly introduced source class must remain visible
    as the fallback prior instead of silently diverging between providers.
    """

    normalized = str(source_type or "").strip().casefold()
    return SOURCE_PRIORS.get(normalized, UNKNOWN_SOURCE_PRIOR)


METRIC_DEFINITION = (
    "closure-v4.23|coverage=.35|declared-upstream-origin-cluster-independence=.25|"
    "source-classifier=conservative-host-bound-v2|"
    "quote-claim-target-consistency=.20|source_prior=.10|conflict=.10|"
    "slot-relevance-admission=.45|explicit-relevance-disclaimer=zero|"
    "relevance-admission-before-consensus|"
    "target-formulation-score=max-slot-description-subgoal-question-source-bound-query|"
    "query-coverage=greedy-required-slot-cover-max3|fetch-allocation=round-robin-query-intents|"
    "claim-admission=same-dominant-writing-system+minimal-fact+quote-consistency>=.60|"
    "contradiction-requires-relevant-inspected-page|"
    "person-attribution-signature-consensus|"
    "authoritative-exception=verified-primary-scope-match|"
    "unknown-origin=shared-unresolved-cluster|dependent=not-counted|"
    "consensus-vote=one-per-origin-dependent-zero|"
    "structured-target-support=winner-only|"
    "aggregation=required-slot-wise-mean|"
    "evidence-entailment=slot-max-of-min-extraction-claim-quote-relevance|"
    "source-reliability=slot-max-supporting-reliability|"
    "source-independence=slot-min-cluster-count-over-required-source-count|"
    "verification=engine-enforced-sentence-coverage-exact-citation-set|"
    "writer-verifier-input=closure-supporting-evidence-only|"
    "multimodal-grounding=content-sha256+locator+perception-model+confidence>=.80|"
    "verification-audit=provider-vs-engine-counts|"
    "verification-enforcement=after-operation-replay|"
    "claim-parser=shared-sentence-semicolon-newline-post-citation-v2|"
    "zero-required-slot-denominator=invalid-null|"
    "hard-gates=source,quote,contradiction,conflict"
)


def _closure_policy_values(config: ClosureConfig | None = None) -> dict[str, Any]:
    config = config or ClosureConfig()
    return {
        "threshold": config.threshold,
        "threshold_role": "diagnostic_only_not_stop_gate",
        "min_sources_per_required_slot": config.min_sources_per_required_slot,
        "min_independent_sources_for_contested_claim": config.min_independent_sources_for_contested_claim,
        "allow_single_authoritative_source": config.allow_single_authoritative_source,
        "min_slot_relevance": config.min_slot_relevance,
    }


def _metric_definition_hash(policy: dict[str, Any]) -> str:
    payload = json.dumps(
        {"definition": METRIC_DEFINITION, "closure_policy": policy},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def methodology_contract() -> dict[str, Any]:
    default_policy = _closure_policy_values()
    return {
        "methodology_version": "evidence-closure-v4.24",
        "metric_definition_hash": _metric_definition_hash(default_policy),
        "snapshot_contract_version": "methodology-snapshot-v2",
        "closure_policy": default_policy,
        "calibrated_probability": False,
        "warning": "All scores are transparent orchestration heuristics, not calibrated probabilities that a claim is true.",
        "source_priors": dict(SOURCE_PRIORS),
        "unknown_source_prior": UNKNOWN_SOURCE_PRIOR,
        "slot_evidence_score": {
            "source_level": 0.35,
            "independent_source_corroboration": 0.30,
            "verbatim_extraction_consistency": 0.20,
            "conflict_resolution": 0.15,
        },
        "closure_score": {
            "answer_slot_coverage": 0.35,
            "source_independence": 0.25,
            "exact_quote_localization": 0.20,
            "source_reliability_prior": 0.10,
            "conflict_resolution": 0.10,
        },
        "metric_contracts": [
            {
                "metric_id": "closure.weighted_progress",
                "name": "流程完成度加权分数",
                "numerator": "先对每个必需回答目标计算五个 0-1 分项，再对各分项按必需目标求算术平均，最后按权重加权求和",
                "denominator": "必需回答目标数；权重总和 1.0；没有必需目标时为 null/invalid，不伪造 0 分",
                "algorithm": "每个槽位：coverage=1(supporting 非空)；source_independence=min(1,非 dependent 的 origin cluster 数/该槽位 required_source_count)；evidence_entailment=max(supporting 中 min(extraction_confidence, claim_quote_consistency, slot_relevance_score))；source_reliability=max(supporting reliability)；conflict_resolution=1 仅当 conflict_resolved 且 contradiction_checked。最终=0.35*mean(coverage)+0.25*mean(source_independence)+0.20*mean(evidence_entailment)+0.10*mean(source_reliability)+0.10*mean(conflict_resolution)。权威来源例外只改变硬门，不把 source_independence 分项直接抬到 1。",
                "decision_role": "仅用于排序证据缺口和展示进度，不能单独触发停止",
            },
            {
                "metric_id": "closure.hard_gate_progress",
                "name": "必需目标硬门进度",
                "numerator": "SlotGateAudit.passed=true 的必需槽位数；每个槽位必须同时通过 supporting、来源数量/权威例外、原文定位与 provenance、反证检查和冲突裁决",
                "denominator": "研究计划中的必需槽位总数",
                "algorithm": "逐槽位硬条件为 supporting 非空 AND source_gate_passed AND exact_quote_gate_passed AND contradiction_checked AND conflict_gate_passed；所有必需槽位通过才允许停止检索并进入写作",
                "decision_role": "强制停止条件，不允许由其他高分项补偿",
            },
            {
                "metric_id": "slot.answer_confidence",
                "name": "回答目标证据充分度",
                "numerator": "单个槽位的 supporting 证据聚合值",
                "denominator": "固定权重总和 1.0；没有 supporting 时 AnswerSlot 保留 schema 初始化值 0.0，闭包分项贡献也为 0",
                "algorithm": "0.35*supporting 中最高 reliability + 0.30*min(1,非 dependent origin cluster 数/required_source_count) + 0.20*supporting 中最高 extraction_confidence + 0.15*(conflict_resolved ? 1 : 0)。它不替代 provenance、claim-quote 或 contradiction 硬门。",
                "decision_role": "用于槽位内排序与展示；不能单独证明事实正确，也不能绕过硬门",
            },
            {
                "metric_id": "sources.independent_clusters",
                "name": "独立来源簇",
                "numerator": "支持证据涉及的不同 origin cluster 数；全文重复、SimHash 近重复和同一可注册域先合并",
                "denominator": "每个槽位实际要求的来源数；争议声明使用更高要求",
                "algorithm": "规范 URL/正文哈希 → SimHash 近重复 → rel=canonical/JSON-LD/转载上游保守合并 → 发布域 fallback → origin cluster",
                "decision_role": "同源证据不重复计数；不同域仍是弱近似，不证明编辑或机构独立",
            },
            {
                "metric_id": "verification.claim_pass_rate",
                "name": "逐句引用通过率",
                "numerator": "状态为 entailed 的预期事实句数",
                "denominator": "写作结果中解析出的全部预期事实句数",
                "algorithm": "引用 ID 必须存在，核验器不得遗漏句子，且引用原文支持完整声明",
                "decision_role": "全部通过才可标记 completed；否则进入定向补证或 verification_failed",
            },
        ],
        "limitations": [
            "Source priors are policy choices and must be calibrated on a labeled benchmark before probabilistic interpretation.",
            "Model extraction confidence is not treated as a probability.",
            "Near duplicates and same registrable domains are merged, but distinct domains remain a weak fallback when upstream ownership is unknown.",
            "Canonical, publisher, author and upstream metadata are self-declared page signals; they may merge potentially dependent sources but never prove publisher identity or independence.",
            "The built-in registrable-domain heuristic is not a complete Public Suffix List implementation.",
            "Claim admission requires a compatible dominant CJK/Latin writing system, a minimally specific fact, matching numbers and negation polarity, and lexical/CJK-bigram coverage. It remains a deterministic screen rather than a complete semantic entailment model.",
            "Claim-to-slot relevance takes the best concept coverage across the slot description, subgoal question, and only the recorded discovery queries bound to that source. When an exact-quote support/contradiction candidate comes from a page retrieved by the same subgoal, a conservative threshold floor avoids CJK/English lexical mismatch; it does not bypass source, quote, provenance, contradiction, or conflict gates. Explicit irrelevance still forces zero, while coreference and deep paraphrase remain heuristic.",
            "The query scheduler greedily selects at most three subgoals that cover the largest number of uncovered required slots, and the remaining page budget is allocated round-robin across those query intents. It improves opportunity fairness but does not assert that a fetched page is relevant.",
            "Claims that explicitly disclaim relevance (for example, 'unrelated' or 'contains no facts about') receive zero target relevance and cannot contribute support or source-count gates.",
            "Target-relevance admission runs before numeric consensus and conflict grouping, so excluded candidates cannot outvote relevant evidence.",
            "English person-attribution claims using created/founded/authored/invented/developed/designed-by patterns are grouped by normalized person signature before consensus; aliases, organizations, multilingual names and complex grammar remain heuristic gaps.",
            "The single-authoritative-source exception requires verified provenance, primary-source role, a reliability prior of at least 0.9, and lexical scope match to the answer target; self-declared publisher metadata cannot activate it.",
            "Evidence with missing origin metadata shares one unresolved-origin cluster, and evidence marked dependent never increases the independent-source count even if it carries a distinct cluster ID.",
            "Numeric and person-attribution consensus gives one vote per non-dependent origin cluster; dependent copies contribute zero votes, and duplicate evidence in one origin cannot amplify consensus weight.",
            "For numeric and person-attribution targets, only evidence selected into the structured consensus winner can populate the answer slot; admitted but unselected candidates remain auditable as consensus-excluded evidence.",
            "The engine recomputes verifier pass/fail from deterministic sentence coverage, known non-empty citations, exact claim text, exact citation-set equality and entailed status; a provider-level passed flag cannot bypass this contract.",
            "Writer and Verifier receive only evidence IDs selected into each slot audit's supporting set; relevance-excluded, consensus-excluded and contradicting evidence remain auditable but cannot be cited for delivery.",
            "Verification reports preserve the provider's raw pass flag and item count alongside the engine-enforced final decision and expected sentence count for human audit.",
            "The deterministic verification contract runs after operation-cache replay as well as after fresh provider responses, so historical raw verifier results cannot bypass current terminal gates.",
            "OpenAI-compatible online models, Mock and engine enforcement share one claim parser; sentence punctuation, Chinese punctuation, semicolons and newlines create independently cited claim units, while a citation-only segment immediately after punctuation binds to the preceding claim.",
            "Image, audio and visually rendered PDF observations are model-generated and are not calibrated truth probabilities; attachment evidence additionally requires the immutable input SHA-256, a human-locatable page/region/time anchor, the exact perception model ID and grounding confidence of at least 0.8, and still cannot satisfy independent-source gates by itself.",
            "A contradiction check counts only after at least one fetched page produces target-relevant evidence; zero results, fetch failures, and irrelevant-only pages do not pass the gate.",
            "A plan with zero required answer slots has no valid metric denominator; closure and component scores are null with score_status=invalid rather than fabricated zeros.",
            "The final citation verifier is a separate gate and does not rely only on these scores.",
        ],
    }


def methodology_snapshot(
    model_provider: str,
    search_provider: str,
    *,
    closure_config: ClosureConfig | None = None,
    min_slot_relevance: float | None = None,
) -> dict[str, Any]:
    policy = _closure_policy_values(closure_config)
    if min_slot_relevance is not None:
        policy["min_slot_relevance"] = min_slot_relevance
    contract = methodology_contract()
    contract["metric_definition_hash"] = _metric_definition_hash(policy)
    contract["closure_policy"] = policy
    return {
        **contract,
        "admission_thresholds": {
            "slot_relevance": policy["min_slot_relevance"],
            "closure_threshold": policy["threshold"],
            "closure_threshold_role": policy["threshold_role"],
        },
        "model_provider": model_provider,
        "search_provider": search_provider,
        "extractor_contract": "exact-substring-minimal-same-language-claim-target-consistency-v6",
        "writer_contract": "closure-supporting-evidence-only-v2",
        "verifier_contract": "engine-enforced-atomic-claim-exact-citation-set-v6",
        "source_independence_contract": "layered-origin-provenance-v3",
    }
