"""Server-owned capability contract rendered by the research dossier UI."""

from __future__ import annotations

from datetime import date
from importlib.metadata import PackageNotFoundError, version
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any


_SNAPSHOT_CHECKED_AT = "2026-07-20"
_RECEIPT_MAX_AGE_DAYS = 30
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _package_version(distribution: str) -> str:
    try:
        return version(distribution)
    except PackageNotFoundError:
        return "not-installed"


def _runtime_status(installed: str, expected: str) -> str:
    if installed == "not-installed":
        return "candidate"
    if installed != expected:
        return "adapter-blocked"
    return "current-runtime"


def _receipt_path() -> Path:
    configured = Path(
        os.environ.get(
            "DEEP_RESEARCH_PROTOCOL_RECEIPTS",
            "conformance/protocol-validation-receipts.json",
        )
    )
    if configured.is_absolute():
        return configured
    return Path(__file__).resolve().parents[2] / configured


def _adapter_receipt(
    name: str,
    installed: str,
    expected: str,
    *,
    required_fields: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    if installed != expected:
        return None
    path = _receipt_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        receipts = payload.get("receipts", {})
        receipt = receipts.get(name) or receipts.get(name.casefold())
    except (AttributeError, OSError, ValueError, TypeError):
        return None
    if not isinstance(receipts, dict) or not isinstance(receipt, dict):
        return None
    if receipt.get("sdk_version") != expected or receipt.get("passed") is not True:
        return None
    if any(receipt.get(key) != value for key, value in (required_fields or {}).items()):
        return None
    if not str(receipt.get("command") or "").strip():
        return None
    checked_at = str(receipt.get("checked_at") or "")[:10]
    try:
        age = (date.today() - date.fromisoformat(checked_at)).days
    except ValueError:
        return None
    if age < 0 or age > _RECEIPT_MAX_AGE_DAYS:
        return None
    evidence_sha256 = str(receipt.get("evidence_sha256") or "")
    if not _SHA256_RE.fullmatch(evidence_sha256):
        return None
    evidence_path = receipt.get("evidence_path")
    if evidence_path:
        evidence = Path(str(evidence_path))
        if not evidence.is_absolute():
            evidence = _receipt_path().parent / evidence
        try:
            digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
        except OSError:
            return None
        if digest != evidence_sha256:
            return None
    return receipt


def _adapter_status(
    name: str,
    installed: str,
    expected: str,
    *,
    required_fields: dict[str, str] | None = None,
) -> tuple[str, str, dict[str, Any] | None]:
    runtime_status = _runtime_status(installed, expected)
    if runtime_status != "current-runtime":
        return runtime_status, runtime_status, None
    receipt = _adapter_receipt(
        name,
        installed,
        expected,
        required_fields=required_fields,
    )
    if receipt is None:
        return runtime_status, "adapter-blocked", None
    return runtime_status, "validated-adapter", receipt


def _boundary_maturity(adapter_status: str, nominal: str) -> str:
    if adapter_status == "validated-adapter":
        return nominal
    return adapter_status


def _version_limit(name: str, installed: str, expected: str) -> str | None:
    status = _runtime_status(installed, expected)
    if status == "current-runtime":
        return None
    if status == "candidate":
        return f"{name} 期望 SDK {expected}，当前解释器未安装；只能作为 candidate，不能复用已验证适配结论。"
    return (
        f"{name} 期望 SDK {expected}，当前安装 {installed}；"
        "版本不一致，当前适配路径标记为 adapter-blocked。"
    )


def _adapter_limit(
    name: str,
    installed: str,
    expected: str,
    runtime_status: str,
    adapter_status: str,
) -> str | None:
    version_limit = _version_limit(name, installed, expected)
    if version_limit:
        return version_limit
    if runtime_status == "current-runtime" and adapter_status != "validated-adapter":
        return (
            f"{name} SDK {expected} 已安装，但没有有效、未过期的 protocol validation receipt；"
            "当前适配路径标记为 adapter-blocked。"
        )
    return None


def _typescript_lock_status(expected: str = "0.0.57") -> str:
    """Check the checked-in client lock, not an unobserved global npm install."""
    root = Path(__file__).resolve().parents[2] / "conformance" / "agui"
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        dependencies = package.get("dependencies", {})
        packages = lock.get("packages", {})
        for name in ("@ag-ui/core", "@ag-ui/client"):
            if dependencies.get(name) != expected:
                return "adapter-blocked"
            if packages.get(f"node_modules/{name}", {}).get("version") != expected:
                return "adapter-blocked"
    except (OSError, ValueError, TypeError):
        return "adapter-blocked"
    return "current-lock"


def _receipt_projection(receipt: dict[str, Any] | None) -> dict[str, Any] | None:
    if receipt is None:
        return None
    return {
        "checked_at": receipt.get("checked_at"),
        "command": receipt.get("command"),
        "evidence_path": receipt.get("evidence_path"),
        "evidence_sha256": receipt.get("evidence_sha256"),
    }


def system_contract() -> dict[str, Any]:
    """Return implementation claims together with their verification boundary."""
    a2a_version = _package_version("a2a-sdk")
    agui_version = _package_version("ag-ui-protocol")
    mcp_version = _package_version("mcp")
    a2a_runtime_status, a2a_status, a2a_receipt = _adapter_status(
        "A2A",
        a2a_version,
        "1.1.1",
        required_fields={"protocol_version": "1.0"},
    )
    agui_runtime_status, agui_conformance_status, agui_receipt = _adapter_status(
        "AG-UI",
        agui_version,
        "0.1.19",
        required_fields={"typescript_version": "0.0.57"},
    )
    agui_typescript_status = _typescript_lock_status()
    if agui_conformance_status == "validated-adapter" and agui_typescript_status != "current-lock":
        agui_conformance_status = "adapter-blocked"
        agui_receipt = None
    # The locked live harness validates only a finite adapter surface. A
    # package/version match is a runtime fact, not conformance evidence.
    mcp_runtime_status, mcp_status, mcp_receipt = _adapter_status(
        "MCP",
        mcp_version,
        "1.28.1",
        required_fields={"protocol_version": "2025-11-25"},
    )
    a2a_version_limit = _adapter_limit(
        "A2A", a2a_version, "1.1.1", a2a_runtime_status, a2a_status
    )
    agui_version_limit = _adapter_limit(
        "AG-UI Python", agui_version, "0.1.19", agui_runtime_status, agui_conformance_status
    )
    mcp_version_limit = _adapter_limit(
        "MCP", mcp_version, "1.28.1", mcp_runtime_status, mcp_status
    )
    receipt_dates = [
        str(receipt.get("checked_at"))
        for receipt in (a2a_receipt, agui_receipt, mcp_receipt)
        if receipt and receipt.get("checked_at")
    ]
    receipt_checked_at = max(receipt_dates) if receipt_dates else None
    return {
        "contract_version": "fieldnote-system-contract-v5",
        "source_of_truth": "server capability registry",
        "official_verification": {
            "checked_at": _SNAPSHOT_CHECKED_AT,
            "snapshot_checked_at": _SNAPSHOT_CHECKED_AT,
            "receipt_checked_at": receipt_checked_at,
            "record": "docs/官方协议版本核验_20260718.md",
            "record_url": "/api/protocol-verification",
            "receipt_path": str(_receipt_path()),
            "receipt_max_age_days": _RECEIPT_MAX_AGE_DAYS,
            "packages": [
                {
                    "name": "A2A",
                    "protocol_version": "1.0",
                    "upstream": "Spec 1.0 / SDK 1.1.1",
                    "installed": f"SDK {a2a_version}",
                    "expected_sdk": "1.1.1",
                    "runtime_status": a2a_runtime_status,
                    "adapter_verification_status": a2a_status,
                    "verification_status": a2a_status,
                    "decision": (
                        "有限 JSON-RPC 绑定已验证；不宣称完整 A2A 网关"
                        if a2a_status == "validated-adapter"
                        else a2a_version_limit or "当前 A2A 适配状态未记录。"
                    ),
                    "snapshot_checked_at": _SNAPSHOT_CHECKED_AT,
                    "receipt_checked_at": a2a_receipt.get("checked_at") if a2a_receipt else None,
                    "receipt": _receipt_projection(a2a_receipt),
                    "source_url": "https://pypi.org/project/a2a-sdk/1.1.1/",
                },
                {
                    "name": "MCP",
                    "protocol_version": "2025-11-25",
                    "upstream": "stable 1.28.1 / preview 2.0.0b2",
                    "installed": f"SDK {mcp_version}",
                    "expected_sdk": "1.28.1",
                    "runtime_status": mcp_runtime_status,
                    "adapter_verification_status": mcp_status,
                    "verification_status": mcp_status,
                    "decision": (
                        "有限 stdio search/fetch 路径已验证；不宣称覆盖 MCP 全能力"
                        if mcp_status == "validated-adapter"
                        else mcp_version_limit or "当前 MCP 适配状态未记录。"
                    ),
                    "snapshot_checked_at": _SNAPSHOT_CHECKED_AT,
                    "receipt_checked_at": mcp_receipt.get("checked_at") if mcp_receipt else None,
                    "receipt": _receipt_projection(mcp_receipt),
                    "source_url": "https://pypi.org/project/mcp/1.28.1/",
                },
                {
                    "name": "AG-UI",
                    "protocol_version": "finite HTTP/SSE adapter profile",
                    "upstream": "Python 0.1.19 / TypeScript 0.0.57",
                    "installed": f"Python {agui_version} / TS pinned 0.0.57",
                    "expected_sdk": "Python 0.1.19 / TypeScript 0.0.57",
                    "runtime_status": agui_runtime_status,
                    "typescript_lock_status": agui_typescript_status,
                    "adapter_verification_status": agui_conformance_status,
                    "verification_status": agui_conformance_status,
                    "version_status": agui_runtime_status,
                    "decision": (
                        "Python/TypeScript 版本匹配，固定有限路径（含 resume、幂等重放与取消）已通过官方客户端 harness；不宣称完整 AG-UI conformance"
                        if agui_conformance_status == "validated-adapter"
                        else agui_version_limit or "当前 AG-UI 适配状态未记录。"
                    ),
                    "snapshot_checked_at": _SNAPSHOT_CHECKED_AT,
                    "receipt_checked_at": agui_receipt.get("checked_at") if agui_receipt else None,
                    "receipt": _receipt_projection(agui_receipt),
                    "source_url": "https://www.npmjs.com/package/@ag-ui/core/v/0.0.57",
                },
            ],
        },
        "warning": "每个协议名称都必须同时展示实际绑定、验证证据和已知限制；六个内部角色不会被包装成六个远程 Agent。",
        "selection_reviews": [
            {
                "id": "internal-transport",
                "decision": "内部角色使用 deep-research-handoff/1.1，不使用 A2A over HTTP",
                "reviewer_role": "architecture-challenger",
                "challenge": "既然 A2A 是最新认可的 Agent-to-Agent 协议，为什么不让 Planner、Scout、Curator、Critic、Writer、Verifier 全部走 A2A？",
                "response": "当前六个角色共享一个 durable run、进程和模型 provider；引入网络 A2A 会增加身份、序列化、断线和跨服务恢复面，却不能增加实际部署隔离。对外暴露完整 ResearchEngine 为一个 A2A Agent，内部用可持久化 receipt 保留同等可审计性。",
                "evidence": "A2A 适合独立部署的 opaque Agent；当前内部 invocation 集成测试与 receipt 消费测试已经覆盖本地协作边界。",
                "revisit_when": "角色需要跨进程、跨组织或独立扩缩容时，重新评估 A2A Task/Artifact 边界。",
                "status": "保留当前选型",
            },
            {
                "id": "workflow-runtime",
                "decision": "显式持久化节点游标、operation ledger 与 SQLite lease",
                "reviewer_role": "runtime-challenger",
                "challenge": "LangGraph 已提供 checkpoint、interrupt 和 durable execution，手写状态机是否会重复造轮子？",
                "response": "当前原型需要对每个搜索、抓取、模型调用、receipt、owner/fence 和未知计费结果给出可重算证据；显式状态让这些写入边界可测试、可解释。LangGraph 是记录过的生产迁移候选，而不是当前实现已经使用的事实。",
                "evidence": "checkpoint/replay、ambiguous operation、lease fence 和 crash recovery 测试；ADR 记录 LangGraph persistence 作为迁移参照。",
                "revisit_when": "需要分布式 worker、跨服务 checkpoint 或团队接受 LangGraph node/checkpointer 的等价审计契约时。",
                "status": "原型阶段保留，生产候选待评估",
            },
            {
                "id": "tool-boundary",
                "decision": "MCP 只承载 search/fetch 等工具，不承载长程研究角色",
                "reviewer_role": "protocol-boundary-challenger",
                "challenge": "把所有角色都注册成 MCP tool 是否能统一接口并减少自定义协议？",
                "response": "MCP 解决 Agent/模型到工具和资源的连接，不定义长程 Agent 任务、阶段恢复或角色交接；把 Critic/Writer 伪装成工具会隐藏状态和失败语义。工具 schema 与内部 HandoffEnvelope 分开更容易验证。",
                "evidence": "MCP 2025-11-25 官方边界、stdio initialize/tools/list/tools/call 集成测试。",
                "revisit_when": "外部工具需要被多个独立 Agent 发现和调用时，只扩展 MCP server，不改变内部角色语义。",
                "status": "保留当前选型",
            },
            {
                "id": "browser-events",
                "decision": "GET 使用明确标注的自定义 SSE；POST 保留固定版本 AG-UI 有限适配，锁定 harness 已验证有限 resume 路径",
                "reviewer_role": "frontend-protocol-challenger",
                "challenge": "既然前端已有 SSE，为什么不直接宣称 AG-UI compliant，或者全部改成一个私有事件格式？",
                "response": "项目自定义 GET 载荷与官方 AG-UI RunAgentInput/Event schema 不等价；POST 的输入、事件构造、首轮终态、resolved/cancelled resume、durable run 复用和幂等重放已由固定 SDK 及真实 HTTP harness 验证。GET 保留浏览器 dossier 所需的有界 state/events/job 投影并明确标注边界。",
                "evidence": "Python 官方 SDK 类型验证；TypeScript core/client 0.0.57 的 transformHttpEventStream + verifyEvents；真实 HTTP 测试覆盖 success、evidence-incomplete interrupt、resolved/cancelled resume、消息快照、schema/事件顺序及负例。该证据只覆盖有限适配路径，不等于完整协议认证。",
                "revisit_when": "官方 schema 覆盖当前 state/history/tools/context 语义并完成更广泛事件、传输和认证 conformance 后，再考虑替换 GET 流。",
                "status": "固定版本有限适配已验证；完整协议 conformance 仍不宣称",
            },
            {
                "id": "search-provider",
                "decision": "默认使用无 key OpenAlex 学术发现和官方 arXiv fallback；运行时保留带显式凭据的 Brave Web Search、无 key DuckDuckGo 与 replay，Brave/Exa/Tavily 是可选扩展",
                "reviewer_role": "retrieval-challenger",
                "challenge": "DuckDuckGo HTML 没有稳定 SLA，为什么还让它进入研究系统？",
                "response": "OpenAlex 只负责无 key 的论文候选发现，返回的元数据不会直接成为结论；系统随后抓取开放获取落地页、仓储页、PDF 或 DOI 页面，并沿用同一套 SSRF/重定向/socket pinning、缓存和来源快照链路。OpenAlex 无匹配或不可用时只回退官方 arXiv，不调用 DuckDuckGo HTML。DuckDuckGo 保留开发入口；Brave 通过固定 HTTPS endpoint、header-only token、禁止认证请求跳转提供可选通用网页发现。",
                "evidence": "真实 OpenAlex 只读响应探测、OpenAlexSearchProvider 响应解析/无 key 配置/抓取链路测试、BraveSearchProvider 请求/缓存/缺密钥测试、ReplaySearchProvider、搜索/抓取幂等测试。",
                "revisit_when": "研究问题需要 OpenAlex/arXiv 之外的通用网页来源，或无 key 入口的召回、区域可用性不满足要求时，接入并实测 Brave/Exa/Tavily，再重新测预算与来源覆盖。",
                "status": "原型可复现，生产不默认承诺",
            },
            {
                "id": "evidence-stop",
                "decision": "加权 evidence sufficiency 只排序缺口，原子 hard gate 决定能否停止",
                "reviewer_role": "evidence-policy-challenger",
                "challenge": "为什么不让一个 98 分的总分直接结束研究，反而要求来源、原文、反证和冲突逐项通过？",
                "response": "缺失的反证或引用不能被其他高分项补偿；总分是诊断性进度，不是事实概率。硬门逐目标记录让人可以指出具体缺口，避免把启发式分数包装成可信度。",
                "evidence": "evidence-closure-v4.24 方法契约、逐目标 SlotGateAudit、多模态 grounding 与验证器 exact citation-set 测试。",
                "revisit_when": "获得带标签的事实核验 benchmark 后校准分数，只能影响排序和展示，不能取消原子证据门。",
                "status": "保留当前选型",
            },
            {
                "id": "protocol-version",
                "decision": "A2A 1.0、MCP 稳定 v1、AG-UI 固定版本并单独跟踪 conformance，不追逐 beta",
                "reviewer_role": "version-challenger",
                "challenge": "最新 beta 版本可能功能更多，为什么不直接升级到 MCP v2 或随 AG-UI latest 漂移？",
                "response": "版本号相同不等于协议能力已验证；稳定线能够锁定回归行为，beta 只有在官方边界、迁移成本和真实 conformance 都清楚后才进入候选。上游最新状态、本机版本和验证范围分开展示。",
                "evidence": "docs/官方协议版本核验_20260718.md、PyPI/npm 官方版本记录、MCP/A2A 官方客户端测试，以及固定版本 AG-UI live harness 的有限 resume、幂等重放和取消路径。",
                "revisit_when": "上游稳定发布、迁移指南和本项目全量兼容测试同时具备时，开启版本升级审查。",
                "status": "锁定版本；各边界按当前 conformance 独立定级",
            },
        ],
        "boundaries": [
            {
                "id": "internal-handoff",
                "layer": "内部协作",
                "title": "六角色私有编排",
                "maturity": "implemented",
                "maturity_label": "内部已实现",
                "protocol": "deep-research-handoff/1.1",
                "implementation": "结构化 HandoffEnvelope 明确区分计划路由和接收 receipt；阶段产物保存为 canonical JSON，记录内容地址、字节数、规范化版本和可重算 SHA-256。",
                "verified_by": [
                    "六角色 AgentInvocation 集成测试",
                    "事件日志和前端交接审计可回到持久化信封及消费 invocation",
                    "测试从不可变 artifact 文件重新计算 SHA-256，并验证后续输入引用前序真实摘要",
                ],
                "limitations": [
                    "角色共享同一进程和模型 provider，不宣称为六个网络远程 Agent。",
                    "历史 deep-research-handoff/1.0 运行没有 receipt 和 canonical artifact 地址，前端必须标记为不可独立验证。",
                ],
            },
            {
                "id": "ag-ui",
                "layer": "浏览器过程流",
                "title": "AG-UI Python + TypeScript 有限适配",
                "maturity": _boundary_maturity(
                    agui_conformance_status,
                    "validated-adapter",
                ),
                "maturity_label": (
                    "有限适配已验证；不等于完整协议认证"
                    if agui_conformance_status == "validated-adapter"
                    else "当前适配被阻断或仅作为候选，不能复用验证结论"
                ),
                "protocol": f"Python {agui_version} / TypeScript core+client 0.0.57",
                "protocol_version": "finite HTTP/SSE adapter profile",
                "runtime_status": agui_runtime_status,
                "typescript_lock_status": agui_typescript_status,
                "adapter_version_status": agui_conformance_status,
                "adapter_verification_status": agui_conformance_status,
                "conformance_status": agui_conformance_status,
                "implementation": "POST 输入使用官方 RunAgentInput；输出由 Python SDK 类型构造，并由 TypeScript 官方客户端解析 SSE、验证 schema 与事件顺序。锁定的 live harness 已覆盖有限 success/interrupt/resume/cancel/idempotency 路径。thread 级协议索引保证外部 runId 全局唯一并记录 parent lineage；中断使用随机 opaque ID、持久化 responseSchema 和 durable 映射。",
                "verified_by": [
                    "RunAgentInput.model_validate",
                    "TypeAdapter(Event) 验证 Python 事件",
                    "真实 HTTP 流通过 transformHttpEventStream + verifyEvents",
                    "首轮客户端 runId、durable run 关联和 success/interrupt 终态断言",
                    "成功与证据不足 interrupt 的首轮官方 SDK 事件路径验证",
                    "锁定 TypeScript client harness 通过 resolved/cancelled resume、durable run 复用、同 runId 幂等重放、消息快照、schema/事件顺序和负例校验",
                    "interrupt 前 STATE_SNAPSHOT + MESSAGES_SNAPSHOT 与响应 schema 断言",
                    "官方 SDK 验证后的完整输入消息及原消息 ID 私有持久化，并用于后续消息快照",
                    "可选 parentRunId 校验外部运行谱系并写入 resume receipt（不等于恢复键）",
                    "thread 级 agui_protocol.sqlite3 为外部 runId 提供全局唯一主键、请求哈希重放和声明 parent lineage",
                    "共享 durable run 使用流引用计数；仅最后一个异常断开的订阅者触发协作式取消",
                    "SQLite execution lease 使用随机 owner token、单调 fence、15 秒 TTL 和 5 秒 heartbeat；启动失败与 worker finally 精确释放",
                ],
                "limitations": [
                    "GET 实时状态流是项目自定义 SSE。",
                    "完整消息历史会持久化并回放，但 ResearchEngine 目前仍只把最后一条用户消息作为研究问题；state、tools 与 context 尚未进入研究语义。",
                    "当前 ResearchEngine 每个终态只产生一个可操作 interrupt；入口和事务层已要求覆盖完整 open 集合，但尚未生成并行多类型中断。",
                    "AG-UI 完整 state/tools/context 多轮语义与正式 conformance 仍未覆盖；当前只可讨论固定有限适配路径。",
                    "thread-wide open interrupt 目前仍通过协议索引外的 durable DB 扫描预检；它没有与单 run checkpoint 提交形成跨数据库原子事务，跨 durable run resolved 仍不支持。",
                    "服务仅监听 loopback 且没有多用户身份层；中断按 threadId 和持久化 open 状态授权，不宣称具备互联网多租户 owner 鉴权。",
                    "execution worker 的 checkpoint、outbox、operation、usage、final 和 source snapshot 写入会在 SQLite 事务中校验 owner/fence；系统仍是单机本地 SQLite/文件系统设计，不支持多主机共享文件系统或分布式共识。",
                    "过期 lease 可从非终态 durable checkpoint 取得更高 fence 恢复；ambiguous 非幂等付费 operation 仍要求人工确认，不能自动重试。",
                    "固定版本 harness 只验证上述有限适配路径；未覆盖 AG-UI 全部事件、传输 profile、state/tools/context 多轮语义或正式认证 conformance，不能声明完整 AG-UI compliant。",
                    *([agui_version_limit] if agui_version_limit else []),
                ],
            },
            {
                "id": "mcp",
                "layer": "工具调用边界",
                "title": "MCP 官方 SDK 工具服务",
                "maturity": _boundary_maturity(mcp_status, "validated-adapter"),
                "maturity_label": (
                    "有限官方客户端验证"
                    if mcp_status == "validated-adapter"
                    else "当前适配被阻断或仅作为候选，不能复用验证结论"
                ),
                "protocol": f"MCP 2025-11-25 / SDK {mcp_version}",
                "protocol_version": "2025-11-25",
                "runtime_status": mcp_runtime_status,
                "adapter_verification_status": mcp_status,
                "implementation": "仅暴露有输入上限的 search 与 fetch；默认 web provider 提供 SSRF、逐跳重定向和连接地址 pinning，正文通过游标分块读取。",
                "verified_by": [
                    "官方 stdio ClientSession 初始化、列举工具和真实调用",
                    "结构化内容、截断标记和 next_cursor 测试",
                ],
                "limitations": [
                    "六个内部角色不是 MCP 工具。",
                    "网页正文始终标记为不可信外部内容。",
                    "MCP adapter 自身只做输入边界；replay/custom provider 的安全边界必须单独验证。",
                    *([mcp_version_limit] if mcp_version_limit else []),
                ],
            },
            {
                "id": "a2a",
                "layer": "远程 Agent 边界",
                "title": "A2A 1.0 单 Agent 持久化网关",
                "maturity": _boundary_maturity(a2a_status, "validated-adapter"),
                "maturity_label": (
                    "有限官方客户端验证"
                    if a2a_status == "validated-adapter"
                    else "当前适配被阻断或仅作为候选，不能复用验证结论"
                ),
                "protocol": f"A2A 1.0 / SDK {a2a_version} / JSON-RPC",
                "protocol_version": "1.0",
                "runtime_status": a2a_runtime_status,
                "adapter_verification_status": a2a_status,
                "implementation": "整个 ResearchEngine 对外是一个 Agent；Task 使用 owner 隔离的 SQLite TaskStore，并确定性映射 durable run。",
                "verified_by": [
                    "官方 SDK Agent Card 解析与 SendMessage",
                    "网关应用重建后 GetTask/ListTasks 仍返回完成 Task 与 Artifact",
                ],
                "limitations": [
                    "当前关闭 streaming 与 push notifications。",
                    "A2A input-required 的同 Task 恢复当前为 adapter-blocked；适配器会显式返回 capability gap，不能把新 Task 当作同 Task resume。",
                    "未宣称覆盖 gRPC、HTTP+JSON 或全部 A2A 绑定。",
                    "ACP 官方项目已经并入 Linux Foundation 下的 A2A，因此不再维护平行 ACP 网关。",
                    *([a2a_version_limit] if a2a_version_limit else []),
                ],
            },
        ],
        "browser_safety": [
            "仅监听 loopback",
            "精确 Origin、Host 与端口校验",
            "拒绝 cross-site 和 same-site 写请求",
            "公开状态不包含待处理网页正文",
            "CSP、frame denial、no-sniff 与 no-store",
        ],
    }
