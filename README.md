[**中文**](README.md) | [English](README_EN.md)

# 可验证的 Deep Research Agent Harness

本项目以离线优先方式实现
`课题1-LLM-Agent深度搜索框架设计文档.md` 中的系统设计。

浏览器版中文项目汇报位于
`deliverables/苏渤涵_学术自我介绍_含智能深度调研系统.html`，其中引用的图片保存在
`deliverables/assets/`。

设计要求与代码实现的逐项对应关系，以及仍需主办方提供材料才能完成的少量事项，见
`IMPLEMENTATION_STATUS.md`。

评分依据、指标计算方法和适用边界见
`docs/评分与证据充分度说明.md`；协议支持范围见
`docs/protocol-support.md`；官方软件包与协议版本的核验记录见
`docs/官方协议版本核验_20260718.md`。该记录中的上游版本信息来自
2026-07-20 11:01 CST 保存的查询快照，服务启动时不会重新联网查询，也不等同于依赖锁文件。

项目中的协议与框架状态标签含义如下：

- `current-runtime`：当前真实运行时使用的执行路径。
- `validated-adapter`：已针对注明的官方 SDK 版本完成限定范围测试的适配器。
- `installed-but-unverified`：软件包已经安装，但版本与现有适配器证据所覆盖的版本不同。
- `not-installed`：当前 Python 解释器中未安装适配器所需的软件包，因此不能沿用已有验证证据。
- `adapter-blocked`：保留为适配目标，但仍有明确的依赖或集成问题需要解决。
- `candidate`：已完成调研，尚未安装或接入运行时的候选方案。

当前协议测试使用 CPython 3.13.9。直接适配器依赖固定为验证证据覆盖的版本：
`ag-ui-protocol==0.1.19`、`mcp==1.28.1`、`sse-starlette==2.4.1` 和
`a2a-sdk[http-server]==1.1.1`。`[mcp]` 可选依赖在 Python `<3.14` 时选择
`starlette==0.38.6`，在 Python `>=3.14` 时选择 `starlette==0.48.0`。
这些版本约束可以避免直接依赖在兼容范围内发生未记录的漂移，但当前项目尚未提供完整的
Python lock 或 constraints 文件，传递依赖和构建后端仍由依赖解析器选择。Python 3.14
分支只表示打包约束，当前没有声明 3.14 运行时或协议一致性验证结果。

## 本地安装

使用 Python 3.11 或更高版本；当前经过验证的开发解释器是 CPython 3.13.9。
如果系统中的 `python3` 仍指向 Python 3.9，请显式调用较新的解释器。

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

只在需要相应协议边界时安装已固定版本的可选适配器：

```bash
python -m pip install -e '.[mcp,a2a]'
npm ci --prefix conformance/agui
```

实际运行配置使用一个兼容 OpenAI 接口的统一网关，通过不同模型 ID 调用 Qwen、GPT 和
DeepSeek；学术检索默认使用无需密钥的 OpenAlex。OpenAlex 适合发现论文、方法、基准和
技术路线，不提供通用网页的完整覆盖。Brave 可作为需要密钥的通用网页检索增强方案；
DuckDuckGo 用于无需密钥的开发环境；replay 用于可重复的离线调试。每次运行既可以选择
单一模型进行对照，也可以使用默认的 `team` 角色路由，让多个模型按职责协作。

OpenAlex 先返回候选论文元数据，检索提供器随后优先访问每篇论文的开放获取落地页、代码
仓库、PDF 或 DOI 解析地址。标题、年份、引用数和摘要只作为发现阶段的元数据；返回的每个
URL 仍需依次通过公网地址校验、重定向校验、固定目标套接字连接、传输大小限制、页面解析、
不可变快照保存和证据绑定，之后才能用于支撑回答。当 OpenAlex 不可用或没有匹配结果时，
系统使用 arXiv 官方 Atom API 作为无需密钥的学术检索后备。Brave 用于更广泛的候选页面
发现；凭据只通过 `X-Subscription-Token` 请求头发送，不写入 URL 或缓存，并且带认证信息
的搜索请求不会自动跟随重定向。DuckDuckGo 的人机验证页面会被识别为检索异常，不会被
误判成“没有搜索结果”。如果 arXiv PDF 超过传输限制，系统会回退到同一论文的官方摘要页，
并在来源记录中明确标注这一变化。

如果本地 TUN 代理把公网域名解析到 RFC 2544 基准测试地址段（`198.18.0.0/15`），可设置
`DR_ALLOW_RFC2544_PROXY_FAKE_IP=true`。该兼容模式只接受具有非字面量公网域名的 HTTPS
URL，TLS 证书仍与原域名绑定，套接字连接仍固定到已检查的目标地址，每次重定向也会重新
校验。HTTP、直接填写的 IP 地址、localhost 和内部网络主机名仍会被拦截。不使用 TUN
Fake-IP DNS 的环境应保持该选项关闭。

## API 配置

先从仓库中的模板创建本地配置：

```bash
cp .env.example .env
```

填写统一网关信息，以及该网关实际提供的模型 ID：

```dotenv
MODEL_API_KEY=your_shared_key
MODEL_BASE_URL=https://your-gateway.example/v1
QWEN_MODEL=your-qwen-model-id
GPT_MODEL=your-gpt-model-id
DEEPSEEK_MODEL=your-deepseek-model-id
DR_MODEL_PRICING_FILE=config/model-pricing.json

DR_SEARCH_PROVIDER=openalex

DR_DEFAULT_PROFILE=team
DR_PERCEPTION_MODEL=gpt
DR_PLANNER_MODEL=gpt
DR_SCOUT_MODEL=deepseek
DR_CURATOR_MODEL=deepseek
DR_WRITER_MODEL=gpt
DR_VERIFIER_MODEL=qwen

QWEN_MODALITIES=text,document
GPT_MODALITIES=text,document,image
DEEPSEEK_MODALITIES=text,document
```

`DR_SEARCH_PROVIDER` 可取 `openalex`、`brave`、`duckduckgo` 或 `replay`。
仓库中的 `.env.example` 默认选择无需搜索密钥的 `openalex`。`brave` 需要
`DR_BRAVE_API_KEY`，部署时也兼容常见的 `BRAVE_API_KEY` 环境变量；仅在需要通用网页发现且
运行者持有密钥时使用。`duckduckgo` 用于无需密钥的开发调试，`replay` 用于结果可重复的
离线测试。

`config/model-pricing.json` 以网关返回的精确 `model_id` 为键，记录已配置 GPT 模型的文本
输入、缓存输入和输出价格，同时为 `qwen3.6-35b-a3b` 与 `deepseek-v4-flash` 明确配置
`$0` 价格。缺失价格不会被当作免费。调用超过已声明的长上下文阈值但没有提供长上下文
单价，或多模态调用的网关用量中没有按模态拆分 token 时，系统会把费用标记为部分估算，
避免给出无法核实的精确金额。

`team` 路由由 GPT 负责多模态感知、研究规划和答案写作，DeepSeek 负责查询策略和证据整理，
Qwen 负责独立引用核验。最终验收由本地确定性证据规则执行，避免让同一个模型同时生成并
批准自己的答案。首页同时提供仅使用 Qwen、GPT 或 DeepSeek 的对照配置。每次模型调用都会
保存提供器、角色别名、精确模型 ID 和实际输入模态；页面会分开展示预设路由与真实调用记录。

系统在调用模型之前，先使用 SHA-256 清单不可变地登记附件。文本、JSON、CSV 和 Markdown
在本地提取；PDF 可提取文字，也可把页面渲染成图像供视觉模型识别；图片通过兼容 OpenAI
接口的原生 `image_url` 内容块发送；只有在精确的网关与模型组合通过验证，并在
`GPT_MODALITIES` 中明确加入 `audio` 后，音频才会通过原生 `input_audio` 内容块发送。
模态声明由运行者配置，本身不能证明网关具备对应能力。运行页会展示原始附件、清单校验
结果、感知模型、内容位置和未经标定的图文对应信号。附件要成为可引用证据，还必须满足：
观察文本精确匹配、位置完整、图文对应信号不低于 `0.80`；附件自身不能替代独立外部来源。

`conformance/model-capability-verification.json` 保存当前真实图片能力探测结果，不保存网关 URL
或 API 密钥。现有记录表明，精确配置的 GPT 模型能够从生成的 PNG 中识别指定随机标识；
只有当前模型 ID 与网关 URL 的 SHA-256 都和该记录一致时，前端才将图片能力标为“已测试”。
音频能力尚未完成验证，因此默认 `GPT_MODALITIES` 中不包含 `audio`。

可通过以下命令确认配置已加载，命令不会打印 API 密钥：

```bash
PYTHONPATH=src python3 -m deep_research.cli config
```

## 交互式前端

```bash
PYTHONPATH=src python3 -m deep_research.cli serve
```

打开 `http://127.0.0.1:8000`。首页支持输入问题、上传可选的图片/音频/PDF/文本附件、选择
模型协作配置并查看历史任务。任务启动后跳转到 `/run.html?id=<run_id>`，运行页依次展示
多模态输入处理、各角色实际使用的模型、完整智能体流程、文章调研顺序、查询—来源—证据—
回答目标关系图、证据账本、最终回答、引用和指标计算方法。页面优先使用有界 SSE 状态流，
流不可用时自动回退到轮询。

独立的 `POST /api/ag-ui` 端点提供限定范围的 AG-UI HTTP/JSON/SSE 适配器；AG-UI 协议本身
不限定传输方式。固定版本的 Python/TypeScript 客户端测试覆盖有限运行、暂停、恢复、取消、
幂等、快照、事件结构与顺序，以及反向异常路径，因此该端点在注明版本下标记为
`validated-adapter`。当前验证范围是这些已测试路径，不代表 AG-UI 全量一致性认证。

适配器保留客户端传入的 `runId`，同时记录其与内部持久化任务的关联；结束事件明确区分
`success`、`interrupt` 和 `RUN_ERROR`。暂停事件包含不透明 ID 和回复数据结构，客户端可以
提交新的外部运行请求，一次性处理同一检查点的全部未决暂停并恢复任务，也可以取消这些
暂停且不启动新工作线程。固定版本客户端测试已覆盖上述有限恢复语义；更广泛的 AG-UI
语义不在当前适配器验证范围内。

服务启动后，运行以下命令执行使用精确锁定版本的官方 TypeScript 客户端验证。TypeScript
依赖图已通过 lock 文件锁定；Python 清单固定了直接协议适配器版本，但没有锁定全部传递依赖：

```bash
npm ci --prefix conformance/agui
npm test --prefix conformance/agui -- http://127.0.0.1:8000
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m pip check
```

浏览器使用的自定义 GET 状态流是 `current-runtime`。POST 适配器会持久化并重放已验证的
消息；当前 ResearchEngine 使用最后一条用户消息执行任务，尚未消费 AG-UI 的共享 `state`、
客户端工具或上下文。可选的 `parentRunId` 用于记录外部任务谱系，不作为恢复键。自定义 GET
状态流、共享状态/工具/上下文语义、更广泛的事件与传输覆盖，以及正式认证，都属于后续扩展
范围。

## 运行与评测

运行一个在线调研任务：

```bash
PYTHONPATH=src python3 -m deep_research.cli run \
  --question "Who created Python and when was it first released?"
```

不使用任何 API，运行离线回放任务：

```bash
PYTHONPATH=src python3 -m deep_research.cli run --offline \
  --question "Who created Python and when was it first released?"
```

查看一个已保存的运行：

```bash
python3 -m deep_research.cli inspect --run-id <RUN_ID>
```

生成可独立打开的可视化报告：

```bash
PYTHONPATH=src python3 -m deep_research.cli report --run-id <RUN_ID>
```

运行离线评测集：

```bash
PYTHONPATH=src python3 -m deep_research.cli eval --offline
```

评测集中的每道题都会写入独立的 `runs/eval-<task-id>/` 目录。批次汇总文件
`runs/evaluation.json` 包含输入任务、完整最终回答、已配置时的答案键判定、完成性与引用检查、
证据和引用数量、模型与搜索用量、token、估算费用、实测运行时延、失败类型及原始审计材料
路径。原始目录保留 `events.jsonl`、`final.json`、`checkpoints.sqlite`、各阶段规范化产物和
不可变来源快照。

仓库中的 `examples/tasks.jsonl` 是用于验证评测流水线的确定性冒烟测试，不代表 BrowseComp
或 GAIA 的评测成绩。当前尚未提供官方任务文件和隐藏答案判分器，因此项目不声明官方基准
分数。完整字段定义、产物目录结构和提交检查清单见 `docs/评测运行记录与复现.md`。

运行 Python 测试：

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## MCP 工具服务

状态：MCP `2025-11-25`、stdio 传输、SDK `1.28.1` 下的 `validated-adapter`。

安装可选的官方 SDK 依赖：

```bash
python3 -m pip install -e '.[mcp]'
```

启动供 MCP 客户端调用的 stdio 服务：

```bash
deep-research-mcp
```

服务只提供 `search` 和 `fetch` 两个工具，系统内部的六个逻辑智能体角色不会注册为 MCP
工具。测试通过真实 SDK 子进程完成协议版本 `2025-11-25` 的握手。`fetch` 返回有长度上限、
可通过游标继续读取的文本块，并明确附带 `untrusted_content` 标记，避免把整页内容直接注入
客户端上下文。ResearchEngine 在内部直接调用 provider 接口，不会让自身工具调用绕经 MCP
服务。浏览器、PDF、resources、prompts、sampling 等 MCP 能力尚未纳入当前验证范围。

## A2A Agent 网关

状态：A2A `1.0`、JSON-RPC `2.0`、SDK `1.1.1` 下的 `validated-adapter`。

安装并启动官方 A2A 1.0 JSON-RPC 2.0 边界：

```bash
python3 -m pip install -e '.[a2a]'
deep-research-a2a --host 127.0.0.1 --port 8010
```

添加 `--offline` 可在不调用 API 的情况下进行确定性回放。Agent Card 地址为
`http://127.0.0.1:8010/.well-known/agent-card.json`；JSON-RPC 请求发送到 `/a2a`，并携带
`A2A-Version: 1.0`。对外暴露的是一个完整的 ResearchEngine Agent，内部六个角色作为同一
研究流程协作，不拆分成六个远程 A2A Agent。A2A Task 使用按所有者隔离的 SQLite TaskStore，
路径为 `runs/a2a_tasks.sqlite3`，因此网关重启后 `GetTask` 和 `ListTasks` 仍可读取已有任务。
可通过 `--task-store PATH` 修改保存位置。

当前测试覆盖该非流式绑定的 Agent Card、SendMessage、GetTask/ListTasks 和重启持久化。
gRPC、HTTP+JSON、流式传输、推送通知及完整 A2A 操作集合尚未纳入验证范围。

## 框架与可观测性候选方案

LangGraph `1.2.9`、smolagents `1.26.0`、OpenTelemetry SDK `1.44.0`，以及 OpenTelemetry
GenAI/MCP 语义约定目前均为 `candidate`，不是当前运行时依赖，也未安装在经过验证的解释器
中。LangGraph 是生产运行时的迁移目标，smolagents 可用于轻量级基线对照，OpenTelemetry
与 W3C Trace Context 是跨协议边界追踪的目标方案。当前追踪使用应用层 ID、JSONL 和 SQLite
记录，尚未实现 W3C `traceparent`/`tracestate` 传播。

运行产物写入 `runs/<run_id>/`，包括 SQLite 操作与用量账本、事务事件、最终状态和私有来源
快照。新增模型或搜索提供器时实现 `src/deep_research/providers/base.py` 中的接口；可选 MCP 服务是面向外部
客户端的工具边界，不承担内部工作流 API 的职责。
