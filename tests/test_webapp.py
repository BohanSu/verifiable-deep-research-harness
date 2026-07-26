import json
import hashlib
import os
import re
import sqlite3
import subprocess
import threading
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from deep_research import webapp
from deep_research.contracts import AgentInvocation
from deep_research.schemas import Evidence, Page, SourceRecord
from deep_research.state import ResearchState
from deep_research.storage import RunStore
from deep_research.system_contract import system_contract


def _evaluate_run_script(expression: str) -> object:
    root = Path(__file__).resolve().parents[1]
    script = (root / "web" / "run.js").read_text(encoding="utf-8")
    declarations = script.split("\n$('stopButton').addEventListener", 1)[0]
    harness = f"""
const vm = require('vm');
const context = {{
  window: {{
    location: {{search: ''}},
    matchMedia: () => ({{matches: false}}),
    localStorage: {{getItem: () => null, setItem: () => {{}}}},
    __latestState: {{}},
  }},
  document: {{getElementById: () => null}},
  URL,
  URLSearchParams,
  console,
  setTimeout,
  clearTimeout,
}};
vm.createContext(context);
vm.runInContext({json.dumps(declarations)}, context);
const result = vm.runInContext({json.dumps(expression)}, context);
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-"],
        check=True,
        capture_output=True,
        input=harness,
        text=True,
    )
    return json.loads(completed.stdout)


class FrontendAssetContractTest(unittest.TestCase):
    def test_run_script_static_ids_exist_and_html_ids_are_unique(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        html_ids = re.findall(r'\bid="([^"]+)"', html)
        referenced_ids = set(re.findall(r"\$\('([^']+)'\)", script))

        self.assertEqual(len(html_ids), len(set(html_ids)), "run.html contains duplicate IDs")
        self.assertEqual(referenced_ids - set(html_ids), set())

    def test_human_facing_runtime_sections_remain_separate_and_auditable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")

        for required_id in (
            "researchPulse",
            "agentNetwork",
            "collaborationMap",
            "researchGraph",
            "sourceJourney",
            "slotGateAudit",
            "auditDialog",
        ):
            self.assertIn(f'id="{required_id}"', html)

    def test_hex_agent_nodes_leave_room_for_runtime_proof(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")
        node_tags = re.findall(
            r'<foreignObject class="agent-node-object"[^>]+>', html
        )

        self.assertEqual(len(node_tags), 6)
        self.assertTrue(
            all(re.search(r'width="132" height="104"', tag) for tag in node_tags)
        )
        centers = [
            (
                float(re.search(r'x="([0-9.]+)"', tag).group(1)) + 66,
                float(re.search(r'y="([0-9.]+)"', tag).group(1)) + 52,
            )
            for tag in node_tags
        ]
        expected = {
            (350.0, 55.0),
            (527.535, 157.5),
            (527.535, 362.5),
            (350.0, 465.0),
            (172.465, 362.5),
            (172.465, 157.5),
        }
        self.assertEqual({(round(x, 3), round(y, 3)) for x, y in centers}, expected)
        ordered_centers = [
            (350.0, 55.0),
            (527.535, 157.5),
            (527.535, 362.5),
            (350.0, 465.0),
            (172.465, 362.5),
            (172.465, 157.5),
        ]
        edge_lengths = [
            ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            for (x1, y1), (x2, y2) in zip(
                ordered_centers,
                ordered_centers[1:] + ordered_centers[:1],
            )
        ]
        self.assertTrue(
            all(abs(length - 205.0) < 0.001 for length in edge_lengths),
            "the six role centers must remain vertices of one regular hexagon",
        )
        anchor_tags = re.findall(r'<circle class="hex-node-anchor"[^>]+>', html)
        self.assertEqual(len(anchor_tags), 6)
        anchors = {
            (
                round(float(re.search(r'cx="([0-9.]+)"', tag).group(1)), 3),
                round(float(re.search(r'cy="([0-9.]+)"', tag).group(1)), 3),
            )
            for tag in anchor_tags
        }
        self.assertEqual(anchors, expected)
        self.assertIn('viewBox="0 -24 700 568"', html)
        self.assertIn(
            ".agent-network>svg{width:700px!important;min-width:700px!important;height:568px!important",
            styles,
        )
        self.assertIn("position:static!important;", styles)
        self.assertIn("inset:auto!important;", styles)
        self.assertIn("transform:none!important;", styles)
        self.assertIn("aspect-ratio:700 / 568", styles)
        self.assertIn(".agent-network>svg{\n    margin:0!important;", styles)

    def test_completed_dossier_uses_human_safe_runtime_language(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="overviewAgents"', html)
        self.assertIn('id="overviewSources"', html)
        self.assertIn('id="overviewTraceSummary"', html)
        self.assertIn('id="overviewTimelineJump"', html)
        self.assertIn('class="overview-facts-wrap"', html)
        self.assertIn("这些数字来自运行记录，不是事实为真的概率", html)
        self.assertNotIn('class="overview-main"', html)
        self.assertLess(html.index('id="overviewAgents"'), html.index('id="overviewFacts"'))
        self.assertIn('id="answerBridge"', html)
        self.assertIn('id="sectionNavToggle"', html)
        self.assertIn('id="sectionNavCurrent"', html)
        self.assertIn('id="sectionNavLinks"', html)
        self.assertIn("renderBreakdown(state.closure || null, state.methodology || {})", script)
        self.assertIn("methodology?.closure_score", script)
        self.assertIn("不能复算贡献分", script)
        self.assertIn("外部模型请求", html)
        self.assertNotIn("实际模型请求", html)
        self.assertIn("closureAdmittedEvidence", script)
        self.assertIn("本次运行未记录", script)
        self.assertIn("data-overview-handoff", script)
        self.assertIn('grid-template-areas:"header" "answer" "trace" "agents" "facts"', styles)
        self.assertIn(".run-page .provider>span:last-child", styles)
        self.assertIn("cite-cluster", script)
        self.assertIn("overviewCitationItems", script)
        self.assertIn("overview-citation-source", script)
        self.assertIn("每句话都能对应到已保存的引用原文，不代表系统认证事实绝对正确", script)
        self.assertIn("六个内部角色已结束本轮协作", script)
        self.assertIn("最终回答已交付，引用材料可逐句回查", script)
        self.assertIn("引用材料已完成本地绑定", script)
        self.assertIn("function runtimeSummary", script)
        self.assertIn("原始输入字段", script)
        self.assertIn("能力接口调用", script)
        self.assertNotIn("次外部请求", script)
        self.assertIn("sectionNavCurrent", script)
        self.assertIn(".section-nav.is-open .section-nav-links", styles)
        self.assertIn("grid-template-columns:repeat(2,minmax(0,1fr))", styles)
        self.assertIn(".selection-review-panel>div{grid-template-columns:1fr", styles)
        self.assertIn(".overview-answer .cite-cluster{max-width:100%;white-space:normal}", styles)
        self.assertIn(".agent-cockpit>.network-transfer{position:static", styles)
        self.assertIn(".journey-technical", styles)
        self.assertIn('id="graphPanHint"', html)
        self.assertIn("let graphFitMode = true", script)
        self.assertNotIn("$('agentNetwork').classList.add('mobile-collapsed')", script)
        self.assertIn("短答案已在上方结论卡完整展示", html)
        self.assertIn("六智能体正六边形关系图", html)
        self.assertIn("network-direction-key", html)
        self.assertIn("蓝：发送与接收信息能对应", html)
        self.assertIn("红：补充材料路线或记录不一致", html)
        self.assertIn('id="agentNetworkLive"', html)
        self.assertIn("showNetworkEdgeAudit", script)
        self.assertIn("当前真实调用", script)
        self.assertIn('id="networkEdgeList"', html)
        self.assertIn('id="networkSequenceList"', html)
        self.assertIn('id="networkRepairLedger"', html)
        self.assertIn("只有同一条交接记录里，发送、接收、系统确认、阶段检查和已保存产物都能对上", html)
        self.assertIn("真实执行顺序和任务交接记录分开统计", html)
        self.assertIn("横向滑动逐边查验", html)
        self.assertIn("transform-box:view-box;transform-origin:350px 260px", styles)
        self.assertIn("width:132px!important", styles)
        self.assertIn("height:104px!important", styles)
        self.assertIn("A252 252", html)
        self.assertIn("outerAgentRoutePath", script)
        self.assertIn(".network-edge-list{display:flex", styles)
        self.assertIn(".network-edge-proof{display:flex", styles)
        self.assertIn(".network-repair-ledger>div{display:flex", styles)
        self.assertIn("sourceSnapshotCapability", script)
        self.assertIn("evidenceForExactFetch", script)
        self.assertIn("不能读取这篇文章的保存快照", script)
        self.assertIn("sourceQueryIndices(source,[item]).includes(0)", script)
        self.assertNotIn("asArray(source?.query_texts).includes(item?.text)", script)
        self.assertIn("页面读取计数未记录", script)
        self.assertIn("开销字段未记录", script)
        self.assertIn("confidence-bar[data-unavailable]", styles)

    def test_runtime_accessibility_and_confirmation_contracts_are_present(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="resumeDialog"', html)
        self.assertIn('id="resumeDialogDetails"', html)
        self.assertIn('id="graphAccessibleList"', html)
        self.assertIn('id="researchGraph" role="group"', html)
        self.assertNotIn('id="researchGraph" role="img"', html)
        self.assertIn('role="status" aria-live="polite"', html)
        self.assertGreaterEqual(html.count('class="sr-only">'), 10)
        for section_id in (
            "researchPulse",
            "gateConsole",
            "resultOverview",
            "answerSection",
            "orchestrationSection",
            "auditTimelineSection",
            "metricConsole",
            "methodSection",
            "graphSection",
            "evidenceSection",
        ):
            section_tag = re.search(
                rf'<section\b[^>]*\bid="{section_id}"[^>]*>', html
            )
            self.assertIsNotNone(section_tag, section_id)
            self.assertIn("aria-labelledby=", section_tag.group(0))

        self.assertIn("const resumeBudgetExtension", script)
        self.assertIn("...resumeBudgetExtension", script)
        self.assertIn("requestResumeConfirmation", script)
        self.assertNotIn("window.confirm", script)
        self.assertIn("scrollToSlotAudit", script)
        self.assertIn("lastAnnouncedRuntimeKey", script)
        self.assertIn("已截断更早记录", script)
        self.assertIn("全局编号", script)
        self.assertNotIn(".toggle input { display: none; }", styles)
        self.assertIn(".toggle input:focus-visible + span", styles)
        self.assertIn(".toggle:has(input:checked)::after", styles)
        self.assertIn(".graph-accessible-list", styles)

    def test_recovery_audit_guide_is_filter_scoped_and_human_facing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="resumeAuditGuide"', html)
        self.assertIn('id="resumeAuditGuideSummary"', html)
        self.assertIn('class="resume-audit-guide hidden"', html)
        self.assertIn("恢复获准，不等于已经恢复完成", html)
        self.assertNotIn("RECOVERY AUDIT ORDER", html)
        self.assertIn("function updateResumeAuditGuide(entries)", script)
        self.assertIn("ledgerFilter === 'resume' && resumeEntries.length > 0", script)
        self.assertIn("updateResumeAuditGuide(entries);", script)
        self.assertIn(".resume-audit-guide", styles)
        self.assertIn(".receipt-state-guide", styles)
        self.assertNotIn("run-orientation", html)
        self.assertNotIn("cockpit-reading-guide", html)

    def test_home_history_exposes_loading_empty_and_error_states(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('aria-busy="true"', html)
        self.assertIn('id="historyStatus"', html)
        self.assertIn("historyScore", script)
        self.assertIn("历史记录暂时不可用", script)
        self.assertIn("data-history-retry", script)
        self.assertIn(".history-state.error", styles)

    def test_home_profile_selector_submits_team_and_multimodal_form_data(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        script = (root / "web" / "app.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn('id="modelSelector"', html)
        for profile in ("team", "qwen", "gpt", "deepseek"):
            self.assertIn(f'value="{profile}"', html)
        self.assertIn('name="profile" value="team" checked', html)
        self.assertIn('id="attachmentInput"', html)
        self.assertIn("const body = new FormData()", script)
        self.assertIn("body.append('profile', selectedProfileId())", script)
        self.assertIn("body.append('attachments', item.file, item.file.name)", script)
        self.assertIn("selected?.configured", script)
        self.assertIn("verified_input_modalities", script)
        self.assertIn("已实测", script)
        self.assertIn(".model-options", styles)
        self.assertIn(".attachment-dropzone", styles)
        self.assertIn(".role-route-list", styles)
        self.assertIn("preview.dataset.state = 'loading'", script)
        self.assertIn("图片预览失败", script)
        self.assertIn(".attachment-preview[data-state=\"error\"]", styles)
        self.assertIn("object-fit:contain", styles)

    def test_run_page_exposes_multimodal_ingress_before_the_six_role_chain(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        for element_id in (
            "inputPerceptionSection",
            "perceptionFlow",
            "perceptionModel",
            "perceptionObservationCount",
            "perceptionBoundary",
            "attachmentAuditGrid",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertLess(
            html.index('id="inputPerceptionSection"'),
            html.index('id="agentPhaseRail"'),
        )
        self.assertEqual(html.count('class="agent-node-object"'), 6)
        self.assertIn("感知置信度也不是事实概率", html)
        self.assertIn("confidence ≥ 0.80", html)
        self.assertIn("renderInputPerception(state, audit)", script)
        self.assertIn("inputAttachmentById", script)
        self.assertIn("data-perception-invocation", script)
        self.assertIn("modelRouteLabel", script)
        self.assertIn("输入模态", script)
        self.assertIn(".input-perception", styles)
        self.assertIn(".attachment-observations", styles)
        self.assertIn("observationTextMarkup", script)
        self.assertIn("查看完整观察原文", script)
        self.assertIn(".observation-text-details", styles)
        self.assertIn('id="briefGapAudit"', html)
        self.assertIn("auditTextPreview(gap.description, 180)", script)
        self.assertIn("@media(max-width:480px)", styles)

    def test_long_grounded_observation_has_preview_and_expandable_full_text(self) -> None:
        result = _evaluate_run_script(
            "observationTextMarkup('A'.repeat(260), 40)"
        )
        self.assertIn('class="observation-text-details"', result)
        self.assertIn("查看完整观察原文 · 260 字符", result)
        self.assertIn("A" * 260, result)
        self.assertLess(result.index("…"), result.index("查看完整观察原文"))

    def test_long_gap_is_compact_in_summary_without_removing_audit_route(self) -> None:
        result = _evaluate_run_script(
            "auditTextPreview('B'.repeat(260), 40)"
        )
        self.assertLess(len(result), 80)
        self.assertIn("…（完整说明见逐目标检查记录）", result)

    def test_team_routes_and_grounding_threshold_have_fail_closed_view_models(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const methodology = {
    model_profile: 'team',
    model_routes: {
      perception: {choice: 'gpt', model: 'gpt-5.4-nano', modalities: ['text', 'image', 'audio']},
      planner: {choice: 'gpt', model: 'gpt-5.4-nano', modalities: ['text']},
      scout: {choice: 'deepseek', model: 'deepseek-v4-flash', modalities: ['text']},
      curator: {choice: 'deepseek', model: 'deepseek-v4-flash', modalities: ['text']},
      writer: {choice: 'gpt', model: 'gpt-5.4-nano', modalities: ['text']},
      verifier: {choice: 'qwen', model: 'qwen3.6-35b-a3b', modalities: ['text']},
    },
  };
  const grounding = attachmentGroundingModel({observations: [
    {locator: 'page 1, chart title', confidence: 0.8, text: 'eligible'},
    {locator: '', confidence: 0.99, text: 'missing locator'},
    {locator: '00:02-00:03', confidence: 0.79, text: 'below threshold'},
    {locator: 'page 2', confidence: null, text: 'missing score'},
  ]});
  const summary = routeModelSummary(methodology);
  return {
    choices: summary.choices,
    isTeam: summary.isTeam,
    perception: modelRouteLabel(modelRouteFor('perception', methodology)),
    critic: modelRouteLabel(modelRouteFor('critic', methodology)),
    verifier: modelRouteLabel(modelRouteFor('verifier', methodology)),
    grounding: {total: grounding.total, eligible: grounding.eligible, located: grounding.located},
  };
})()
"""
        )
        self.assertEqual(result["choices"], ["gpt", "deepseek", "qwen"])
        self.assertTrue(result["isTeam"])
        self.assertIn("gpt-5.4-nano", result["perception"])
        self.assertEqual(result["critic"], "本地固定规则检查")
        self.assertIn("qwen3.6-35b-a3b", result["verifier"])
        self.assertEqual(
            result["grounding"], {"total": 4, "eligible": 1, "located": 3}
        )

    def test_metric_empty_denominators_are_not_presented_as_zero_percent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn("暂无可计算比值", script)
        self.assertIn("尚未建立分母", script)
        self.assertIn("data-unavailable", script)
        self.assertIn(".metric-meter[data-unavailable]", styles)

    def test_malformed_numeric_fields_are_not_coerced_to_zero(self) -> None:
        result = _evaluate_run_script(
            "[finiteValue(false), finiteValue([]), finiteValue('   '), finiteValue(0)]"
        )
        self.assertEqual(result, [None, None, None, 0])

    def test_verification_ratio_requires_matching_provider_and_contract_counts(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const base = {
    items: [
      {claim_id: 'C1', claim: 'one', status: 'entailed', expected_evidence_ids: ['E1'], verifier_evidence_ids: ['E1'], citation_set_match: true},
      {claim_id: 'C2', claim: 'two', status: 'unsupported', expected_evidence_ids: [], verifier_evidence_ids: [], citation_set_match: true},
    ],
    expected_item_count: 2, provider_item_count: 2,
    contract_version: 'engine-verification-contract-v6', passed: false,
  };
  return {
    valid: verificationModel(base),
    providerMismatch: verificationModel({...base, provider_item_count: 1}),
    invalidStatus: verificationModel({...base, items: base.items.map(item => ({...item, status: 'partial'}))}),
    missingCitationContract: verificationModel({...base, items: [{...base.items[0], verifier_evidence_ids: null}]}),
  };
})()
"""
        )
        self.assertTrue(result["valid"]["providerCountMatches"])
        self.assertEqual(result["valid"]["ratio"], 0.5)
        for key in ("providerMismatch", "invalidStatus", "missingCitationContract"):
            self.assertIsNone(result[key]["ratio"])
            self.assertFalse(result[key]["contractComplete"])

    def test_durable_fetch_coverage_fails_closed_for_unavailable_audit_hard_gates_duplicates_and_contradictions(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const f1 = {fetch_record_id: 'F1', source_id: 'S1', status: 'fetched', fetch_mode: 'live_provider', binding_status: 'server_bound', binding_valid: true};
  const f2 = {fetch_record_id: 'F2', source_id: 'S2', status: 'fetched', fetch_mode: 'live_provider', binding_status: 'server_bound', binding_valid: true};
  const base = {
    plan: {slots: [{id: 'slot-1', description: '目标', required: true}]},
    closure: {required_slots: 1, slot_audits: [{slot_id: 'slot-1', passed: true, supporting_evidence_ids: ['E1'], contradicting_evidence_ids: ['E2'], source_gate_passed: true, exact_quote_gate_passed: true, contradiction_checked: true, conflict_gate_passed: true}]},
    evidence: [
      {id: 'E1', slot_id: 'slot-1', source_id: 'S1', fetch_record_id: 'F1', fetch_binding_status: 'server_bound', fetch_binding_valid: true},
      {id: 'E2', slot_id: 'slot-1', source_id: 'S2', fetch_record_id: 'F2', fetch_binding_status: 'server_bound', fetch_binding_valid: true},
      {id: 'cross-slot', slot_id: 'other-slot', source_id: 'S2', fetch_record_id: 'F2', fetch_binding_status: 'server_bound', fetch_binding_valid: true},
    ],
    sources: [{id: 'S1', fetch_attempts: [f1]}, {id: 'S2', fetch_attempts: [f2]}],
  };
  window.__latestAudit = {available: true, sourceFetchesRecorded: true, sourceFetches: [f1, f2], pages: {source_fetches: {windowed: true, hasMore: false, items: [f1, f2]}}};
  const contradictionOnly = sourcePageCoverageModel(base);
  const hardGateFail = sourcePageCoverageModel({...base, closure: {...base.closure, slot_audits: [{...base.closure.slot_audits[0], conflict_gate_passed: false}]}});
  window.__latestAudit = {...window.__latestAudit, available: false};
  const auditUnavailable = sourcePageCoverageModel(base);
  window.__latestAudit = {available: true, sourceFetchesRecorded: true, sourceFetches: [f1, {...f1}], pages: {source_fetches: {windowed: true, hasMore: false, items: [f1, {...f1}]}}};
  const duplicate = sourcePageCoverageModel(base);
  return {
    contradictionOnly: {ratio: contradictionOnly.ratio, numerator: contradictionOnly.numerator, denominator: contradictionOnly.denominator},
    hardGateFail: {ratio: hardGateFail.ratio, numerator: hardGateFail.numerator},
    auditUnavailable: {ratio: auditUnavailable.ratio, denominator: auditUnavailable.denominator},
    duplicate: {ratio: duplicate.ratio, denominator: duplicate.denominator},
  };
})()
"""
        )
        self.assertEqual(result["contradictionOnly"], {"ratio": 0.5, "numerator": 1, "denominator": 2})
        self.assertEqual(result["hardGateFail"], {"ratio": None, "numerator": None})
        self.assertEqual(result["auditUnavailable"], {"ratio": None, "denominator": None})
        self.assertEqual(result["duplicate"], {"ratio": None, "denominator": None})

    def test_live_transport_and_pagination_have_fail_safe_loading_contracts(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        self.assertIn("liveWatchdog", script)
        self.assertIn("fallbackToPolling", script)
        self.assertIn("实时事件流返回了无法解析的 JSON", script)
        self.assertIn('aria-busy=\"${String(pending)}\"', script)
        self.assertIn('id="auditWindowStatus" role="status" aria-live="polite" aria-atomic="true" tabindex="-1"', html)
        self.assertIn('id="protocolRuntimeAudit" tabindex="-1"', html)

    def test_page_coverage_requires_closure_admission_and_exact_fetch_binding(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const fetch = {
    fetch_record_id: 'F1', source_id: 'S1', status: 'fetched',
    fetch_mode: 'live_provider', binding_status: 'server_bound',
    binding_valid: true, invocation_id: 'I1',
    final_url: 'https://example.test/article',
    content_hash: 'body-hash', content_hash_scope: 'page_text',
    snapshot_sha256: 'a'.repeat(64),
  };
  const state = {
    plan: {slots: [{id: 'slot-1', description: '目标', required: true}]},
    closure: {required_slots: 1, slot_audits: [{
      slot_id: 'slot-1', passed: true,
      supporting_evidence_ids: ['E1'], contradicting_evidence_ids: [],
      source_gate_passed: true, exact_quote_gate_passed: true,
      contradiction_checked: true, conflict_gate_passed: true,
    }]},
    evidence: [{
      id: 'E1', slot_id: 'slot-1', source_id: 'S1',
      source_url: 'https://example.test/article', fetch_record_id: 'F1',
      content_hash: 'body-hash', content_hash_scope: 'page_text',
      snapshot_sha256: 'a'.repeat(64), fetch_binding_status: 'server_bound',
      fetch_binding_valid: true,
    }],
    sources: [{id: 'S1', url: 'https://example.test/article', fetch_attempts: [fetch]}],
  };
  window.__latestAudit = {
    available: true, sourceFetchesRecorded: true,
    sourceFetches: [fetch],
    pages: {source_fetches: {windowed: true, hasMore: false, items: [fetch]}},
  };
  const exact = sourcePageCoverageModel(state);
  state.evidence[0].fetch_record_id = 'F2';
  const unbound = sourcePageCoverageModel(state);
  return {
    exact: {numerator: exact.numerator, denominator: exact.denominator, ratio: exact.ratio},
    unbound: {numerator: unbound.numerator, denominator: unbound.denominator, ratio: unbound.ratio},
  };
})()
"""
        )
        self.assertEqual(
            result,
            {
                "exact": {"numerator": 1, "denominator": 1, "ratio": 1},
                "unbound": {"numerator": 0, "denominator": 1, "ratio": 0},
            },
        )

    def test_page_coverage_does_not_score_an_incomplete_fetch_audit_window(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const fetch = {
    fetch_record_id: 'F1', source_id: 'S1', status: 'fetched',
    fetch_mode: 'live_provider', binding_status: 'server_bound',
    binding_valid: true,
  };
  window.__latestAudit = {
    available: true, sourceFetchesRecorded: true,
    sourceFetches: [fetch],
    pages: {source_fetches: {windowed: true, hasMore: true, items: [fetch]}},
  };
  const result = sourcePageCoverageModel({sources: [{id: 'S1', fetch_attempts: [fetch]}]});
  return {numerator: result.numerator, denominator: result.denominator, ratio: result.ratio, loaded: result.loadedDenominator};
})()
"""
        )
        self.assertEqual(
            result,
            {"numerator": None, "denominator": None, "ratio": None, "loaded": 1},
        )

    def test_unverified_event_window_uses_local_not_global_numbering(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const events = [{event_id: 'legacy-1'}, {event_id: 'legacy-2'}];
  const model = eventWindowModel({event_window: {
    returned_count: 2,
    total_count: null,
    first_global_index: null,
    last_global_index: null,
    complete: false,
    count_status: 'legacy_unverified',
  }});
  window.__latestEventWindow = model;
  return {
    total: model.total,
    incomplete: model.incomplete,
    range: eventWindowRange(model),
    globalIndex: eventGlobalIndex(events[0], events),
  };
})()
"""
        )
        self.assertIsNone(result["total"])
        self.assertTrue(result["incomplete"])
        self.assertIn("全局范围不可验证", result["range"])
        self.assertIsNone(result["globalIndex"])

    def test_unmeasured_usage_ledger_does_not_render_zero_as_external_usage(self) -> None:
        result = _evaluate_run_script(
            """
(() => usageLedgerModel({
  audit_usage_recorded: true,
  audit_usage: {
    ledger_entry_count: 3,
    usage_status: 'not_applicable',
    pricing_status: 'not_applicable',
    model_calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    estimated_cost_usd: 0,
  },
  counters: {model_calls: 0, input_tokens: 0, output_tokens: 0, estimated_cost_usd: 0},
}))()
"""
        )
        self.assertIsNone(result["modelCalls"])
        self.assertIsNone(result["inputTokens"])
        self.assertIsNone(result["outputTokens"])
        self.assertIsNone(result["estimatedCost"])

    def test_usage_and_pricing_statuses_gate_each_displayed_measurement(self) -> None:
        result = _evaluate_run_script(
            """
(() => ({
  completeUnpriced: usageLedgerModel({
    audit_usage_recorded: true,
    audit_usage: {
      usage_status: 'complete', pricing_status: 'unavailable',
      model_calls: 0, input_tokens: 0, output_tokens: 0, estimated_cost_usd: 0,
    },
  }),
  partial: usageLedgerModel({
    audit_usage_recorded: true,
    audit_usage: {
      usage_status: 'partial', pricing_status: 'partial',
      model_calls: 1, input_tokens: 120, output_tokens: 0, estimated_cost_usd: 0,
    },
  }),
}))()
"""
        )
        complete = result["completeUnpriced"]
        self.assertEqual(complete["modelCalls"], 0)
        self.assertEqual(complete["inputTokens"], 0)
        self.assertEqual(complete["outputTokens"], 0)
        self.assertIsNone(complete["estimatedCost"])
        partial = result["partial"]
        self.assertEqual(partial["modelCalls"], 1)
        self.assertIsNone(partial["inputTokens"])
        self.assertIsNone(partial["outputTokens"])
        self.assertIsNone(partial["estimatedCost"])
        self.assertEqual(partial["knownCostLowerBound"], 0)
        self.assertTrue(partial["costIsLowerBound"])

    def test_usage_cost_keeps_precision_and_exposes_live_settlement_state(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const exact = usageLedgerModel({
    audit_usage_recorded: true,
    audit_usage: {
      ledger_entry_count: 2,
      usage_status: 'complete',
      pricing_status: 'complete',
      model_calls: 1,
      input_tokens: 100,
      output_tokens: 25,
      estimated_cost_usd: 0.000123,
      updated_at: '2026-07-23T12:34:56Z',
      usage_revision: 7,
      pending_model_operations: 1,
      settled_model_operations: 1,
      settled_model_responses: 3,
      latest_entry: {provider: 'deepseek', model_calls: 1, estimated_cost_usd: 0, pricing_status: 'complete'},
      provider_breakdown: [
        {provider: 'deepseek', model_calls: 1, input_tokens: 100, output_tokens: 25, estimated_cost_usd: 0},
      ],
    },
  });
  const partial = usageLedgerModel({
    audit_usage_recorded: true,
    audit_usage: {
      usage_status: 'partial',
      pricing_status: 'partial',
      model_calls: 1,
      input_tokens: 100,
      output_tokens: 25,
      estimated_cost_usd: 0.000123,
      pending_model_operations: 0,
    },
  });
  return {
    exactCost: exact.estimatedCost,
    exactRecordedCost: exact.recordedCost,
    updatedAt: exact.updatedAt,
    pending: exact.pendingModelOperations,
    settled: exact.settledModelOperations,
    settledResponses: exact.settledModelResponses,
    revision: exact.usageRevision,
    latest: usageEntryLabel(exact.latestEntry, {latest:true}),
    breakdown: usageBreakdownLabel(exact),
    formatted: formatEstimatedCost(0.000123),
    partialCost: partial.estimatedCost,
    partialRecordedCost: partial.recordedCost,
    partialKnownCost: partial.knownCostLowerBound,
    partialIsLowerBound: partial.costIsLowerBound,
    partialLabel: formatKnownCost(partial.knownCostLowerBound),
  };
})()
"""
        )
        self.assertEqual(result["exactCost"], 0.000123)
        self.assertEqual(result["exactRecordedCost"], 0.000123)
        self.assertEqual(result["updatedAt"], "2026-07-23T12:34:56Z")
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["settled"], 1)
        self.assertEqual(result["settledResponses"], 3)
        self.assertEqual(result["revision"], 7)
        self.assertEqual(result["latest"], "最近到账：DeepSeek · 1 次模型响应 · 价格表为 $0（调用与 Token 仍计入）")
        self.assertEqual(result["breakdown"], "DeepSeek · 1 次模型响应 · 价格表为 $0（调用与 Token 仍计入）")
        self.assertEqual(result["formatted"], "$0.000123")
        self.assertIsNone(result["partialCost"])
        self.assertEqual(result["partialRecordedCost"], 0.000123)
        self.assertEqual(result["partialKnownCost"], 0.000123)
        self.assertTrue(result["partialIsLowerBound"])
        self.assertEqual(result["partialLabel"], "$0.000123+")

    def test_live_usage_snapshot_wins_over_a_stale_full_audit_snapshot(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  window.__latestUsageRunId = 'live-usage-run';
  window.__latestUsageSnapshot = {
    usage_revision: 4,
    usage_status: 'complete', pricing_status: 'complete',
    model_calls: 4, input_tokens: 400, output_tokens: 40,
    estimated_cost_usd: 0.004, updated_at: '2026-07-24T12:00:04Z',
  };
  const audit = normalizeAudit({
    job: {run_id: 'live-usage-run'},
    usage: {
      usage_revision: 3,
      usage_status: 'complete', pricing_status: 'complete',
      model_calls: 3, input_tokens: 300, output_tokens: 30,
      estimated_cost_usd: 0.003, updated_at: '2026-07-24T12:00:03Z',
    },
    audit: {usage: {
      usage_revision: 2,
      usage_status: 'complete', pricing_status: 'complete',
      model_calls: 2, input_tokens: 200, output_tokens: 20,
      estimated_cost_usd: 0.002, updated_at: '2026-07-24T12:00:02Z',
    }},
  }, {run_id: 'live-usage-run'});
  return {revision: audit.usage.usage_revision, calls: audit.usage.model_calls, recorded: audit.usageRecorded};
})()
"""
        )
        self.assertEqual(result, {"revision": 4, "calls": 4, "recorded": True})

    def test_same_ledger_revision_refreshes_when_the_live_observation_is_newer(self) -> None:
        result = _evaluate_run_script(
            """
(() => ({
  newerObservation: usageSnapshotIsNewer({
    usage_revision: 4, ledger_entry_count: 4, pending_model_operations: 0,
    snapshot_at: '2026-07-24T12:00:02.000Z',
  }, {
    usage_revision: 4, ledger_entry_count: 4, pending_model_operations: 1,
    snapshot_at: '2026-07-24T12:00:01.000Z',
  }),
  staleObservation: usageSnapshotIsNewer({
    usage_revision: 4, ledger_entry_count: 4, pending_model_operations: 1,
    snapshot_at: '2026-07-24T12:00:01.000Z',
  }, {
    usage_revision: 4, ledger_entry_count: 4, pending_model_operations: 0,
    snapshot_at: '2026-07-24T12:00:02.000Z',
  }),
  higherRevision: usageSnapshotIsNewer({
    usage_revision: 5, ledger_entry_count: 5,
    snapshot_at: '2026-07-24T12:00:01.000Z',
  }, {
    usage_revision: 4, ledger_entry_count: 4,
    snapshot_at: '2026-07-24T12:00:03.000Z',
  }),
}))()
"""
        )
        self.assertEqual(
            result,
            {
                "newerObservation": True,
                "staleObservation": False,
                "higherRevision": True,
            },
        )

    def test_live_usage_snapshot_is_small_and_browser_orderable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "live-usage-snapshot")
            snapshot = webapp._live_usage_snapshot(store)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot["usage_revision"], 0)
        self.assertIn("snapshot_at", snapshot)
        self.assertTrue(str(snapshot["snapshot_at"]).endswith("+00:00"))

    def test_durable_audit_and_fetch_mode_contracts_are_visible(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "run.html").read_text(encoding="utf-8")
        script = (root / "web" / "run.js").read_text(encoding="utf-8")
        styles = (root / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertEqual(html.count('class="agent-edge-hit"'), 6)
        self.assertGreaterEqual(html.count("foreignObject"), 7)
        self.assertEqual(html.count('stroke-width="24"'), 7)
        self.assertIn('vector-effect="non-scaling-stroke"', html)
        self.assertIn("data-execution-expand", script)
        self.assertIn("data-timeline-expand", script)
        self.assertIn("auditHandoffRecords(events", script)
        self.assertIn("handoffArtifactRecords(envelope", script)
        self.assertIn("durable source_fetches", script)
        self.assertIn("usageLedgerModel", script)
        self.assertIn("applyLiveUsageSnapshot", script)
        self.assertIn("pollUsage", script)
        self.assertIn("/usage", script)
        self.assertIn("source.addEventListener('usage'", script)
        self.assertIn("formatKnownCost", script)
        self.assertIn('id="costValue"', html)
        self.assertIn('id="tokenMetric"', html)
        self.assertIn('id="costBreakdown"', html)
        self.assertIn("provider_cache", script)
        self.assertIn("offline_corpus", script)
        self.assertIn("durable_operation_replay", script)
        self.assertIn(".agent-edge-hit", styles)
        self.assertIn("stroke-width:24", styles)
        self.assertIn(".receipt-verdict.field_match", styles)

    def test_nonstandard_agent_routes_stay_outside_the_hexagon(self) -> None:
        result = _evaluate_run_script(
            "({opposite: outerAgentRoutePath('planner', 'critic'), cross: outerAgentRoutePath('verifier', 'scout')})"
        )
        self.assertEqual(
            result["opposite"],
            "M350 55 L350 8 A252 252 0 0 1 350 512 L350 465",
        )
        self.assertEqual(
            result["cross"],
            "M172.465 157.5 L131.762 134 A252 252 0 0 1 568.238 134 L527.535 157.5",
        )
        self.assertNotIn("350 260", result["opposite"])

    def test_paginated_audit_does_not_hide_later_role_invocations(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const snapshot = {
    agent_invocations: [
      {invocation_id:'P',agent_id:'planner',status:'succeeded',provenance_status:'store_consistent'},
      {invocation_id:'S',agent_id:'scout',status:'succeeded',provenance_status:'store_consistent'},
      {invocation_id:'C',agent_id:'curator',status:'succeeded',provenance_status:'store_consistent'},
      {invocation_id:'R',agent_id:'critic',status:'succeeded',provenance_status:'store_consistent'},
      {invocation_id:'W',agent_id:'writer',status:'succeeded',provenance_status:'store_consistent'},
      {invocation_id:'V',agent_id:'verifier',status:'failed',provenance_status:'store_consistent'},
    ],
  };
  const audit = {
    invocations: snapshot.agent_invocations.slice(0, 4),
    artifacts: [], handoffs: [], sourceFetches: [], fetchAttemptsBySource: new Map(),
  };
  const merged = mergeDurableAuditIntoState(snapshot, audit);
  return {
    count: merged.agent_invocations.length,
    writer: agentRuntimeEvidence('writer', merged.agent_invocations).status,
    verifier: agentRuntimeEvidence('verifier', merged.agent_invocations).status,
  };
})()
"""
        )
        self.assertEqual(result["count"], 6)
        self.assertEqual(result["writer"], "done")
        self.assertEqual(result["verifier"], "blocked")

    def test_completed_orchestrator_is_presented_as_finalization_not_planning(self) -> None:
        result = _evaluate_run_script(
            """
({
  agent: nodeAgents.emit_finalize,
  contract: agentContracts.orchestrator.name,
  operation: operationName('emit_finalize'),
  summary: runtimeSummary('Projected durable finalize stage', 'emit_finalize', 'output'),
})
"""
        )

        self.assertEqual(result["agent"], "orchestrator")
        self.assertEqual(result["contract"], "研究总控")
        self.assertEqual(result["operation"], "研究总控归档交付")
        self.assertEqual(result["summary"], "最终回答、检查结论与已保存产物清单已归档")

        script = (Path(__file__).resolve().parents[1] / "web" / "run.js").read_text(encoding="utf-8")
        self.assertIn("全流程完成：交付前检查和逐句引用检查均通过", script)
        self.assertNotIn("本轮协作已停止，等待审计结论", script)

    def test_invocation_order_does_not_become_receipt_backed_transition(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const invocations = [
    {agent_id: 'planner', invocation_id: 'I1'},
    {agent_id: 'scout', invocation_id: 'I2'},
  ];
  const raw = invocationTransitionRecords(invocations);
  const noReceipt = receiptBackedAgentTransitions([]);
  const fieldMatch = receiptBackedAgentTransitions([{
    envelope: {producer: 'planner', message_id: 'M1'},
    assessment: {
      status: 'field_match',
      receipt: {consumed_by_agent_id: 'scout'},
    },
  }]);
  const serverValidated = receiptBackedAgentTransitions([{
    envelope: {producer: 'planner', message_id: 'M2'},
    assessment: {
      status: 'server_validated',
      receipt: {consumed_by_agent_id: 'scout'},
    },
  }]);
  return {
    raw: raw.map(item => [item.from, item.to]),
    noReceipt,
    fieldMatch: fieldMatch.map(item => [item.from, item.to, item.status]),
    serverValidated: serverValidated.map(item => [item.from, item.to, item.status]),
  };
})()
"""
        )
        self.assertEqual(result["raw"], [["planner", "scout"]])
        self.assertEqual(result["noReceipt"], [])
        self.assertEqual(result["fieldMatch"], [["planner", "scout", "field_match"]])
        self.assertEqual(result["serverValidated"], [["planner", "scout", "server_validated"]])

    def test_metric_decision_model_exposes_missing_denominators_and_hard_gate_state(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const missing = metricDecisionModel({
    closureScore: 0.6,
    sourceGateAvailable: false,
    pageCoverageRatio: null,
    verificationRatio: 0.5,
    tokens: null,
    gates: gateDefinitions.map((definition, index) => ({
      key: definition.key,
      tone: index === 0 ? 'blocked' : 'passed',
    })),
  });
  const passed = metricDecisionModel({
    closureScore: 1,
    sourceGateAvailable: true,
    pageCoverageRatio: 1,
    verificationRatio: 1,
    tokens: 100,
    gates: gateDefinitions.map(definition => ({key: definition.key, tone: 'passed'})),
  });
  return {missing, passed};
})()
"""
        )

        self.assertEqual(result["missing"]["available"], 2)
        self.assertEqual(
            result["missing"]["missing"],
            ["来源互证", "页面证据覆盖", "Token 合计"],
        )
        self.assertEqual(result["missing"]["gateTone"], "blocked")
        self.assertEqual(result["missing"]["decision"]["tone"], "blocked")
        self.assertEqual(result["passed"]["available"], 5)
        self.assertEqual(result["passed"]["missing"], [])
        self.assertEqual(result["passed"]["decision"]["tone"], "passed")

    def test_durable_only_handoff_is_visible_without_an_event_window(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const envelope = {
    message_id: 'H-durable', run_id: 'R1', trace_id: 'R1',
    producer: 'planner', producer_invocation_id: 'P1', intended_consumer: 'scout',
  };
  const audit = normalizeAudit({audit: {
    handoffs: [{...envelope, envelope}], receipts: [], invocations: [],
  }}, {run_id: 'R1'});
  return auditHandoffRecords([], audit).map(record => ({
    id: record.id,
    durable: Boolean(record.durable),
    event: Boolean(record.event),
    status: handoffReceiptAssessment(record.envelope, [], [], audit).status,
  }));
})()
"""
        )

        self.assertEqual(
            result,
            [{"id": "H-durable", "durable": True, "event": False, "status": "unverified"}],
        )

    def test_strong_handoff_proof_cannot_be_assembled_across_envelopes(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const invocation = {
    invocation_id: 'C1', agent_id: 'scout', operation: 'search_sources',
    status: 'succeeded', consumed_handoff_message_ids: ['H-receipt', 'H-complete'],
    run_id: 'R1', trace_id: 'R1', identity_validation: {status: 'validated'},
  };
  const producerInvocation = {
    invocation_id: 'P1', agent_id: 'planner', operation: 'plan_research',
    status: 'succeeded', run_id: 'R1', trace_id: 'R1',
    handoff_message_ids: ['H-receipt', 'H-gate', 'H-artifact', 'H-complete'],
    output_artifact_ids: ['A-split', 'A-complete'],
    identity_validation: {status: 'validated'},
  };
  const base = {
    run_id: 'R1', trace_id: 'R1', producer: 'planner', producer_invocation_id: 'P1',
    intended_consumer: 'scout',
  };
  const receiptFor = messageId => ({
    message_id: messageId, run_id: 'R1', trace_id: 'R1',
    consumed_by_agent_id: 'scout', consumed_by_invocation_id: 'C1',
    consumed_by_operation: 'search_sources',
    consumed_from_producer_invocation_id: 'P1',
    validation_status: 'server_validated', server_validated: true, valid: true,
    validation: {
      valid: true, status: 'server_validated', checks: {
        run_trace_scope: true, source_message_exists: true,
        source_producer_binding: true, intended_consumer_binding: true,
        consumer_invocation_exists: true, consumer_operation_matches_route: true,
        explicit_consumption_binding: true, active_consumption_fence: true,
        single_consumer: true, timestamp_order: true,
      },
    },
  });
  const artifactFor = (artifactId, messageId, checksum) => ({
    artifact_id: artifactId, kind: 'research/test', revision: 1, checksum,
    metadata_hash: 'c'.repeat(64), producer: 'planner',
    producer_invocation_id: 'P1', handoff_message_id: messageId,
    content_uri: `artifacts/${artifactId}.json`, byte_length: 12,
    media_type: 'application/json', canonicalization: 'json-sort-keys-utf8-v1',
  });
  const manifestFor = artifact => ({
    ...artifact, run_id: 'R1', manifest_valid: true, files_present: true,
    passable: true, integrity_status: 'verified', status: 'committed',
  });
  const splitArtifact = artifactFor('A-split', 'H-artifact', 'a'.repeat(64));
  const splitAudit = normalizeAudit({audit: {
    invocations: [producerInvocation, invocation],
    handoffs: [
      {...base, message_id: 'H-receipt', envelope: {...base, message_id: 'H-receipt'}},
      {...base, message_id: 'H-gate', envelope: {...base, message_id: 'H-gate', quality_gate: {status: 'passed'}}},
      {...base, message_id: 'H-artifact', envelope: {...base, message_id: 'H-artifact', output_artifacts: [splitArtifact]}},
    ],
    receipts: [receiptFor('H-receipt')],
    artifacts: [manifestFor(splitArtifact)],
  }}, {run_id: 'R1'});
  const split = auditHandoffRecords([], splitAudit).map(record => ({
    ...record,
    assessment: handoffReceiptAssessment(record.envelope, [], splitAudit.invocations, splitAudit),
  })).map(record => handoffProofModel(record, 'planner', 'scout', splitAudit));
  const completeArtifact = artifactFor('A-complete', 'H-complete', 'b'.repeat(64));
  const completeEnvelope = {
    ...base, message_id: 'H-complete', quality_gate: {status: 'passed'},
    output_artifacts: [completeArtifact],
  };
  const completeAudit = normalizeAudit({audit: {
    invocations: [producerInvocation, invocation],
    handoffs: [{...completeEnvelope, envelope: completeEnvelope}],
    receipts: [receiptFor('H-complete')],
    artifacts: [manifestFor(completeArtifact)],
  }}, {run_id: 'R1'});
  const completeRecord = auditHandoffRecords([], completeAudit)[0];
  completeRecord.assessment = handoffReceiptAssessment(
    completeRecord.envelope, [], completeAudit.invocations, completeAudit,
  );
  const complete = handoffProofModel(completeRecord, 'planner', 'scout', completeAudit);
  return {
    splitStrong: split.filter(item => item.strong).length,
    splitParts: split.map(item => [item.gatePassed, item.hasChecksummedArtifact, item.assessment.status]),
    complete: [complete.strong, complete.gatePassed, complete.hasChecksummedArtifact, complete.assessment.status],
  };
})()
"""
        )

        self.assertEqual(result["splitStrong"], 0)
        self.assertIn([False, False, "server_validated"], result["splitParts"])
        self.assertIn([True, False, "unverified"], result["splitParts"])
        self.assertIn([False, True, "unverified"], result["splitParts"])
        self.assertEqual(result["complete"], [True, True, True, "server_validated"])

    def test_nonsequential_receipts_are_repairs_while_no_receipt_stays_order_only(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const invocations = [
    {agent_id: 'planner', invocation_id: 'I1'},
    {
      agent_id: 'scout', invocation_id: 'I2', status: 'succeeded',
      consumed_handoff_message_ids: ['H-repair'],
      identity_validation: {status: 'validated'},
    },
  ];
  const empty = networkEdgeLedgerModel(invocations, [], {available: false});
  const repairEvent = {payload: {handoff_envelope: {
    message_id: 'H-repair', producer: 'critic', intended_consumer: 'scout',
    receipt: {
      message_id: 'H-repair', consumed_by_agent_id: 'scout',
      consumed_by_invocation_id: 'I2', valid: true,
    },
  }}};
  const repair = networkEdgeLedgerModel(invocations, [repairEvent], {available: false});
  return {
    emptySequence: empty.sequence.map(item => item.kind),
    emptyRouteTone: empty.routes[0].tone,
    repairs: repair.repairs.map(item => [item.from, item.to, item.assessment.status]),
  };
})()
"""
        )

        self.assertEqual(result["emptySequence"], ["order"])
        self.assertEqual(result["emptyRouteTone"], "order")
        self.assertEqual(result["repairs"], [["critic", "scout", "field_match"]])

    def test_source_read_modes_do_not_collapse_replay_or_evidence_only_into_live(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const base = {status: 'fetched', fetch_invocation_id: 'I1', fetch_binding_status: 'server_bound', binding_valid: true};
  return [
    sourceReadAssessment({...base, fetch_mode: 'live_provider'}).mode,
    sourceReadAssessment({...base, fetch_mode: 'provider_cache'}).mode,
    sourceReadAssessment({...base, fetch_mode: 'offline_corpus'}).mode,
    sourceReadAssessment({...base, fetch_mode: 'durable_operation_replay', fetch_execution_mode: 'replayed'}).mode,
    sourceReadAssessment({status: 'evidence_only', fetch_mode: 'evidence_only'}).mode,
    sourceReadAssessment({...base, fetch_mode: 'live_provider', fetch_binding_status: 'unbound'}).mode,
  ];
})()
"""
        )
        self.assertEqual(
            result,
            ["live", "provider_cache", "offline_corpus", "replayed", "evidence_only", "fetched_unbound"],
        )

    def test_missing_fetch_binding_status_cannot_be_promoted_by_invocation_id(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const normalized = normalizeAuditSourceFetch({
    source_id: 'S1', invocation_id: 'I1', operation_key: 'O1', status: 'fetched',
  });
  return {
    binding: normalized.binding_status,
    mode: sourceReadAssessment({
      status: 'fetched',
      fetch_invocation_id: normalized.invocation_id,
      fetch_binding_status: normalized.binding_status,
      fetch_mode: 'live_provider',
    }).mode,
  };
})()
"""
        )
        self.assertEqual(result["binding"], "legacy_unverified")
        self.assertEqual(result["mode"], "fetched_unbound")

    def test_server_bound_without_integrity_boolean_is_not_read_as_verified(self) -> None:
        result = _evaluate_run_script(
            """
(() => sourceReadAssessment({
  status: 'fetched',
  fetch_invocation_id: 'I1',
  fetch_binding_status: 'server_bound',
  fetch_mode: 'live_provider',
}))()
"""
        )
        self.assertEqual(result["mode"], "fetched_unbound")

    def test_snapshot_capability_is_bound_to_one_exact_fetch_attempt(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const noSnapshot = {
    fetch_record_id: 'F1', source_id: 'S1', invocation_id: 'I1',
    recorded_at: '2026-07-20T10:00:00Z',
    status: 'fetched', fetch_mode: 'live_provider',
    binding_status: 'server_bound', binding_valid: true,
    snapshot_available: false, snapshot_sha256: '',
  };
  const withSnapshot = {
    fetch_record_id: 'F2', source_id: 'S1', invocation_id: 'I2',
    recorded_at: '2026-07-20T10:01:00Z',
    status: 'fetched', fetch_mode: 'live_provider',
    binding_status: 'server_bound', binding_valid: true,
    snapshot_available: true, snapshot_sha256: 'b'.repeat(64),
  };
  const source = {
    id: 'S1', url: 'https://example.test/article', final_url: 'https://example.test/article',
    fetch_attempts: [noSnapshot, withSnapshot],
  };
  const state = {
    sources: [source],
    evidence: [
      {id: 'E1', source_id: 'S1', source_url: source.url, fetch_record_id: 'F1'},
      {id: 'E2', source_id: 'S1', source_url: source.url, fetch_record_id: 'F2'},
    ],
  };
  const firstTrace = exactEvidenceFetchBinding(state.evidence[0], state);
  const secondTrace = exactEvidenceFetchBinding(state.evidence[1], state);
  const first = exactSnapshotCapability(firstTrace);
  const second = exactSnapshotCapability(secondTrace);
  const sourceCapability = sourceSnapshotCapability(source, state.evidence, state);
  return {
    first: {available: first.available, id: first.fetch_record_id, label: first.label},
    second: {available: second.available, id: second.fetch_record_id},
    source: {
      available: sourceCapability.available,
      id: sourceCapability.fetch_record_id,
      evidence: sourceCapability.evidence.map(item => item.id),
    },
    firstExactEvidence: evidenceForExactFetch(noSnapshot, state.evidence, state).map(item => item.id),
    secondExactEvidence: evidenceForExactFetch(withSnapshot, state.evidence, state).map(item => item.id),
  };
})()
"""
        )

        self.assertFalse(result["first"]["available"])
        self.assertEqual(result["first"]["id"], "F1")
        self.assertIn("该 attempt 没有保存快照", result["first"]["label"])
        self.assertEqual(result["second"], {"available": True, "id": "F2"})
        self.assertEqual(
            result["source"],
            {"available": True, "id": "F2", "evidence": ["E2"]},
        )
        self.assertEqual(result["firstExactEvidence"], ["E1"])
        self.assertEqual(result["secondExactEvidence"], ["E2"])

    def test_latest_fetch_without_snapshot_cannot_borrow_an_older_source_snapshot(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const older = {
    fetch_record_id: 'F-old', source_id: 'S1', invocation_id: 'I-old',
    recorded_at: '2026-07-20T10:00:00Z',
    status: 'fetched', binding_status: 'server_bound', binding_valid: true,
    snapshot_available: true, snapshot_sha256: 'a'.repeat(64),
  };
  const latest = {
    fetch_record_id: 'F-latest', source_id: 'S1', invocation_id: 'I-latest',
    recorded_at: '2026-07-20T10:01:00Z',
    status: 'fetched', binding_status: 'server_bound', binding_valid: true,
    snapshot_available: false, snapshot_sha256: '',
  };
  const source = {
    id: 'S1', snapshot_available: true, snapshot_sha256: 'a'.repeat(64),
    fetch_attempts: [older, latest],
  };
  const capability = sourceSnapshotCapability(source, [], {sources: [source], evidence: []});
  return {available: capability.available, id: capability.fetch_record_id, label: capability.label};
})()
"""
        )

        self.assertFalse(result["available"])
        self.assertEqual(result["id"], "F-latest")
        self.assertIn("该 attempt 没有保存快照", result["label"])

    def test_declared_slot_pass_count_without_reproducible_slot_audits_is_not_verified(self) -> None:
        result = _evaluate_run_script(
            """
(() => requiredSlotProgressModel({
  plan: {slots: [
    {id: 'slot-a', description: 'A', required: true},
    {id: 'slot-b', description: 'B', required: true},
  ]},
  closure: {required_slots: 2, passed_slots: 2},
}))()
"""
        )
        self.assertIsNone(result["passed"])
        self.assertEqual(result["declaredPassed"], 2)
        self.assertEqual(result["passedSource"], "backend_declaration_only")

    def test_unknown_slot_evidence_reference_is_unverifiable(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const audit = {
    slot_id: 'slot-a', passed: true,
    supporting_evidence_ids: ['ghost-evidence'],
    contradicting_evidence_ids: [],
    source_gate_passed: true, exact_quote_gate_passed: true,
    contradiction_checked: true, conflict_gate_passed: true,
  };
  const state = {
    plan: {slots: [{id: 'slot-a', description: 'A', required: true}]},
    closure: {required_slots: 1, slot_audits: [audit]}, evidence: [],
  };
  const progress = requiredSlotProgressModel(state);
  const closure = closureEvidenceAuditModel(state, true);
  return {progress, closure: {available: closure.available, invalid: [...closure.invalidEvidenceIds]}, gate: gateConsoleModel(state)[0]};
})()
"""
        )
        self.assertIsNone(result["progress"]["passed"])
        self.assertFalse(result["closure"]["available"])
        self.assertEqual(result["closure"]["invalid"], ["ghost-evidence"])
        self.assertEqual(result["gate"]["tone"], "unverifiable")

    def test_missing_relevance_threshold_does_not_reclassify_historical_evidence(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const item = {id: 'E1', slot_id: 'slot-1', stance: 'supports', slot_relevance_score: 0.1};
  const historical = {methodology: {}, closure: {}};
  const current = {methodology: {admission_thresholds: {slot_relevance: 0.45}}, closure: {}};
  return {
    missing: relevanceAdmissionThreshold(historical),
    historicalRole: evidenceEffectiveRole(item, historical),
    currentRole: evidenceEffectiveRole(item, current),
  };
})()
"""
        )
        self.assertIsNone(result["missing"])
        self.assertEqual(result["historicalRole"]["kind"], "supports")
        self.assertIn("未使用当前默认值", result["historicalRole"]["reason"])
        self.assertEqual(result["currentRole"]["kind"], "excluded")

    def test_event_receipt_cannot_be_upgraded_but_durable_receipt_has_four_states(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const invocation = {
    invocation_id: 'C1', agent_id: 'curator', operation: 'ingest_evidence',
    status: 'succeeded', consumed_handoff_message_ids: ['H1'],
    run_id: 'R1', trace_id: 'R1', identity_validation: {status: 'validated'},
  };
  const producerInvocation = {
    invocation_id: 'P1', agent_id: 'scout', operation: 'search_sources',
    status: 'succeeded', handoff_message_ids: ['H1'],
    run_id: 'R1', trace_id: 'R1', identity_validation: {status: 'validated'},
  };
  const envelope = {
    message_id: 'H1', run_id: 'R1', trace_id: 'R1', producer: 'scout',
    producer_invocation_id: 'P1', intended_consumer: 'curator',
  };
  const eventOnly = handoffReceiptAssessment(
    {...envelope, receipt: {message_id: 'H1', consumed_by_agent_id: 'curator', consumed_by_invocation_id: 'C1', valid: true}},
    [], [invocation], {available: false},
  ).status;
  const statuses = ['server_validated', 'field_match', 'unverified', 'invalid'].map(status => {
    const receipt = {
      message_id: 'H1', run_id: 'R1', trace_id: 'R1',
      consumed_by_agent_id: 'curator', consumed_by_invocation_id: 'C1',
      consumed_by_operation: 'ingest_evidence',
      consumed_from_producer_invocation_id: 'P1',
      validation_status: status, server_validated: status === 'server_validated',
      valid: status !== 'invalid',
      validation: status === 'server_validated' ? {
        valid: true, status: 'server_validated', checks: {
          run_trace_scope: true, source_message_exists: true,
          source_producer_binding: true, intended_consumer_binding: true,
          consumer_invocation_exists: true, consumer_operation_matches_route: true,
          explicit_consumption_binding: true, active_consumption_fence: true,
          single_consumer: true, timestamp_order: true,
        },
      } : {},
    };
    const audit = normalizeAudit({audit: {
      invocations: [producerInvocation, invocation], handoffs: [{...envelope, envelope}],
      receipts: status === 'unverified' ? [] : [receipt],
    }}, {});
    const current = audit.handoffByMessage.get('H1').envelope;
    return handoffReceiptAssessment(current, [], audit.invocations, audit).status;
  });
  return {eventOnly, statuses};
})()
"""
        )
        self.assertEqual(result["eventOnly"], "field_match")
        self.assertEqual(result["statuses"], ["server_validated", "field_match", "unverified", "invalid"])

    def test_receipt_rejection_and_short_manifest_digest_fail_closed(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const envelope = {
    message_id: 'H1', run_id: 'R1', trace_id: 'R1', producer: 'planner',
    producer_invocation_id: 'P1', intended_consumer: 'scout',
    quality_gate: {status: 'passed'},
  };
  const valid = {
    message_id: 'H1', consumed_by_invocation_id: 'C1',
    consumed_by_agent_id: 'scout', validation_status: 'server_validated',
    server_validated: true, valid: true,
  };
  const rejected = {
    message_id: 'H1', consumed_by_invocation_id: 'C1',
    consumed_by_agent_id: 'scout', validation_status: 'invalid',
    valid: false, validation_error: 'duplicate consumer',
  };
  const audit = normalizeAudit({audit: {
    handoffs: [{...envelope, envelope}], receipts: [valid, rejected], invocations: [],
  }}, {run_id: 'R1'});
  const receiptStatus = handoffReceiptAssessment(
    audit.handoffByMessage.get('H1').envelope, [], audit.invocations, audit,
  ).status;
  const artifact = {
    artifact_id: 'A1', checksum: 'abc', metadata_hash: 'd'.repeat(64),
    producer: 'planner', producer_invocation_id: 'P1', handoff_message_id: 'H1',
    content_uri: 'artifacts/A1.json', byte_length: 2,
    media_type: 'application/json', canonicalization: 'json-sort-keys-utf8-v1',
  };
  envelope.output_artifacts = [artifact];
  const producerInvocation = {
    invocation_id: 'P1', agent_id: 'planner', status: 'succeeded',
    identity_validation: {status: 'validated'}, output_artifact_ids: ['A1'],
  };
  const proof = handoffProofModel({
    envelope,
    assessment: {
      status: 'server_validated', receipt: {
        consumed_by_agent_id: 'scout', consumed_by_invocation_id: 'C1',
      },
      producerInvocation,
    },
  }, 'planner', 'scout', {
    available: true,
    artifacts: [{
      ...artifact, run_id: 'R1', manifest_valid: true, files_present: true,
      passable: true, integrity_status: 'verified', status: 'committed',
    }],
  });
  return {
    receiptStatus,
    strong: proof.strong,
    manifestComplete: proof.manifestComplete,
    reasons: proof.artifactProofs[0].reasons,
  };
})()
"""
        )

        self.assertEqual(result["receiptStatus"], "invalid")
        self.assertFalse(result["strong"])
        self.assertFalse(result["manifestComplete"])
        self.assertTrue(any("不是完整的 SHA-256 值" in reason for reason in result["reasons"]))

    def test_explicit_source_url_and_snapshot_identity_conflicts_are_rejected(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const fetch = {
    fetch_record_id: 'F1', source_id: 'S1', invocation_id: 'I1',
    recorded_at: '2026-07-20T10:00:00Z', binding_status: 'server_bound',
    binding_valid: true, snapshot_available: true, snapshot_sha256: 'a'.repeat(64),
  };
  const source = {
    id: 'S1', url: 'https://example.test/right',
    final_url: 'https://example.test/right', fetch_attempts: [fetch],
  };
  const evidence = {
    id: 'E1', source_id: 'S1', source_url: 'https://example.test/wrong',
    fetch_record_id: 'F1',
  };
  const trace = exactEvidenceFetchBinding(evidence, {sources: [source], evidence: [evidence]});
  return {
    sourceMatches: sourceMatchesEvidence(source, evidence),
    traceStatus: trace.status,
    wrongSource: snapshotIdentityAssessment(
      {source_id: 'S2', fetch_record_id: 'F1'}, source, fetch,
    ).valid,
    wrongFetch: snapshotIdentityAssessment(
      {source_id: 'S1', fetch_record_id: 'F2'}, source, fetch,
    ).valid,
    exact: snapshotIdentityAssessment(
      {source_id: 'S1', fetch_record_id: 'F1'}, source, fetch,
    ).valid,
  };
})()
"""
        )

        self.assertFalse(result["sourceMatches"])
        self.assertEqual(result["traceStatus"], "invalid")
        self.assertFalse(result["wrongSource"])
        self.assertFalse(result["wrongFetch"])
        self.assertTrue(result["exact"])

    def test_missing_slot_confidence_is_not_drawn_as_zero_progress(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        innerHTML: '',
        textContent: '',
        classList: {add() {}, remove() {}},
      });
    }
    return nodes.get(id);
  };
  document.getElementById = node;
  renderSlots([{id: 'slot-1', description: '目标字段'}]);
  return node('slots').innerHTML;
})()
"""
        )

        self.assertIn('data-unavailable="true"', result)
        self.assertIn("当前不可计算", result)
        self.assertNotIn("width:0%", result)

    def test_gate_summary_uses_required_slot_ids_and_marks_missing_audit(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const passedAudit = {
    slot_id: 'required-a',
    passed: true,
    supporting_evidence_ids: ['E45ABC'],
    source_gate_passed: true,
    exact_quote_gate_passed: true,
    contradiction_checked: true,
    conflict_gate_passed: true,
  };
  const optionalAudit = {...passedAudit, slot_id: 'optional-c'};
  const state = {
    plan: {slots: [
      {id: 'required-a', description: 'A', required: true},
      {id: 'required-b', description: 'B', required: true},
      {id: 'optional-c', description: 'C', required: false},
    ]},
    closure: {required_slots: 2, slot_audits: [passedAudit, optionalAudit]},
    evidence: [{id: 'E45ABC'}],
  };
  return {
    gates: gateConsoleModel(state).map(item => ({
      value: item.value,
      tone: item.tone,
      targetSlotId: item.targetSlotId,
    })),
    rows: slotAuditRows(state, true).map(row => ({
      slotId: row.slotId,
      required: row.required,
      present: row.present,
    })),
  };
})()
"""
        )

        self.assertEqual({item["value"] for item in result["gates"]}, {"1/2"})
        self.assertEqual(
            {item["tone"] for item in result["gates"]}, {"unverifiable"}
        )
        self.assertEqual(
            {item["targetSlotId"] for item in result["gates"]},
            {"required-b"},
        )
        self.assertEqual(
            result["rows"],
            [
                {"slotId": "required-a", "required": True, "present": True},
                {"slotId": "required-b", "required": True, "present": False},
                {"slotId": "optional-c", "required": False, "present": True},
            ],
        )

    def test_legacy_gate_fields_render_as_unverifiable_without_throwing(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {innerHTML: '', querySelectorAll: () => []});
    return nodes.get(id);
  };
  document.getElementById = node;
  document.querySelectorAll = () => [];
  const state = {
    plan: {slots: [{id: 'legacy-slot', description: 'Legacy slot', required: true}]},
    closure: {slot_audits: [{slot_id: 'legacy-slot', description: 'Legacy slot'}]},
    evidence: [{id: 'E-evidence', slot_id: 'legacy-slot', stance: 'supports'}],
  };
  window.__latestState = state;
  renderBreakdown(state.closure, {});
  renderSlotGateAudit(state.closure, undefined);
  return {
    breakdown: node('closureBreakdown').innerHTML,
    audit: node('slotGateAudit').innerHTML,
    gates: gateConsoleModel(state).map(item => item.tone),
  };
})()
"""
        )

        self.assertEqual(set(result["gates"]), {"unverifiable"})
        self.assertIn("历史字段未记录", result["breakdown"])
        self.assertIn("检查记录不完整，暂不能判断", result["audit"])
        self.assertIn("不可验证", result["audit"])

    def test_citations_match_current_evidence_ids_and_unknown_ids_stay_visible(self) -> None:
        result = _evaluate_run_script(
            """
(() => {
  const evidence = [
    {id: 'E45ABC', source_title: 'Uppercase source', source_url: 'https://a.test'},
    {id: 'E-evidence', source_title: 'Hyphen source', source_url: 'https://b.test'},
  ];
  const answer = '甲 [E45ABC]。乙 [E-evidence]。丙 [E-missing]。';
  const richAnswer = formatCitations(
    '## 对问题的直接回答\\n\\n**核心结论**：2D 外观与 `f_pose` 一起使用 [E45ABC]。\\n\\n1. 先形成外观特征\\n2. 再融合结构特征\\n\\n- 可直接回看证据',
    evidence,
  );
  const summary = buildAnswerSummary(
    '这句没有引用。完整的核查结论 [E-evidence]。后续说明。',
    evidence,
  );
  const directSummary = buildAnswerSummary(
    '## 当前可交付回答\\n\\n说明文字。\\n\\n## 对问题的直接回答\\n\\n**简短结论**：3D 结构补充 2D 外观 [E45ABC]。\\n\\n## 仍待补充的核验\\n\\n不应进入顶部。',
    evidence,
  );
  const latestSummary = buildAnswerSummary(
    'ReID 是跨摄像机识别同一目标 [E45ABC]。\\n\\n近年第一条主线是 Transformer 与局部结构 [E-evidence]。\\n\\n鲁棒性评测开始覆盖真实退化 [E-evidence]。',
    evidence,
    'ReID领域最新进展',
  );
  return {
    formatted: formatCitations(answer, evidence),
    richAnswer,
    ids: overviewCitationItems(answer, evidence).map(item => ({id: item.id, known: Boolean(item.item)})),
    summary,
    directSummary,
    latestSummary,
  };
})()
"""
        )

        self.assertIn('data-evidence="E45ABC"', result["formatted"])
        self.assertIn('data-evidence="E-evidence"', result["formatted"])
        self.assertIn("[E-missing]", result["formatted"])
        self.assertIn("未知 ID", result["formatted"])
        self.assertNotIn('href="#E-missing"', result["formatted"])
        self.assertIn("<h4>对问题的直接回答</h4>", result["richAnswer"])
        self.assertIn("<strong>核心结论</strong>", result["richAnswer"])
        self.assertIn("<code>f_pose</code>", result["richAnswer"])
        self.assertIn("<ol><li>先形成外观特征</li><li>再融合结构特征</li></ol>", result["richAnswer"])
        self.assertIn("<ul><li>可直接回看证据</li></ul>", result["richAnswer"])
        self.assertNotIn("## 对问题的直接回答", result["richAnswer"])
        self.assertEqual(
            result["ids"],
            [
                {"id": "E45ABC", "known": True},
                {"id": "E-evidence", "known": True},
                {"id": "E-missing", "known": False},
            ],
        )
        self.assertEqual(
            result["summary"]["text"],
            "这句没有引用。完整的核查结论 [E-evidence]。后续说明。",
        )
        self.assertIn("可核查引用", result["summary"]["label"])
        self.assertIn("3D 结构补充 2D 外观", result["directSummary"]["text"])
        self.assertNotIn("不应进入顶部", result["directSummary"]["text"])
        self.assertIn("Transformer 与局部结构", result["latestSummary"]["text"])
        self.assertNotIn("跨摄像机识别同一目标", result["latestSummary"]["text"])


class PublicStateTest(unittest.TestCase):
    def test_public_state_redacts_pending_page_text_and_keeps_frontend_state(self) -> None:
        private_page_text = "PRIVATE PENDING PAGE BODY"
        state = ResearchState(
            run_id="public-state-run",
            question="What should the frontend display?",
            status="drafting",
            next_node="verify",
            pending_pages=[
                Page(
                    url="https://example.com/private",
                    title="Pending page",
                    text=private_page_text,
                )
            ],
            sources=[
                SourceRecord(
                    id="S-source",
                    url="https://example.com/source",
                    title="Public source",
                    source_type="reference",
                    snippet="Source summary for the frontend",
                    status="fetched",
                )
            ],
            evidence=[
                Evidence(
                    id="E-evidence",
                    subgoal_id="SG1",
                    slot_id="slot-1",
                    claim="A supported public claim",
                    quote="A short public evidence quote",
                    source_url="https://example.com/source",
                    source_title="Public source",
                    stance="supports",
                    reliability=0.9,
                    extraction_confidence=0.8,
                    content_hash="content-hash",
                    source_cluster_id="example.com",
                    source_id="S-source",
                )
            ],
        )

        public_state = webapp._public_state_dict(state)

        self.assertNotIn(private_page_text, json.dumps(public_state))
        for page in public_state.get("pending_pages", []):
            self.assertNotIn("text", page)
        self.assertEqual(public_state["run_id"], "public-state-run")
        self.assertEqual(public_state["question"], state.question)
        self.assertEqual(public_state["status"], "drafting")
        self.assertEqual(public_state["next_node"], "verify")
        self.assertEqual(public_state["sources"][0]["id"], "S-source")
        self.assertEqual(
            public_state["sources"][0]["snippet"],
            "Source summary for the frontend",
        )
        self.assertEqual(public_state["evidence"][0]["id"], "E-evidence")
        self.assertEqual(
            public_state["evidence"][0]["quote"],
            "A short public evidence quote",
        )

    def test_durable_terminal_state_overrides_stale_in_memory_job(self) -> None:
        state = ResearchState(
            run_id="terminal-wins",
            question="Which state wins?",
            status="completed",
            next_node="done",
        )
        projected = webapp._durable_job_view(
            {"status": "running", "error": "stale worker"}, state
        )

        self.assertEqual(projected["status"], "completed")
        self.assertEqual(projected["error"], "")


class RequestMetadataAllowedTest(unittest.TestCase):
    def test_exact_loopback_origin_and_port_are_allowed(self) -> None:
        allowed_requests = (
            ("localhost:8000", "http://localhost:8000", "same-origin", 8000),
            ("127.0.0.1:4312", "http://127.0.0.1:4312", "same-origin", 4312),
        )

        for host, origin, sec_fetch_site, server_port in allowed_requests:
            with self.subTest(host=host, origin=origin):
                self.assertTrue(
                    webapp._request_metadata_allowed(
                        host, origin, sec_fetch_site, server_port
                    )
                )

    def test_mismatched_origin_port_is_rejected(self) -> None:
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000",
                "http://localhost:8001",
                "same-origin",
                8000,
            )
        )

    def test_different_loopback_name_is_not_exact_same_origin(self) -> None:
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000",
                "http://127.0.0.1:8000",
                "same-origin",
                8000,
            )
        )

    def test_cross_site_fetch_is_rejected(self) -> None:
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000",
                "http://localhost:8000",
                "cross-site",
                8000,
            )
        )

    def test_non_loopback_host_is_rejected(self) -> None:
        self.assertFalse(
            webapp._request_metadata_allowed(
                "example.com:8000",
                "http://example.com:8000",
                "same-origin",
                8000,
            )
        )

    def test_originless_client_requires_loopback_host_without_cross_site_marker(self) -> None:
        self.assertTrue(
            webapp._request_metadata_allowed("localhost:8000", None, None, 8000)
        )
        self.assertTrue(
            webapp._request_metadata_allowed("127.0.0.1:8000", None, None, 8000)
        )
        self.assertFalse(
            webapp._request_metadata_allowed(
                "localhost:8000", None, "cross-site", 8000
            )
        )
        self.assertFalse(
            webapp._request_metadata_allowed("example.com:8000", None, None, 8000)
        )


class SystemContractTest(unittest.TestCase):
    @patch(
        "deep_research.system_contract._package_version",
        side_effect=lambda name: {
            "a2a-sdk": "1.1.1",
            "ag-ui-protocol": "0.1.19",
            "mcp": "1.28.1",
        }[name],
    )
    def test_protocol_claims_include_evidence_and_limitations(
        self,
        _package_version: object,
    ) -> None:
        contract = system_contract()
        boundaries = {item["id"]: item for item in contract["boundaries"]}
        packages = {
            item["name"]: item
            for item in contract["official_verification"]["packages"]
        }

        self.assertEqual(
            set(boundaries),
            {"internal-handoff", "ag-ui", "mcp", "a2a"},
        )
        self.assertEqual(boundaries["ag-ui"]["maturity"], "validated-adapter")
        self.assertEqual(boundaries["ag-ui"]["adapter_version_status"], "validated-adapter")
        self.assertEqual(boundaries["ag-ui"]["conformance_status"], "validated-adapter")
        self.assertEqual(boundaries["mcp"]["maturity"], "validated-adapter")
        self.assertEqual(boundaries["a2a"]["maturity"], "validated-adapter")
        self.assertEqual(packages["AG-UI"]["version_status"], "current-runtime")
        self.assertEqual(packages["AG-UI"]["verification_status"], "validated-adapter")
        self.assertEqual(packages["AG-UI"]["receipt_checked_at"], "2026-07-20")
        self.assertIn("SQLite TaskStore", boundaries["a2a"]["implementation"])
        self.assertTrue(boundaries["a2a"]["verified_by"])
        self.assertTrue(boundaries["a2a"]["limitations"])
        self.assertTrue(
            any("conformance" in item for item in boundaries["ag-ui"]["limitations"])
        )
        self.assertIn("TypeScript", boundaries["ag-ui"]["title"])
        self.assertTrue(
            any("state" in item for item in boundaries["ag-ui"]["limitations"])
        )
        self.assertIn(
            "不宣称为六个网络远程 Agent",
            boundaries["internal-handoff"]["limitations"][0],
        )
        self.assertEqual(contract["official_verification"]["checked_at"], "2026-07-20")
        self.assertEqual(
            contract["official_verification"]["record_url"],
            "/api/protocol-verification",
        )
        self.assertEqual(contract["official_verification"]["receipt_checked_at"], "2026-07-20")
        self.assertTrue(webapp.PROTOCOL_VERIFICATION_PATH.is_file())
        reviews = contract["selection_reviews"]
        self.assertGreaterEqual(len(reviews), 7)
        self.assertEqual(len({item["id"] for item in reviews}), len(reviews))
        for review in reviews:
            for key in ("decision", "reviewer_role", "challenge", "response", "evidence", "revisit_when"):
                self.assertTrue(review[key], f"missing selection review field: {review['id']} / {key}")

    @patch(
        "deep_research.system_contract._package_version",
        side_effect=lambda name: {
            "a2a-sdk": "1.2.0",
            "ag-ui-protocol": "0.1.20",
            "mcp": "1.29.0",
        }[name],
    )
    def test_protocol_claims_fail_closed_when_installed_versions_drift(
        self,
        _package_version: object,
    ) -> None:
        contract = system_contract()
        packages = {
            item["name"]: item
            for item in contract["official_verification"]["packages"]
        }
        boundaries = {item["id"]: item for item in contract["boundaries"]}

        for package in packages.values():
            self.assertEqual(package["verification_status"], "adapter-blocked")
            self.assertIn("版本不一致", package["decision"])
        self.assertEqual(boundaries["a2a"]["maturity"], "adapter-blocked")
        self.assertEqual(boundaries["mcp"]["maturity"], "adapter-blocked")
        self.assertEqual(boundaries["ag-ui"]["maturity"], "adapter-blocked")
        for boundary_id in ("a2a", "mcp", "ag-ui"):
            self.assertTrue(
                any("版本不一致" in item for item in boundaries[boundary_id]["limitations"])
            )

    @patch(
        "deep_research.system_contract._package_version",
        side_effect=lambda name: {
            "a2a-sdk": "1.1.1",
            "ag-ui-protocol": "0.1.19",
            "mcp": "1.28.1",
        }[name],
    )
    def test_protocol_claims_accept_only_a_fresh_recomputable_receipt(
        self,
        _package_version: object,
    ) -> None:
        evidence = b"pinned protocol harness passed\n"
        digest = hashlib.sha256(evidence).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "harness.log"
            evidence_path.write_bytes(evidence)
            receipt_path = root / "receipts.json"
            receipt_path.write_text(
                json.dumps(
                    {
                        "schema_version": "protocol-validation-receipt-v1",
                        "receipts": {
                            "A2A": {
                                "sdk_version": "1.1.1",
                                "protocol_version": "1.0",
                                "passed": True,
                                "command": "python -m unittest tests.test_protocols.A2AGatewayTest",
                                "checked_at": date.today().isoformat(),
                                "evidence_path": str(evidence_path),
                                "evidence_sha256": digest,
                            },
                            "MCP": {
                                "sdk_version": "1.28.1",
                                "protocol_version": "2025-11-25",
                                "passed": True,
                                "command": "python -m unittest tests.test_protocols.McpServerTest",
                                "checked_at": date.today().isoformat(),
                                "evidence_path": str(evidence_path),
                                "evidence_sha256": digest,
                            },
                            "AG-UI": {
                                "sdk_version": "0.1.19",
                                "typescript_version": "0.0.57",
                                "passed": True,
                                "command": "npm test --prefix conformance/agui",
                                "checked_at": date.today().isoformat(),
                                "evidence_path": str(evidence_path),
                                "evidence_sha256": digest,
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"DEEP_RESEARCH_PROTOCOL_RECEIPTS": str(receipt_path)},
                clear=False,
            ):
                contract = system_contract()
        packages = {item["name"]: item for item in contract["official_verification"]["packages"]}
        boundaries = {item["id"]: item for item in contract["boundaries"]}
        for name in ("A2A", "MCP", "AG-UI"):
            self.assertEqual(packages[name]["verification_status"], "validated-adapter")
            self.assertEqual(packages[name]["receipt_checked_at"], date.today().isoformat())
        self.assertEqual(boundaries["a2a"]["maturity"], "validated-adapter")
        self.assertEqual(boundaries["mcp"]["maturity"], "validated-adapter")
        self.assertEqual(boundaries["ag-ui"]["maturity"], "validated-adapter")
        self.assertEqual(contract["official_verification"]["receipt_checked_at"], date.today().isoformat())

    @patch(
        "deep_research.system_contract._package_version",
        side_effect=lambda name: {
            "a2a-sdk": "1.1.1",
            "ag-ui-protocol": "0.1.19",
            "mcp": "1.28.1",
        }[name],
    )
    def test_protocol_claims_block_when_receipt_is_missing_even_for_matching_versions(
        self,
        _package_version: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-receipts.json"
            with patch.dict(
                os.environ,
                {"DEEP_RESEARCH_PROTOCOL_RECEIPTS": str(missing)},
                clear=False,
            ):
                contract = system_contract()
        packages = contract["official_verification"]["packages"]
        self.assertTrue(all(item["runtime_status"] == "current-runtime" for item in packages))
        self.assertTrue(all(item["verification_status"] == "adapter-blocked" for item in packages))
        self.assertIsNone(contract["official_verification"]["receipt_checked_at"])


class AgUiCancellationTest(unittest.TestCase):
    def tearDown(self) -> None:
        with webapp._jobs_lock:
            webapp._jobs.pop("agui-disconnect-test", None)
            webapp._cancel_events.pop("agui-disconnect-test", None)
            webapp._agui_stream_counts.pop("agui-disconnect-test", None)

    def test_disconnect_requests_cooperative_cancellation(self) -> None:
        event = threading.Event()
        with webapp._jobs_lock:
            webapp._jobs["agui-disconnect-test"] = {
                "status": "running",
                "error": "",
            }
            webapp._cancel_events["agui-disconnect-test"] = event

        webapp._cancel_background_run(
            "agui-disconnect-test",
            "client disconnected",
        )

        self.assertTrue(event.is_set())
        self.assertEqual(
            webapp._jobs["agui-disconnect-test"]["status"],
            "cancelling",
        )
        self.assertEqual(
            webapp._jobs["agui-disconnect-test"]["error"],
            "client disconnected",
        )

    def test_agui_stream_reference_count_prevents_single_subscriber_cancellation(self) -> None:
        self.assertEqual(webapp._register_agui_stream("agui-disconnect-test"), 1)
        self.assertEqual(webapp._register_agui_stream("agui-disconnect-test"), 2)
        self.assertEqual(webapp._release_agui_stream("agui-disconnect-test"), 1)
        with webapp._jobs_lock:
            self.assertEqual(
                webapp._agui_stream_counts["agui-disconnect-test"],
                1,
            )
        self.assertEqual(webapp._release_agui_stream("agui-disconnect-test"), 0)

    def test_resume_worker_reservation_is_single_owner_and_releasable(self) -> None:
        self.assertTrue(webapp._reserve_resume_worker("agui-disconnect-test"))
        self.assertFalse(webapp._reserve_resume_worker("agui-disconnect-test"))
        webapp._clear_resume_worker_reservation("agui-disconnect-test")
        self.assertTrue(webapp._reserve_resume_worker("agui-disconnect-test"))
        with webapp._jobs_lock:
            webapp._jobs["agui-disconnect-test"] = {"status": "queued", "error": ""}
        webapp._clear_resume_worker_reservation("agui-disconnect-test")
        with webapp._jobs_lock:
            self.assertNotIn("agui-disconnect-test", webapp._jobs)

    def test_higher_fence_replaces_stale_job_and_old_worker_cannot_clear_it(self) -> None:
        old_lease = {"owner_token": "old-owner", "fence": 3}
        new_lease = {"owner_token": "new-owner", "fence": 4}
        with webapp._jobs_lock:
            webapp._jobs["agui-disconnect-test"] = webapp._job_for_lease(
                "running", old_lease
            )

        self.assertTrue(
            webapp._reserve_resume_worker("agui-disconnect-test", new_lease)
        )
        webapp._clear_resume_worker_reservation(
            "agui-disconnect-test", old_lease
        )

        with webapp._jobs_lock:
            current = dict(webapp._jobs["agui-disconnect-test"])
            self.assertFalse(
                webapp._worker_owns_job("agui-disconnect-test", old_lease)
            )
            self.assertTrue(
                webapp._worker_owns_job("agui-disconnect-test", new_lease)
            )
        self.assertEqual(current["owner_token"], "new-owner")
        self.assertEqual(current["fence"], 4)

    def test_event_reader_ignores_crash_tail_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_bytes(
                b'{"event_id":"complete","payload":{}}\n'
                b'{"event_id":"partial"'
            )

            self.assertEqual(
                [item["event_id"] for item in webapp._read_events(path)],
                ["complete"],
            )

    def test_agui_lifecycle_emits_one_start_and_one_terminal_event(self) -> None:
        events: list[dict[str, object]] = []
        lifecycle = webapp._AgUiLifecycle(events.append)

        lifecycle.start("thread-1", "run-1")
        lifecycle.start("thread-1", "run-1")
        self.assertTrue(
            lifecycle.finish(
                "thread-1",
                "run-1",
                result={"outcome": "completed"},
                success=True,
            )
        )
        self.assertFalse(
            lifecycle.error("thread-1", "run-1", "late worker error")
        )
        self.assertEqual(
            [event["type"] for event in events],
            ["RUN_STARTED", "RUN_FINISHED"],
        )
        self.assertTrue(lifecycle.started)
        self.assertTrue(lifecycle.terminal)

    def test_agui_lifecycle_rejects_terminal_before_start(self) -> None:
        lifecycle = webapp._AgUiLifecycle(lambda _event: None)
        with self.assertRaisesRegex(RuntimeError, "RUN_STARTED"):
            lifecycle.error("thread-1", "run-1", "not started")

    def test_open_interrupt_query_migrates_a_recognized_pre_agui_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "legacy-run")
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                connection.execute("DROP TABLE agui_interrupts")
                connection.commit()

            self.assertEqual(
                webapp._open_interrupts_for_thread(runs_dir, "thread-1"),
                [],
            )
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(agui_interrupts)"
                    )
                }
            self.assertIn("response_schema_json", columns)

    def test_open_interrupt_query_migrates_checkpoint_only_legacy_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_dir = runs_dir / "checkpoint-only-run"
            run_dir.mkdir(parents=True)
            database = run_dir / "checkpoints.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE checkpoints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        node TEXT NOT NULL,
                        state_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO checkpoints(created_at, node, state_json) VALUES (?, ?, ?)",
                    (
                        "2026-07-01T00:00:00+00:00",
                        "finalize",
                        json.dumps(
                            {
                                "run_id": "checkpoint-only-run",
                                "question": "legacy question",
                                "status": "completed",
                            }
                        ),
                    ),
                )
                connection.commit()

            self.assertEqual(
                webapp._open_interrupts_for_thread(runs_dir, "thread-1"),
                [],
            )
            with closing(sqlite3.connect(database)) as connection, connection:
                migrated = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'agui_interrupts'"
                ).fetchone()
            self.assertIsNotNone(migrated)

    def test_open_interrupt_query_rejects_forged_checkpoint_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_dir = runs_dir / "claimed-run"
            run_dir.mkdir(parents=True)
            database = run_dir / "checkpoints.sqlite"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    """
                    CREATE TABLE checkpoints (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        created_at TEXT NOT NULL,
                        node TEXT NOT NULL,
                        state_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO checkpoints(created_at, node, state_json) VALUES (?, ?, ?)",
                    (
                        "2026-07-01T00:00:00+00:00",
                        "finalize",
                        json.dumps(
                            {
                                "run_id": "different-run",
                                "question": "forged",
                                "status": "completed",
                            }
                        ),
                    ),
                )
                connection.commit()

            with self.assertRaises(webapp.OpenInterruptQueryError):
                webapp._open_interrupts_for_thread(runs_dir, "thread-1")

    def test_open_interrupt_query_fails_closed_on_missing_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_dir = runs_dir / "corrupt-run"
            run_dir.mkdir()
            with closing(
                sqlite3.connect(run_dir / "checkpoints.sqlite")
            ) as connection, connection:
                connection.execute("CREATE TABLE unrelated(value TEXT)")
                connection.commit()

            with self.assertRaises(webapp.OpenInterruptQueryError):
                webapp._open_interrupts_for_thread(runs_dir, "thread-1")

    def test_open_interrupt_query_fails_closed_on_malformed_schema_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "malformed-schema-run")
            interrupt_id = store.create_agui_interrupt(
                "thread-1",
                "producer-1",
                "evidence_incomplete",
            )
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                connection.execute(
                    "UPDATE agui_interrupts SET response_schema_json = ? WHERE interrupt_id = ?",
                    ("{not-json", interrupt_id),
                )
                connection.commit()

            with self.assertRaises(webapp.OpenInterruptQueryError):
                webapp._open_interrupts_for_thread(runs_dir, "thread-1")

    def test_worker_exception_is_written_to_durable_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            run_id = "worker-audit-run"
            store = RunStore(runs_dir, run_id)
            lease = store.acquire_execution_lease("producer:worker-audit-run")
            self.assertIsNotNone(lease)
            lease = dict(lease)
            with webapp._jobs_lock:
                webapp._jobs[run_id] = webapp._job_for_lease("queued", lease)
                webapp._cancel_events[run_id] = threading.Event()
            self.assertTrue(webapp._worker_slots.acquire(blocking=False))

            class FailingEngine:
                def __init__(self, *_args, **_kwargs) -> None:
                    pass

                async def run(self, _question: str, _run_id: str) -> ResearchState:
                    raise RuntimeError("synthetic worker failure")

            try:
                with patch.dict(os.environ, {"DR_RUNS_DIR": str(runs_dir)}):
                    with patch.object(webapp, "ResearchEngine", FailingEngine):
                        webapp._run_in_background(
                            run_id,
                            "question",
                            True,
                            execution_lease=lease,
                        )
            finally:
                with webapp._jobs_lock:
                    webapp._jobs.pop(run_id, None)
                    webapp._cancel_events.pop(run_id, None)

            worker_events = webapp._worker_audit_projection(store)
            self.assertEqual(len(worker_events), 1)
            self.assertEqual(worker_events[0]["event_type"], "worker_exception")
            self.assertEqual(
                worker_events[0]["payload"]["exception_type"],
                "RuntimeError",
            )
            self.assertEqual(worker_events[0]["payload"]["fence"], lease["fence"])

    def test_resume_worker_exception_with_durable_failure_remains_reclaimable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            config = webapp.AppConfig(runs_dir=runs_dir)
            run_id = "resume-worker-failure"
            store = RunStore(runs_dir, run_id)
            initial = ResearchState(
                run_id=run_id,
                question="Recover a failed worker",
                status="evidence_incomplete",
                next_node="done",
                suspension={"resume_node": "plan"},
            )
            store.commit_stage("finalize", initial, "run_finished", {})
            prepared = webapp.prepare_resume(
                config,
                run_id,
                {},
                source="manual",
                idempotency_key="manual:resume-worker-failure:request",
            )
            lease = store.acquire_execution_lease(prepared.idempotency_key)
            self.assertIsNotNone(lease)
            lease = dict(lease)
            self.assertTrue(
                store.claim_resume_execution(
                    prepared.idempotency_key,
                    owner_token=lease["owner_token"],
                    fence=lease["fence"],
                )
            )
            with webapp._jobs_lock:
                webapp._jobs[run_id] = webapp._job_for_lease("queued", lease)
                webapp._cancel_events[run_id] = threading.Event()
            self.assertTrue(webapp._worker_slots.acquire(blocking=False))

            class PersistThenFailEngine:
                def __init__(self, config_arg, *_args, **kwargs) -> None:
                    self.config = config_arg
                    self.execution_lease = kwargs["execution_lease"]

                async def run(self, _question: str, _run_id: str) -> ResearchState:
                    failed = ResearchState(
                        run_id=_run_id,
                        question="Recover a failed worker",
                        status="failed",
                        next_node="done",
                        suspension={"resume_node": "plan"},
                        failures=[
                            {
                                "type": "runtime_error",
                                "reason": "synthetic durable failure",
                                "retryable": True,
                                "next_node": "plan",
                            }
                        ],
                    )
                    durable = RunStore(self.config.runs_dir, _run_id)
                    durable.bind_execution_fence(
                        str(self.execution_lease["owner_token"]),
                        int(self.execution_lease["fence"]),
                    )
                    durable.commit_stage("recover", failed, "run_failed", {})
                    raise RuntimeError("synthetic worker failure after checkpoint")

            try:
                with patch.object(webapp, "ResearchEngine", PersistThenFailEngine):
                    webapp._run_in_background(
                        run_id,
                        "Recover a failed worker",
                        True,
                        execution_lease=lease,
                        runs_dir=runs_dir,
                    )
            finally:
                with webapp._jobs_lock:
                    webapp._jobs.pop(run_id, None)
                    webapp._cancel_events.pop(run_id, None)

            receipt = store.resume_receipt(prepared.idempotency_key)
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt["execution_status"], "failed")
            self.assertEqual(receipt["durable_run_status"], "failed")
            self.assertFalse(receipt["execution_claimed"])
            self.assertEqual(receipt["claim_fence"], 0)
            replay = webapp.prepare_resume(
                config,
                run_id,
                {},
                source="manual",
                idempotency_key=prepared.idempotency_key,
            )
            self.assertTrue(replay.replayed)
            self.assertTrue(replay.should_start_worker)
            self.assertEqual(replay.response["durable_run_status"], "failed")

    def test_event_window_marks_a_bounded_tail_as_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "event-window-run")
            with closing(
                sqlite3.connect(store.database_path)
            ) as connection, connection:
                connection.executemany(
                    """
                    INSERT INTO outbox(
                        event_id, event_json, created_at, published_at
                    ) VALUES (?, '{}', ?, ?)
                    """,
                    [
                        (f"event-{index}", str(index), str(index))
                        for index in range(4)
                    ],
                )
                connection.commit()

            projection = webapp._event_window_projection(
                store,
                [{"event_id": "event-2"}, {"event_id": "event-3"}],
            )

            self.assertEqual(projection["returned_count"], 2)
            self.assertEqual(projection["total_count"], 4)
            self.assertEqual(projection["first_global_index"], 3)
            self.assertEqual(projection["last_global_index"], 4)
            self.assertFalse(projection["complete"])

    def test_legacy_jsonl_events_do_not_invent_impossible_global_indices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "legacy-event-window-run")
            projection = webapp._event_window_projection(
                store,
                [{"event_id": "legacy-1"}, {"event_id": "legacy-2"}],
            )

            self.assertEqual(projection["returned_count"], 2)
            self.assertIsNone(projection["total_count"])
            self.assertIsNone(projection["first_global_index"])
            self.assertIsNone(projection["last_global_index"])
            self.assertFalse(projection["complete"])
            self.assertEqual(projection["count_status"], "legacy_unverified")

    def test_get_and_sse_share_the_same_durable_audit_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "shared-audit-run")

            audit = webapp._run_audit_projection(store)

            self.assertTrue(
                {
                    "invocations",
                    "handoffs",
                    "receipts",
                    "source_fetches",
                    "artifacts",
                    "resume_receipts",
                    "usage",
                    "worker",
                    "pagination",
                }.issubset(audit)
            )
            self.assertEqual(audit["usage"]["usage_status"], "unavailable")
            self.assertIn("updated_at", audit["usage"])
            self.assertIn("pending_model_operations", audit["usage"])
            self.assertEqual(audit["usage"]["pending_model_operations"], 0)
            self.assertEqual(
                audit["pagination"]["limit"], webapp.DEFAULT_AUDIT_PAGE_LIMIT
            )
            self.assertFalse(audit["pagination"]["has_more"])

            store.begin_operation("live-plan", "plan", "question-v1")
            store.record_model_usage_event(
                "live-plan",
                {
                    "model_calls": 1,
                    "model_cache_hits": 0,
                    "input_tokens": 120,
                    "output_tokens": 30,
                    "estimated_cost_usd": 0.0015,
                    "provider": "gpt",
                    "pricing_configured": True,
                    "pricing_status": "complete",
                },
            )
            live_audit = webapp._run_audit_projection(store)
            self.assertEqual(live_audit["usage"]["model_calls"], 1)
            self.assertAlmostEqual(live_audit["usage"]["estimated_cost_usd"], 0.0015)
            self.assertEqual(live_audit["usage"]["settled_model_responses"], 1)
            self.assertEqual(live_audit["usage"]["settled_model_operations"], 1)

    def test_bounded_audit_projection_keyset_cursor_has_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "paged-audit-run")
            for index in range(5):
                store.save_invocation(
                    AgentInvocation(
                        invocation_id=f"page-{index}",
                        agent_id="planner",
                        role="planner",
                        operation="plan",
                        attempt=1,
                        started_at=f"2026-01-01T00:00:0{index}Z",
                        ended_at=f"2026-01-01T00:00:0{index}Z",
                        status="succeeded",
                        input_type="question",
                    )
                )

            cursor: dict[str, object] = {}
            seen: list[str] = []
            pages = []
            for _ in range(4):
                page = webapp._run_audit_projection(
                    store,
                    limit=2,
                    cursor=cursor,
                )
                pages.append(page)
                seen.extend(item["invocation_id"] for item in page["invocations"])
                next_cursor = page["pagination"]["next_cursor"]
                if not next_cursor:
                    break
                cursor = webapp._decode_audit_cursor(str(next_cursor))

            self.assertEqual(seen, [f"page-{index}" for index in range(5)])
            self.assertEqual(len(seen), len(set(seen)))
            self.assertTrue(pages[0]["pagination"]["invocations"]["has_more"])
            self.assertFalse(pages[-1]["pagination"]["has_more"])
            self.assertLessEqual(
                webapp._json_size(pages[0]),
                webapp.MAX_AUDIT_RESPONSE_BYTES,
            )

    def test_artifact_audit_page_keeps_filesystem_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "orphan-page-run")
            artifacts = store.run_dir / "artifacts"
            artifacts.mkdir(exist_ok=True)
            (artifacts / "Aorphan.json").write_text("{}", encoding="utf-8")
            (artifacts / "Aorphan.meta.json").write_text("{}", encoding="utf-8")

            page = store.artifact_manifest_audit_page(limit=1)

            self.assertEqual([item["artifact_id"] for item in page["items"]], ["Aorphan"])
            self.assertFalse(page["has_more"])
            self.assertIsNone(page["next_cursor"])

    def test_audit_query_rejects_unsafe_window_and_oversized_record(self) -> None:
        with self.assertRaises(ValueError):
            webapp._parse_audit_query("limit=0")
        with self.assertRaises(ValueError):
            webapp._parse_audit_query("limit=51")
        with self.assertRaises(ValueError):
            webapp._parse_audit_query("limit=2&cursor=not-a-cursor")

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp), "oversized-audit-run")
            with patch.object(
                store,
                "invocation_rows_page",
                return_value={
                    "items": [{"invocation_id": "huge", "payload": "x" * 600_000}],
                    "has_more": False,
                    "next_cursor": None,
                },
            ):
                with self.assertRaises(webapp.AuditResponseTooLargeError):
                    webapp._run_audit_projection(store, limit=10, cursor={})

    def test_agui_resume_interrupt_maps_to_durable_run_and_budget_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "durable-run")
            interrupt_id = store.create_agui_interrupt(
                "thread-1",
                "external-run-1",
                "evidence_incomplete",
            )
            parsed = webapp._agui_resume_request(
                {
                    "resume": [
                        {
                            "interruptId": interrupt_id,
                            "status": "resolved",
                            "payload": {
                                "action": "continue_research",
                                "additionalIterations": 2,
                                "additionalSearchCalls": 4,
                                "additionalPages": 6,
                            },
                        }
                    ]
                },
                runs_dir,
                "thread-1",
                "external-resume-1",
            )
            self.assertEqual(parsed[0], "durable-run")
            self.assertEqual(parsed[1], interrupt_id)
            self.assertEqual(parsed[2]["additional_iterations"], 2)
            self.assertFalse(parsed[2]["confirm_ambiguous_retry"])
            self.assertNotIn("durable-run", interrupt_id)

    def test_agui_resume_accepts_payloadless_cancellation_and_rejects_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "durable-run")
            interrupt_id = store.create_agui_interrupt(
                "thread-1",
                "external-run-1",
                "evidence_incomplete",
            )
            parsed = webapp._agui_resume_request(
                {
                    "resume": [
                        {
                            "interruptId": interrupt_id,
                            "status": "cancelled",
                        }
                    ]
                },
                runs_dir,
                "thread-1",
                "external-cancel-1",
            )
            self.assertEqual(
                parsed[2]["interrupt_responses"][0]["status"],
                "cancelled",
            )
            with self.assertRaisesRegex(ValueError, "unknown or duplicate interrupt"):
                webapp._agui_resume_request(
                    {
                        "resume": [
                            {
                                "interruptId": "int:v1:unknown",
                                "status": "resolved",
                                "payload": {"action": "continue_research"},
                            }
                        ]
                    },
                    runs_dir,
                    "thread-1",
                    "external-unknown-1",
                )

    def test_agui_resume_uses_the_schema_persisted_with_the_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            store = RunStore(runs_dir, "schema-run")
            interrupt_id = store.create_agui_interrupt(
                "thread-schema",
                "external-schema-producer",
                "evidence_incomplete",
                {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "const": "legacy_continue"}
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            )
            parsed = webapp._agui_resume_request(
                {
                    "resume": [
                        {
                            "interruptId": interrupt_id,
                            "status": "resolved",
                            "payload": {"action": "legacy_continue"},
                        }
                    ]
                },
                runs_dir,
                "thread-schema",
                "external-schema-resume",
            )
            self.assertEqual(
                parsed[2]["interrupt_responses"][0]["payload"]["action"],
                "legacy_continue",
            )

    def test_new_agui_resume_rejects_a_partial_thread_wide_interrupt_set(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            first = RunStore(runs_dir, "thread-run-one").create_agui_interrupt(
                "shared-thread",
                "producer-one",
                "evidence_incomplete",
            )
            RunStore(runs_dir, "thread-run-two").create_agui_interrupt(
                "shared-thread",
                "producer-two",
                "verification_failed",
            )
            with self.assertRaisesRegex(ValueError, "thread-wide open interrupt set"):
                webapp._agui_resume_request(
                    {
                        "resume": [
                            {
                                "interruptId": first,
                                "status": "resolved",
                                "payload": {"action": "continue_research"},
                            }
                        ]
                    },
                    runs_dir,
                    "shared-thread",
                    "new-external-run",
                )


if __name__ == "__main__":
    unittest.main()
