# 架构选型与协议 ADR

日期：2026-07-20 11:01 CST（基于官方规范、仓库与注册表的版本快照复核）

## 1. 决策摘要

| 层级 | 选择 | 不选择 | 原因 |
|---|---|---|---|
| 内部协作 | 类型化 `HandoffEnvelope` + artifact schema | A2A over HTTP | 六个角色当前位于同一进程，A2A 会引入不必要的分布式失败面 |
| 工作流运行时 | 当前显式持久化节点游标 + pending artifacts；LangGraph 作为可选生产运行时 | 只保存整份 state 后从头重跑 | 当前实现已支持节点后继恢复；LangGraph `1.2.9` 是未安装的迁移候选，采用前必须证明 checkpointer 能保留 operation ledger、副作用幂等和 lease/fence 语义 |
| 工具接入 | 官方 MCP SDK stdio server（当前 search/fetch） | 把工具伪装成远程 Agent | MCP 正确覆盖工具边界；其他工具需逐项增加 schema 与测试 |
| 远程 Agent | 官方 A2A SDK `1.1.1` 的 A2A 1.0 over JSON-RPC 2.0 gateway | 内部每阶段都使用 A2A | 整个 ResearchEngine 作为一个远程 Agent，内部六角色继续使用类型化交接；ACP 官方项目已并入 A2A，不再平行接入 |
| 前端事件 | 自定义 GET SSE + polling fallback；POST 使用固定版本的 AG-UI Python + 精确锁定的 TypeScript SDK 适配 | 把受限 POST 或自定义 GET 夸大为完整兼容 | 当前环境 Python `0.1.19` 构造事件，TypeScript core/client `0.0.57` 的固定 live harness 已覆盖首轮 SSE/终态/interrupt、resume、取消、幂等重放和消息快照；完整事件/传输/state/tools/context 语义与正式认证未覆盖，消息会持久化/回放但仅最后一条用户消息进入研究语义 |
| 观测 | 当前 JSONL + SQLite；OpenTelemetry GenAI conventions + W3C Trace Context 作为生产目标 | 把应用 `run_id` 叫作 W3C trace，或声称已跨边界埋点 | 当前没有 OTel 依赖、span、exporter 或 `traceparent`/`tracestate` 传播；设计目标保留为后续候选 |
| 当前无 key 搜索 | OpenAlex 主、官方 arXiv fallback；Brave/Exa/Tavily 作为有凭据的扩展 | DuckDuckGo HTML 作为正式评测来源 | OpenAlex 和 arXiv 可返回可追溯的学术候选；DuckDuckGo HTML 无 SLA 且依赖非官方 DOM |
| 静态抓取 | 安全异步 fetch + Trafilatura | 裸 `urllib` + 通用 HTMLParser | 需要 SSRF 防护、连接池、正文质量和 locator |
| 动态网页 | 隔离容器 Playwright，静态失败后单次降级 | 主进程直接运行浏览器 | 减少攻击面和成本 |
| 证据停止 | 原子声明硬门控 | 可相互补偿的总分单独闭包 | 单个高分项不能补偿缺引用或未解决冲突 |

### 1.1 状态语义与当前校准

本文使用以下六个状态，不允许用候选版本号替代实现证据：

- `current-runtime`：当前默认执行路径直接使用，并有本项目测试或持久化运行证据。
- `validated-adapter`：通过指定官方 SDK/客户端验证且有新鲜、可重算 protocol receipt 的有限适配路径；不等于完整协议 conformance。
- `installed-but-unverified`：历史状态词，表示包已安装但版本不同于适配证据覆盖的版本；当前能力契约统一以 `adapter-blocked` 呈现，不能复用该适配结论。
- `not-installed`：历史状态词，表示期望包不在当前解释器；当前能力契约以 `candidate` 呈现，不能复用该适配结论。
- `adapter-blocked`：设计目标保留，但当前受版本漂移、缺失/过期回执、缺失外部资产或尚未实现的必要语义阻断。
- `candidate`：已完成一手资料选型，尚未安装、导入或接入当前运行时。

| 对象 | 状态 | 当前边界 |
|---|---|---|
| 显式 async 状态机、SQLite checkpoint/ledger/lease、内部 handoff、JSONL/SQLite 观测、自定义 GET SSE | `current-runtime` | 当前 ResearchEngine 的实际执行和审计路径 |
| A2A `1.0` / `a2a-sdk 1.1.1` | `validated-adapter`（receipt 2026-07-20） | 仅 JSON-RPC 2.0、单 Agent、非流式、无 push；同 Task `input-required` 恢复是 `adapter-blocked` capability gap；不是全部 A2A 绑定 |
| MCP `2025-11-25` / `mcp 1.28.1` | `validated-adapter`（receipt 2026-07-20） | 仅 stdio `search`/`fetch`；当前 ResearchEngine 仍直接调用 provider，不经 MCP；自定义 provider 不继承默认 SSRF 保证 |
| AG-UI Python `0.1.19` + TS core/client `0.0.57` | `validated-adapter`（receipt 2026-07-20） | 固定 live harness 已测 POST JSON/SSE 首轮、终态、resolved/cancelled resume、durable 复用、幂等重放、消息快照和负例；完整事件/传输/state/tools/context 语义与正式认证未覆盖，GET SSE 是私有格式 |
| 完整 AG-UI 语义与 Python 3.14 协议 extra | `adapter-blocked` / `candidate` | state/tools/context、多轮推理和完整事件/传输未覆盖；Python 3.14 依赖已精确条件化，但本机没有 3.14 运行证据 |
| LangGraph `1.2.9`、smolagents `1.26.0` | `candidate` | 当前解释器均未安装，代码也未导入；分别保留为生产 runtime 迁移和轻量 baseline 候选 |
| OpenTelemetry SDK `1.44.0`、GenAI/MCP semconv、W3C Trace Context | `candidate` | 当前未安装、未埋点、未传播；GenAI/MCP semantic conventions 仍为 `Development` |

有限适配的 `validated-adapter` 标签还必须通过
`conformance/protocol-validation-receipts.json` 与
`conformance/protocol-validation-evidence-20260720.txt` 的协议版本、SDK
版本、日期、命令和 SHA-256 校验；仅安装同版本 SDK 不会自动变成已验证能力。

`pyproject.toml` 对已验证的直接适配器使用精确 pin：AG-UI Python
`0.1.19`、MCP `1.28.1`、SSE Starlette `2.4.1`、A2A `1.1.1`，并按
Python 版本固定 MCP 使用的 Starlette `0.38.6`/`0.48.0`。这只消除直接
包的解析范围漂移；没有 Python lock/constraints 文件时，传递依赖和
build-system 仍由 resolver 选择，不能称为完整环境复现。

## 2. 协议边界

```text
Browser
  ↕ custom GET SSE (`ag-ui-shaped-v0`, current-runtime, not AG-UI compatible)
Research Workflow Runtime
  ↕ typed HandoffEnvelope (in-process)
Planner → Scout → Curator → Critic → Writer → Verifier
  ↕ direct provider interfaces
Search / Fetch

External protocol adapters (status differs by boundary):
  POST /api/ag-ui ↔ AG-UI JSON/SSE finite adapter (fixed resume paths validated)
  MCP stdio ↔ search / fetch only
  A2A 1.0 over JSON-RPC 2.0 ↔ complete ResearchEngine as one remote Agent

Current observability: JSONL + SQLite correlation.
Target observability: OpenTelemetry + W3C Trace Context (candidate, not implemented).
```

### 2.1 A2A

A2A v1.0 提供 Agent Card、任务生命周期和 artifact，并定义 JSON-RPC、gRPC、HTTP+JSON 绑定，适用于独立 Agent 服务。当前实现使用官方 `a2a-sdk 1.1.1` 的 protobuf 类型和 JSON-RPC routes；这里的 A2A 协议版本是 `1.0`，底层 JSON-RPC 版本是 `2.0`。Agent Card 仅声明该 JSON-RPC binding、非流式、无 push。一个 A2A Task 通过确定性映射对应一个持久化 `run_id`，最终返回研究答案或明确的证据失败状态。

六角色共享同一个模型 provider 和进程，不宣称为 A2A 网络多 Agent。A2A Task 使用实现官方 `TaskStore` 抽象的 owner-scoped SQLite 存储；Task protobuf、状态、Artifact 与历史在网关重启后仍可通过官方客户端 `GetTask`/`ListTasks` 查询。研究 checkpoint、来源快照和证据账本继续由每个 durable run 的 SQLite/文件系统持久化。已验证的是 Agent Card、SendMessage、GetTask/ListTasks 与应用重建后的这一条非流式 JSON-RPC 路径；不宣称覆盖全部 JSON-RPC 方法、gRPC、HTTP+JSON、流式或 push。`input-required` 的同 Task 恢复当前仍是 `adapter-blocked` capability gap，适配器会显式返回该限制，不能用新 Task 伪装成旧 Task resume。

### 2.2 MCP

MCP 用于模型/Agent 到工具和资源的连接。2026-07-20 官方 `latest` 仍重定向到 `2025-11-25` 规范，基础协议使用 JSON-RPC 2.0。搜索、抓取、浏览器、PDF、文件读取可作为后续 MCP 工具/资源候选，但当前 server 只有 search/fetch；Planner、Critic、Writer 是长程决策角色，不应伪装成普通 MCP tool。

当前 `deep-research-mcp` 使用官方 Python SDK `mcp 1.28.1` 的 FastMCP stdio transport，真实测试完成 `initialize(2025-11-25) → tools/list → tools/call(search)`，并覆盖 fetch 分页等本项目契约。只宣称独立对外的 search/fetch 工具适配器经过验证；ResearchEngine 当前仍直接调用 `SearchProvider`，不经 MCP，也不宣称内部六角色 MCP-compliant。默认网络 provider 的 SSRF/重定向/socket pinning 保护不自动扩散到 replay 或 custom provider；custom provider 必须显式提供 `supports_ssrf_guard=true` 与 `validate_public_url`。

### 2.3 AG-UI

AG-UI 适合 Agent backend 到交互前端，能够标准化生命周期、状态变化、工具活动和人类介入。当前已经提供有界 SSE 与 polling fallback；POST 适配路径使用固定官方 SDK 并通过带 SHA-256 receipt 的有限 live conformance，GET 事件仍标记为 `ag-ui-shaped-v0`，不宣称完整协议符合或正式认证。若回执缺失、过期或版本漂移，运行时契约立即降为 `adapter-blocked`。

AG-UI 协议本身传输无关；官方标准 HTTP/`HttpAgent` profile 使用 `POST RunAgentInput` 并返回 `BaseEvent` 流，run 以 `RUN_STARTED` 开始、以 `RUN_FINISHED`/`RUN_ERROR` 结束，`STATE_SNAPSHOT` 使用 `snapshot` 字段。当前 `GET /api/runs/{id}/stream` 的 `state/events/job` 组合是项目自定义载荷，不能通过改名宣称兼容。

`POST /api/ag-ui` 会先把旧版最小请求补齐为官方必填字段，再使用 `ag-ui-protocol 0.1.19` 的 `RunAgentInput` 验证；输出由官方 `RunStartedEvent`、`StateSnapshotEvent`、`MessagesSnapshotEvent`、`CustomEvent`、`RunFinishedEvent` 和 `RunErrorEvent` 构造。客户端 `runId` 作为标准生命周期关联 ID 原样返回，服务端另生成安全的 `deep_research_run_id` 管理 checkpoint。完成状态使用 `success`，可由用户继续处理的证据不足、引用失败、取消和费用不确定状态使用带 `responseSchema` 的 `interrupt`，基础设施失败使用 `RUN_ERROR`。客户端断开会触发协作式取消。interrupt ID 是随机 opaque token，只通过 SQLite 中的 thread/open-state 映射定位 durable run，不再编码内部 run ID。新的外部 run 必须用完整 `resume[]` 覆盖当前 thread 的全部 open interrupt；`resolved` 恢复同一 durable run，payloadless `cancelled` 原子消费中断且不启动 worker。可选 `parentRunId` 只校验并记录外部运行谱系，不作为恢复关联键。响应头标识 `ag-ui-python-sdk-validated-adapter-v4`，表示固定版本有限适配路径已验证，不表示完整 AG-UI conformance。

`conformance/agui` 精确锁定官方 `@ag-ui/core` 与 `@ag-ui/client` `0.0.57`，用 `transformHttpEventStream + verifyEvents` 解析真实 HTTP SSE，覆盖成功和证据不足两种首轮终态、resolved/cancelled resume、durable run 复用、同 runId 幂等重放、runId 关联、内部 durable run 关联、中断前状态/消息快照、响应 schema、事件顺序和负例。该 harness 验证的是固定版本有限适配路径，不覆盖全部 AG-UI 事件、传输 profile、state/tools/context 多轮语义或正式认证，因此仍不使用笼统的 `AG-UI compliant` 描述。GET SSE 继续明确标为项目自定义；完整输入消息会持久化并回放，但 ResearchEngine 仍只把最后一条用户消息作为问题，`state`、`tools`、`context` 尚未进入研究语义，当前引擎也尚未产生并行异构 interrupt。`parentRunId` 只校验并记录谱系，不是恢复键。服务仅面向 loopback，没有互联网多租户 owner 鉴权。

内部 `HandoffEnvelope` 的 schema version 使用 `deep-research-handoff/1.1` 命名空间。`consumer/intended_consumer` 只表示编排器选择的计划路由；只有后续 invocation 产生并按 `message_id` 引用的 receipt 才证明消费。每个阶段产物保存为不可变 canonical JSON，并记录内容地址、字节数、规范化规则和可重算 SHA-256。历史 1.0 记录没有这些证明字段，前端会明确标为不可验证。该 schema 不是 A2A v1.0 `Message`、`Task` 或 `Artifact` wire object。

### 2.4 ACP 与 ANP

- IBM/BeeAI ACP 官方仓库已经明确宣布 ACP 并入 Linux Foundation 下的 A2A；其 README 所指历史迁移指南在 2026-07-20 复核时为 404，因此本决策只依赖仍可验证的官方合并公告。项目不新增平行 ACP 依赖；需要跨远程 Agent 时统一采用 A2A。
- Zed Agent Client Protocol 面向编辑器/编码 Agent，不适用本项目。
- ANP 面向去中心化公开 Agent 网络，当前无此需求，只作为远期实验项。

### 2.5 框架与观测候选

- LangGraph 官方 PyPI 当前为 `1.2.9`，smolagents 当前为 `1.26.0`；二者在本项目 `/opt/miniconda3/bin/python` 环境均未安装，源码也未导入。LangGraph 保留为生产编排迁移候选，smolagents 保留为轻量 Agent baseline/对照候选。
- smolagents 的 `CodeAgent` 设计目标不变，但官方当前明确说明 `LocalPythonExecutor` 不是安全边界；任何非可信代码执行必须使用真正的容器或远程沙箱。
- OpenTelemetry SDK 当前为 `1.44.0`、Python semantic-conventions 包为 `0.65b0`；独立 GenAI/MCP semantic conventions 的文档状态仍为 `Development`。当前代码没有这些依赖或 span，不得把 `run_id`/应用 `trace_id` 描述成 W3C Trace Context。
- W3C Trace Context 当前发布版仍是 `W3C Recommendation 23 November 2021`，定义 `traceparent`/`tracestate`；Baggage 是独立的 `Candidate Recommendation Snapshot 30 May 2024`。二者均是目标传播规范，不是当前实现事实。
- 当前解释器快照为 CPython `3.13.9`。项目声明允许 Python `>=3.11`，并已为 `[mcp]` extra 按 Python 版本精确选择 Starlette `0.38.6` 或 `0.48.0`；本机没有 Python 3.14 安装、运行和协议回归证据，因此不把依赖可解析写成 3.14 支持证明。

## 3. 证据与评分决策

Evidence Closure 总分只作为诊断展示，不再单独决定停止。每个必需原子声明必须满足：

1. 引用 ID 存在且 quote 能在不可变 source snapshot 中定位。
2. claim 与 quote 通过数字覆盖、否定极性和词项/中文二元组覆盖规则门；这是确定性一致性检查，不宣称完整语义蕴含。最终回答仍由独立 Verifier 逐句验收。
3. 回答中的每个事实声明均出现在 verifier 输出中。
4. 没有未解决高置信反证。
5. 争议声明需要两个真实独立来源，或一个第一方原始来源加一个独立二手来源。
6. 至少执行一次反证搜索，未搜索时冲突状态为 `unknown`，不是 `resolved`。

来源初始等级仅用于搜索排序，不作为事实真伪概率。

## 4. 运行时决策

当前运行时已经持久化 `next_node`、pending query/page/gap 和 evidence/closure/draft/verification revision，并通过 run lease 阻止同一 run 并发执行。Planner、Scout 查询生成、Curator、Writer、Verifier 以及逐 query 搜索、逐 URL 抓取均已接入 SQLite operation ledger。模型 operation 对未知结果 fail-closed；幂等 GET 搜索/抓取使用同一语义 key 增加 attempt 后重试。成功结果在 reducer/checkpoint 前崩溃时从 ledger 回放，不再次访问 provider。checkpoint 与 outbox event 在同一事务提交，JSONL 只是可重建投影。

DeepSeek provider 不再在内部隐藏重试。网络超时或连接中断会使 operation 保持 `started` 并转为 `ambiguous_operation`，系统为避免重复计费而暂停；收到明确 HTTP 错误才允许后续显式重试。已付费的原始响应在 JSON 解析前写入模型 cache，解析修复不会重复请求。

```text
plan → query → retrieve → extract → closure
                         ↘ repair ↗
closure → draft → verify → finalize
              ↘ targeted repair ↗
```

边界仍需准确说明：第三方模型 provider 若不支持幂等键，请求发出后进程立即死亡仍无法证明是否计费。因此当前可称为“持久化成功结果 exactly-once replay + 模型未知结果 fail-closed + GET 工具 at-least-once attempt”，不能笼统声称端到端 exactly-once。

## 5. 安全决策

- Web API 只接受 JSON，限制 body/question 长度，验证 Host/Origin，限制并发并服务端生成 run ID。
- SSE 使用独立并发槽；DELETE 复用 Host/Origin 校验；静态页面发送 CSP、`nosniff`、拒绝 frame 嵌入和 no-referrer。
- 所有 outbound URL 执行逐跳 SSRF 检查，禁止私网、loopback、link-local、metadata、URL credentials 和异常端口。
- hostile webpage 始终标记为 untrusted data，不允许改变系统策略或工具。
- `.env` 使用 `0600`，run/cache 目录使用 `0700`，文件采用原子写。
- PDF 和浏览器在隔离环境运行；未隔离前限制大小、CPU、超时和页面数。

## 6. 主要依据

- A2A specification: https://a2a-protocol.org/latest/specification/
- A2A Linux Foundation repository: https://github.com/a2aproject/A2A
- A2A SDK PyPI: https://pypi.org/pypi/a2a-sdk/json
- MCP specification 2025-11-25: https://modelcontextprotocol.io/specification/2025-11-25
- MCP SDK PyPI: https://pypi.org/pypi/mcp/json
- AG-UI: https://docs.ag-ui.com/
- AG-UI Python PyPI: https://pypi.org/pypi/ag-ui-protocol/json
- AG-UI TypeScript core: https://registry.npmjs.org/@ag-ui%2fcore/latest
- AG-UI TypeScript client: https://registry.npmjs.org/@ag-ui%2fclient/latest
- SSE Starlette PyPI: https://pypi.org/pypi/sse-starlette/json
- Starlette PyPI: https://pypi.org/pypi/starlette/json
- OpenTelemetry GenAI conventions: https://github.com/open-telemetry/semantic-conventions-genai
- OpenTelemetry MCP conventions: https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/mcp.md
- W3C Trace Context: https://www.w3.org/TR/trace-context/
- W3C Baggage: https://www.w3.org/TR/baggage/
- LangGraph persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph PyPI: https://pypi.org/pypi/langgraph/json
- smolagents PyPI: https://pypi.org/pypi/smolagents/json
- smolagents repository: https://github.com/huggingface/smolagents
- OWASP SSRF Prevention: https://cheatsheetseries.owasp.org/cheatsheets/Server_Side_Request_Forgery_Prevention_Cheat_Sheet.html
- OWASP Prompt Injection Prevention: https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html
- RFC 9309 Robots Exclusion Protocol: https://www.rfc-editor.org/rfc/rfc9309.html
- RFC 9111 HTTP Caching: https://www.rfc-editor.org/rfc/rfc9111.html
- FActScore: https://aclanthology.org/2023.emnlp-main.741/
- ALCE: https://arxiv.org/abs/2305.14627
- SciFact: https://aclanthology.org/2020.emnlp-main.609/
- TRUE: https://aclanthology.org/2022.naacl-main.287/
