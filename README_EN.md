[中文](README.md) | [**English**](README_EN.md)

# Verifiable Deep Research Harness

An offline-first implementation of the design in
`课题1-LLM-Agent深度搜索框架设计文档.md`.

The browser-based Chinese project presentation is
`deliverables/苏渤涵_学术自我介绍_含智能深度调研系统.html`; its referenced images
are kept in `deliverables/assets/`.

See `IMPLEMENTATION_STATUS.md` for the exact design-to-code audit and the few
remaining items that require organizer-provided assets.

The scoring assumptions and calibration boundary are documented in
`docs/评分与证据充分度说明.md`.
Protocol support and non-compliance boundaries are documented in
`docs/protocol-support.md`.
The official package/spec verification record is
`docs/官方协议版本核验_20260718.md`. Its upstream values are the snapshot
queried on 2026-07-20 at 11:01 CST, not an online check performed at server
startup and not a dependency lock.

Protocol/framework status labels have strict meanings:

- `current-runtime`: the actual execution path.
- `validated-adapter`: a limited path tested against named official SDK versions.
- `installed-but-unverified`: a package is installed, but its version differs
  from the version covered by the adapter evidence.
- `not-installed`: the expected adapter package is absent from the running
  interpreter, so its evidence cannot be reused.
- `adapter-blocked`: a retained target with a concrete dependency or integration blocker.
- `candidate`: researched but not installed or wired into the runtime.

The current protocol test interpreter snapshot is CPython 3.13.9. The direct
adapter dependencies are pinned to the versions covered by the evidence:
`ag-ui-protocol==0.1.19`, `mcp==1.28.1`, `sse-starlette==2.4.1`, and
`a2a-sdk[http-server]==1.1.1`; the `[mcp]` extra selects
`starlette==0.38.6` on Python `<3.14` and `starlette==0.48.0` on Python
`>=3.14`. These pins prevent direct-package range drift without claiming a
fully locked Python environment: there is still no Python lock/constraints
file, and transitive dependencies plus the build backend remain resolver
selected. The Python 3.14 branch is a packaging constraint, not a 3.14
runtime or protocol-conformance validation.

## Local setup

Use Python 3.11 or newer; the current verified development interpreter is
CPython 3.13.9. On systems where `python3` still points to Python 3.9, invoke
the newer interpreter explicitly.

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp .env.example .env
```

Install the pinned optional adapters only when their boundaries are needed:

```bash
python -m pip install -e '.[mcp,a2a]'
npm ci --prefix conformance/agui
```

The real configuration uses one OpenAI-compatible gateway with Qwen, GPT, and
DeepSeek model IDs, plus no-key OpenAlex scholarly discovery by default.
OpenAlex is appropriate for papers, methods, benchmarks and technical routes,
not as a claim of general-web coverage. Brave remains an optional credentialed
web-search enhancement; DuckDuckGo remains a keyless development option and
replay remains available for deterministic offline debugging. A run can use one
model for comparison or the default role-routed `team` profile.

OpenAlex provides candidate work metadata without a key, then the provider
prefers an open-access landing page, repository, PDF or DOI resolver for each
work. Its title, year, citation count and abstract are discovery metadata only:
every returned URL still passes the existing public-URL validation, redirect
checks, pinned socket connection, transport limits, parser, immutable snapshot
and evidence-grounding path before it can support an answer. If OpenAlex is
unavailable or has no matching work, the provider uses the official arXiv Atom
API as a no-key scholarly fallback. Brave provides optional broader candidate
discovery; its credential is sent only in `X-Subscription-Token`, is never put
in a URL or cache, and authenticated search requests do not follow redirects.
DuckDuckGo challenge pages are not treated as empty search results in its
development provider. Oversized arXiv PDFs fall back to the same paper's
official abstract page with an explicit provenance signal.

If a local TUN proxy resolves public domains into RFC 2544 benchmarking
addresses (`198.18.0.0/15`), set `DR_ALLOW_RFC2544_PROXY_FAKE_IP=true`. This is
a narrowly scoped compatibility mode, not a general private-network allowlist:
it accepts only HTTPS URLs with non-literal public-style domain names, keeps TLS
certificate validation bound to that domain, pins the checked destination for
the socket connection, and revalidates every redirect. HTTP, literal IP URLs,
localhost, and private/internal hostnames remain blocked. Leave it disabled on
hosts that do not use TUN Fake-IP DNS.

## API configuration

Create the local configuration from the checked-in template:

```bash
cp .env.example .env
```

Fill the shared gateway fields and the exact model IDs exposed by that gateway:

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

`DR_SEARCH_PROVIDER` accepts `openalex`, `brave`, `duckduckgo`, or `replay`.
The checked-in `.env.example` defaults to `openalex`, which needs no search key.
`brave` requires `DR_BRAVE_API_KEY` (the common `BRAVE_API_KEY` environment
variable is also accepted for deployment compatibility); use it only when
general-web discovery is needed and an operator key is available. Use
`duckduckgo` only for keyless development or `replay` for deterministic offline
tests.

`config/model-pricing.json` is keyed by the exact gateway `model_id`. It
contains the configured GPT text, cached-input and output prices plus explicit
`$0` entries for `qwen3.6-35b-a3b` and `deepseek-v4-flash`. A missing rate is
not treated as free. Calls above a declared long-context threshold without a
numeric long-context price, and multimodal calls whose gateway usage lacks a
per-modality token breakdown, are reported as partial estimates rather than
fabricated exact costs.

The `team` route uses GPT for multimodal perception, planning and writing,
DeepSeek for query strategy and evidence curation, and Qwen for independent
citation verification. Closure Critic is deliberately a local deterministic
evidence hard gate so no model approves its own output. The home page also
offers Qwen-only, GPT-only and DeepSeek-only comparison profiles. Every
invocation persists its provider, alias, exact model ID and observed input
modalities; configuration routes and actual calls are shown separately.

Attachments are stored with an immutable SHA-256 manifest before any model is
called. Text, JSON, CSV and Markdown are extracted locally; PDF text is
extracted and pages can be rendered for visual perception; images use native
OpenAI-compatible `image_url` parts; audio uses native `input_audio` parts when
the exact gateway/model has been verified and `audio` is explicitly added to
`GPT_MODALITIES`. Modality declarations are operator configuration, not proof
that a gateway supports them. The run page exposes the original attachment,
manifest result, perception model, locator and uncalibrated grounding signal.
Attachment evidence additionally requires an exact observation-text match,
locator and grounding signal of at least `0.80`, and still cannot satisfy an
independent-source gate by itself.

`conformance/model-capability-verification.json` records the current real
image probe without storing the gateway URL or API key. The probe passed for
the exact configured GPT model and recognized a nonce from a generated PNG;
the UI labels image as tested only while both the model ID and SHA-256 of the
configured gateway URL still match that receipt. Audio remains unverified and
is therefore absent from the default `GPT_MODALITIES` declaration.

Verify that configuration was loaded without printing the key:

```bash
PYTHONPATH=src python3 -m deep_research.cli config
```

## Interactive UI

```bash
PYTHONPATH=src python3 -m deep_research.cli serve
```

Open `http://127.0.0.1:8000`. The landing page accepts a question, optional
image/audio/PDF/text attachments and a model profile, then shows run history.
Starting a task redirects to `/run.html?id=<run_id>`, where multimodal ingress,
the actual per-role model route, the full agent workflow, article research
order, query-source-evidence-target graph, evidence ledger, answer, citations,
and methodology are visualized. The run page prefers a bounded SSE state stream
and falls back to polling if the stream is unavailable.

The separate `POST /api/ag-ui` endpoint is a limited AG-UI HTTP/JSON/SSE
adapter; AG-UI itself is transport-independent. The fixed-version
Python/TypeScript client harness validates the finite first-run, interrupt, resume, cancellation,
idempotency, snapshot, schema/order, and negative paths, so this adapter is
`validated-adapter` at the named versions. This is not a claim of full AG-UI
conformance or formal certification.
It preserves the client `runId`, exposes the internal durable run correlation,
uses explicit `success`, `interrupt`, or `RUN_ERROR` terminal semantics, and
emits actionable interrupts with opaque IDs and response schemas. A new external
run may resolve the complete open interrupt set to resume the same checkpoint,
or cancel the set without starting another worker; these finite resume semantics
are covered by the locked client validation, while broader AG-UI semantics remain
outside the adapter boundary.
Run the precisely locked official TypeScript client validation while the server
is active. The TypeScript dependency graph is locked; the Python manifest pins
the direct protocol adapters but does not lock their transitive closure:

```bash
npm ci --prefix conformance/agui
npm test --prefix conformance/agui -- http://127.0.0.1:8000
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m pip check
```

The custom browser GET stream is `current-runtime`, not AG-UI. The POST adapter
persists and replays validated messages, but ResearchEngine currently uses only
the last user message and does not apply shared `state`, client tools or context.
Optional `parentRunId` records external lineage and is not the resume key. The
adapter does not claim complete AG-UI compliance. The custom GET stream,
shared state/tools/context semantics, broader event/transport coverage, and
formal certification remain outside the validated finite path.

## Run

```bash
PYTHONPATH=src python3 -m deep_research.cli run \
  --question "Who created Python and when was it first released?"
```

Run without any API:

```bash
PYTHONPATH=src python3 -m deep_research.cli run --offline \
  --question "Who created Python and when was it first released?"
```

Inspect a saved run:

```bash
python3 -m deep_research.cli inspect --run-id <RUN_ID>
```

Generate a standalone visual report:

```bash
PYTHONPATH=src python3 -m deep_research.cli report --run-id <RUN_ID>
```

Run the offline evaluation set:

```bash
PYTHONPATH=src python3 -m deep_research.cli eval --offline
```

Each dataset item gets an isolated `runs/eval-<task-id>/` directory. The batch
summary at `runs/evaluation.json` includes the input task, complete final answer,
answer-key result when configured, closure and citation checks, evidence and
citation counts, model/search usage, tokens, estimated cost, observed wall-clock
latency, failure categories, and paths to the raw audit artifacts. The raw
directory retains `events.jsonl`, `final.json`, `checkpoints.sqlite`, canonical
stage artifacts, and immutable source snapshots.

The checked-in `examples/tasks.jsonl` is a deterministic smoke test for the
evaluation pipeline. It is not a BrowseComp or GAIA result. Official task files
and hidden-answer judges have not been provided, so no official benchmark score
is claimed. See `docs/评测运行记录与复现.md` for the exact field definitions,
artifact layout, and submission checklist.

Run tests:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## MCP tool server

Status: `validated-adapter` for MCP `2025-11-25` over stdio with SDK `1.28.1`.

Install the optional official SDK dependency:

```bash
python3 -m pip install -e '.[mcp]'
```

Start the stdio server used by MCP clients:

```bash
deep-research-mcp
```

It exposes only `search` and `fetch`. The internal six logical Agent roles are
not MCP tools. Tests perform a real SDK subprocess handshake using protocol
version `2025-11-25`. `fetch` returns bounded, cursor-based text chunks with an
explicit `untrusted_content` marker rather than injecting an entire page into
the client context. The ResearchEngine calls provider interfaces directly; it
does not route its internal tool calls through this MCP server. Browser, PDF,
resources, prompts, sampling and other MCP capabilities are not currently covered.

## A2A Agent gateway

Status: `validated-adapter` for A2A `1.0` over JSON-RPC `2.0` with SDK `1.1.1`.

Install and start the official A2A 1.0 JSON-RPC 2.0 boundary:

```bash
python3 -m pip install -e '.[a2a]'
deep-research-a2a --host 127.0.0.1 --port 8010
```

Use `--offline` for deterministic replay without an API. The Agent Card is at
`http://127.0.0.1:8010/.well-known/agent-card.json`; JSON-RPC requests are sent
to `/a2a` with `A2A-Version: 1.0`. The complete ResearchEngine is one remote
Agent. Its six internal roles are intentionally not exposed as six A2A Agents.
A2A Task records use an owner-scoped SQLite TaskStore at
`runs/a2a_tasks.sqlite3`, so `GetTask` and `ListTasks` continue to work after a
gateway restart. Override the location with `--task-store PATH`.

The tested scope is Agent Card, SendMessage, GetTask/ListTasks and restart
persistence for this non-streaming binding. It does not cover gRPC, HTTP+JSON,
streaming, push notifications or the complete A2A operation set.

## Framework and observability candidates

LangGraph `1.2.9`, smolagents `1.26.0`, OpenTelemetry SDK `1.44.0` and the
OpenTelemetry GenAI/MCP semantic conventions are `candidate`, not current
runtime dependencies. They are not installed in the verified interpreter.
LangGraph remains the production-runtime migration target, smolagents remains a
lightweight baseline option, and OTel plus W3C Trace Context remains the target
for cross-boundary tracing. Current tracing is limited to application IDs and
JSONL/SQLite records; no W3C `traceparent`/`tracestate` propagation is implemented.

Runtime artifacts are written to `runs/<run_id>/`, including SQLite operation
and usage ledgers, transactional events, final state, and private source
snapshots. Add model/search providers through `src/deep_research/providers/base.py`; the optional
MCP server is an external tool boundary rather than the internal workflow API.
