# Implementation Status

This file audits the current implementation against
`课题1-LLM-Agent深度搜索框架设计文档.md`.

Protocol/framework labels are calibrated as follows: `current-runtime` is the
actual execution path; `validated-adapter` is a limited path tested with named
official SDKs and a fresh hash-anchored receipt; `adapter-blocked` preserves a
target with a concrete blocker; `candidate` is researched but not installed or
wired. Upstream versions below are a 2026-07-20 snapshot, not a live check or a
Python dependency lock. The runtime contract fails closed when the receipt is
missing, expired, or inconsistent with the installed SDK/lock versions.

## Implemented

- Explicit, serializable research state and deterministic orchestration loop.
- Executable node cursor with persisted pending queries, fetched pages, repair
  gaps, and evidence/closure/draft/verification revisions. Resume continues
  from the checkpoint successor instead of restarting the whole loop.
- Per-run SQLite execution lease assigns a random owner token and monotonically
  increasing fence with TTL/heartbeat. Every execution-worker checkpoint,
  outbox, operation, usage, final, source-snapshot, and canonical-artifact write
  validates the active owner/fence inside its SQLite write transaction. The
  existing `fcntl` run lock remains a second same-host guard.
- Six real in-process logical Agent objects for planning, retrieval strategy,
  evidence curation, closure criticism, citation-constrained writing, and
  sentence-level verification. Every invocation records status, attempt,
  timing, chronological log predecessor, execution/replay mode, provider-call
  count, explicitly consumed handoff IDs, input/output summary, artifacts, and
  quality-gate outcomes. Log adjacency is not presented as a causal parent.
- A role-routed model team uses GPT for perception/planning/writing, DeepSeek
  for query strategy/evidence curation, and Qwen for citation verification.
  The Critic remains a deterministic local hard gate. Every model invocation
  records provider, selected alias, exact model ID and input modalities; replay
  records preserve the original model provenance while recording zero new
  provider calls.
- Durable multimodal ingress stores each attachment in a private
  content-addressed path with an independently revalidated SHA-256/byte-length
  manifest. Image and rendered-PDF perception uses native `image_url` parts,
  audio uses native `input_audio`, and text/document extraction remains local.
  Each attachment has an independent idempotent perception operation and
  produces page/region/time-located observations before the six-role chain.
  Attachment evidence fails closed unless it has an exact observation-text
  match, locator, perception model and grounding signal of at least 0.80; the
  signal is explicitly not treated as a calibrated truth probability.
- `deep-research-handoff/1.1` envelopes distinguish intended route from actual
  consumption. A later invocation must emit a receipt referencing the original
  message ID before the UI labels it consumed. Stage outputs are immutable
  canonical JSON files with content URI, byte length, canonicalization version,
  recomputable SHA-256, idempotency key, and explicit gate result.
- DeepSeek OpenAI-compatible provider with JSON mode, schema repair, raw-response
  cache, token accounting, and configurable cost estimation. Hidden provider
  retries are disabled. Interrupted reads, successful-but-invalid responses,
  and paid-response cache-write failures become `ProviderOutcomeUncertain`, so
  the operation remains ambiguous and cannot automatically repeat a billable call.
- No-key OpenAlex scholarly discovery with official arXiv fallback, optional
  Brave Web Search API candidate discovery with header-only credentials and no
  authenticated redirects, canonical URLs, static HTML extraction, PDF
  extraction through `pdftotext`, keyless DuckDuckGo development fallback, and
  local replay search.
- Answer-slot planning, dynamic gap-driven query generation, exact and
  near-duplicate query filtering.
- Concurrent search and page fetch under hard iteration/call/page budgets.
- Evidence Ledger with quote grounding, content deduplication, canonical URLs,
  normalized-body hashes, SimHash near-duplicate detection, registrable-domain
  publisher grouping, origin clusters, source reliability priors, stance, and
  per-evidence provenance snapshots.
- HTML provenance extraction records `rel=canonical`, Schema.org/JSON-LD
  publisher and author declarations, OpenGraph site name, citation/isBasedOn,
  and syndication upstream URLs. These self-declared signals only merge
  potentially dependent sources; they never prove publisher identity or
  independence, and the frontend exposes the raw signals for human review.
- Evidence Closure over slot coverage, layered origin clusters, exact quote
  grounding, source-type policy priors, and conflict workflow status.
- Per-slot hard-gate audits expose effective source count, authoritative-source
  exception use, exact quote grounding, executed contradiction checks,
  conflict candidates, evidence IDs, and human-readable failure reasons.
- Claim-quote consistency checks require matching numeric/date values, negation
  polarity, and minimum lexical or Chinese-bigram coverage. This deterministic
  gate is exposed as a rule check, not a calibrated entailment probability.
- Contradiction searches count only after results are fetched and inspected;
  zero-result, search-failed, and fetch-failed attempts remain explicit failed
  states and cannot unlock writing.
- Writing is blocked unless all Evidence Closure hard gates pass. Completion
  additionally requires aligned evidence, closure, draft, and verification
  revisions, preventing stale drafts from being delivered after evidence changes.
- Public methodology endpoint that exposes every heuristic weight and states
  explicitly that evidence scores are not calibrated truth probabilities.
- Versioned metric contracts defining each displayed metric's numerator,
  denominator, algorithm, decision role, hard gates, and limitations.
- `evidence-closure-v4.19` now records the actual required-slot aggregation:
  slot-wise means, supporting-evidence max/min operators, and the distinction
  between diagnostic confidence and mandatory provenance/contradiction gates.
- Fetch evidence coverage is fail-closed: its denominator is the complete
  loaded set of successful immutable Fetch records with a valid server binding;
  its numerator requires a closure-admitted Evidence row with the same unique
  `fetch_record_id`. Partial audit pages, missing IDs, source-wide URL matches,
  and excluded Evidence rows cannot inflate the ratio.
- Numeric/year candidate consensus to distinguish conflicting values such as
  “development began in 1989” versus “first release in 1991”.
- Citation-constrained drafting, citation verification, and one targeted
  citation-repair round.
- Error taxonomy and failure-directed recovery metadata.
- SQLite checkpoints, transactional outbox, model operation ledger, per-operation
  usage ledger, file-locked and crash-tail-repairing JSONL event projection,
  final artifacts, resume, cancellation,
  model/search counters, and HTML reports. Completed model responses replay without
  another provider call; ambiguous dispatched requests fail closed.
- Search and fetch use per-query/per-canonical-URL idempotent operations with
  persisted physical attempt counts. Completed results replay through idempotent
  state reducers; budgets are reconstructed from SQLite instead of checkpoint-only counters.
- Immutable per-run source snapshots are stored with private permissions and a
  SHA-256 integrity endpoint. The frontend can open the captured text and highlight
  every quote used by the Evidence Ledger.
- Failed, cancelled, verification-failed, and evidence-incomplete runs expose an
  explicit resume action with bounded budget extensions. Ambiguous paid operations
  require affirmative user confirmation before their ledger row is unlocked.
- Source snapshots with discovery/fetch timestamps, final redirect URL, HTTP
  status, MIME type, parser version, byte count, content hash, and cache status.
- CLI for run, inspect, eval, report, config, and serve.
- Versioned batch evaluation records include the sanitized input task, complete
  delivered answer, answer-key/closure/citation results, evidence and citation
  counts, tool-event totals, model/search calls, tokens, estimated cost,
  wall-clock latency, failure categories, dataset SHA-256, and portable links
  to each run's raw events, checkpoints, artifacts, and source snapshots. A
  failed task is recorded without aborting the remaining dataset, and reused
  runs are explicitly separated from fresh end-to-end latency measurements.
- Interactive responsive frontend with a separate submission page and research
  dossier page, result-first completed state, six-role live phase rail,
  invocation/replay and planned-route/receipt visualizations, per-Agent and
  per-invocation audit dialogs, recomputable canonical-artifact inspection,
  path-focused query-source-evidence-target graph, ordered article journey,
  source snapshot inspection, evidence ledger, hard-gate metrics, token/cost
  accounting, citation navigation, run history, restore, and cancellation.
- `[current-runtime]` Custom bounded GET SSE state snapshots with automatic
  polling fallback. The `ag-ui-shaped-v0` payload is not an AG-UI wire format.
- `[validated-adapter]` `POST /api/ag-ui` normalizes legacy-minimal requests and validates official
  `RunAgentInput` with `ag-ui-protocol 0.1.19`; every emitted event is created
  by official Python SDK classes. The adapter preserves the client `runId`,
  exposes a separate filesystem-safe durable run ID, uses explicit success or
  interrupt outcomes, cancels background work on client disconnect, and emits
  `RUN_ERROR` for writable post-header failures. `conformance/agui` pins the
  official TypeScript core/client `0.0.57` and validates real HTTP SSE framing,
  schema, order, success, evidence-incomplete interrupt, resolved/cancelled
  resume, durable-run reuse, idempotent replay, message snapshots and negative
  cases on the pinned finite path. A resolved Fieldnote interrupt resumes the
  same durable run through the shared atomic budget/paid-operation safety gate.
  GET SSE remains custom; shared state,
  tools and context are not yet applied to ResearchEngine semantics. Full input
  messages and parent external-run lineage are persisted and replayed. Only the
  last user message currently drives ResearchEngine; `parentRunId` records
  lineage and is not the resume key.
- `[validated-adapter]` Official MCP Python SDK 1.28.1 stdio server exposed through
  `deep-research-mcp`, with bounded `search` and a `fetch` path protected by the
  default network provider's SSRF checks. Replay is offline; custom providers
  must declare `supports_ssrf_guard=true` and `validate_public_url` and do not
  inherit the default provider guarantee.
  A subprocess integration test negotiates protocol `2025-11-25`, lists tools,
  calls tools, and validates structured content. ResearchEngine itself calls its
  provider interfaces directly; browser/PDF/files are not current MCP tools.
- MCP query, strategy, subgoal, URL, source-type and title inputs have explicit
  contracts. Fetch text is cursor-paginated with a 12,000-character hard cap,
  total/returned length, truncation and next-cursor fields, and an explicit
  untrusted-content marker.
- `[validated-adapter]` Official A2A SDK 1.1.1 gateway exposed through
  `deep-research-a2a`. It uses A2A protocol `1.0` over JSON-RPC `2.0`; one Task maps to
  one durable research run, and an owner-scoped SQLite TaskStore preserves
  protobuf Tasks across gateway restarts. Official SDK tests validate the
  completed Task, Artifact, status message, outcome metadata, and post-restart
  `GetTask`/`ListTasks`. Streaming and push notifications are explicitly disabled;
  same-Task `input-required` continuation remains an explicit `adapter-blocked`
  capability gap; gRPC, HTTP+JSON and the complete A2A operation set are not claimed.
- Versioned `/api/system-contract` capability registry owns the protocol claims
  rendered by the frontend, including dynamically read installed SDK versions,
  a code-owned upstream version snapshot, receipt-backed adapter verification,
  maturity labels, and limitations. It does not query upstream registries online.
  Static HTML remains only as a network-failure fallback. Current receipts are
  anchored by `conformance/protocol-validation-evidence-20260720.txt`.
- Browser/API state uses a public projection that removes pending page bodies.
  Mutation checks validate loopback Host, exact Origin including port, and
  Fetch Metadata; cross-site and cross-port writes are rejected.
- Resume budgets are persisted as absolute limits and extend from the greater
  of the previous limit or actual consumption. All ambiguous non-idempotent
  operations are discovered from SQLite and require one explicit confirmation.
- A worker whose lease expires can resume a nonterminal durable checkpoint only
  after acquiring a higher fence. In-memory jobs carry owner/fence identity, so
  a stale worker cannot overwrite a newer generation's visible or protocol state.
- SSE connection limits, mutation Origin/Host checks, CSP, no-sniff, no-referrer,
  frame denial, and no-store JSON responses.
- Offline deterministic development/evaluation path requiring no APIs.
- Automated unit/integration-level tests for core closure, conflicts, all six
  Agent invocations, cancellation, persistence, DeepSeek parsing/usage and
  verifier omissions, SSRF/path traversal, cache corruption, recovery, and
  reports.

## Requires External Assets

These cannot be truthfully marked complete until the organizers provide the
corresponding assets or an explicit replacement is selected:

- `[adapter-blocked]` Adapter for the organizer-provided Simple Agent framework. The framework is
  not present in the workspace; the current `ModelProvider` protocol is the
  intended integration boundary.
- Official BrowseComp and GAIA task files/hidden-answer judges. The batch
  evaluator and JSONL task format exist, but official evaluation data has not
  been supplied.
- Reproducible benchmark result tables and ablation numbers. These require the
  official dataset, fixed model/search budget, and multiple completed runs.

## Optional Production Extensions

These are intentionally not required for the two-week research prototype:

- Serper/Tavily/Bing provider for higher search stability and SLA.
- Headless browser automation for JavaScript-only sites, logins, and CAPTCHA.
- PostgreSQL/distributed worker deployment and multi-user authentication.
- `[candidate]` MCP adapters for production search, browser, PDF, and file tools. The current
  provider protocols remain the compatibility baseline.
- `[candidate]` LangGraph `1.2.9` runtime replacement. It is not installed. The
  current state machine implements checkpoint and routing semantics without that
  dependency; migration must preserve operation ledger, side-effect idempotency,
  ambiguous paid-call handling, and lease/fence guarantees.
- `[candidate]` smolagents `1.26.0` as a lightweight Agent baseline. It is not
  installed; untrusted CodeAgent execution requires a real sandbox.
- `[candidate]` OpenTelemetry SDK `1.44.0`, GenAI/MCP semantic conventions and
  W3C Trace Context for cross-boundary tracing. They are not installed or
  instrumented; current observability is JSONL/SQLite correlation.
- `[candidate]` Python 3.14 protocol extras. The `[mcp]` extra now selects
  `starlette>=0.48.0,<1` on Python 3.14 (and the older range below 3.14), but
  the current verified interpreter snapshot is CPython 3.13.9 and no 3.14
  install/runtime or protocol regression has been run.

## Current Verification

- A real native `image_url` probe passed against the current shared gateway and
  exact GPT model: the model recognized the generated image nonce and returned
  a located observation. The non-secret, model/gateway-bound receipt is
  `conformance/model-capability-verification.json`. Audio has not passed an
  external `input_audio` probe and remains disabled by default.
- 338 automated tests pass, including model/search/fetch operation replay,
  ambiguous operation refusal, injected crashes after fetch/draft/verification,
  cross-domain same-origin rejection, authoritative-prior rejection,
  failed contradiction search, strict citation sets, Chinese sentence parsing,
  run question binding, fence loss, nonterminal crash recovery, concurrent
  outbox flush, crash-tail repair, canonical artifact recomputation, and stale
  worker exclusion.
- Real OpenAlex search and one OpenAlex-selected arXiv page fetch have been
  exercised successfully, along with real DuckDuckGo search. OpenAlex
  integration is covered by response parsing, no-key configuration and hardened
  fetch-path tests. Brave integration remains covered by request-format,
  cache-isolation, missing-credential and hardened fetch-path tests; no operator
  Brave credential is included in the repository or test suite.
- Real DeepSeek structured planning has been exercised successfully.
- A real end-to-end task completed with web evidence, closure, final answer,
  and citation verification.
- On 2026-07-21, persisted run `completed-team-20260721` completed the full
  planner/scout/curator/critic/writer/verifier workflow with real GPT,
  DeepSeek, and Qwen model calls through the shared gateway. Its durable state
  records `status=completed`, `next_node=done`, all required closure gates
  passed, citation verification passed, 11 model calls, 7,803 input tokens,
  and 2,785 output tokens. Search/fetch used the deterministic replay provider,
  so this run proves multi-model orchestration rather than live-web freshness.
- Browser acceptance on 2026-07-21 verified a decodable local blob image
  preview, six completed role nodes, an explicit `(350, 260)` orchestration
  rotation center, an outer-routed `critic -> scout` repair edge, all three
  model labels, the final answer, and the passed hard-gate summary.
- Web history, submit, poll, restore, and cancellation endpoints have been
  exercised against a running local server.
- Source snapshot integrity and manual resume endpoints were exercised against
  real persisted runs. On 2026-07-20 the AG-UI endpoint passed the pinned
  official TypeScript live harness over HTTP for completed (`success`) and
  evidence-incomplete (`interrupt`) first-run paths, resolved/cancelled resume,
  durable-run reuse, same-runId idempotent replay, message snapshots, negative
  schema/event ordering and duplicate-run cases. This validates the finite
  pinned adapter path; it does not claim full AG-UI conformance.
- A2A 1.0 Agent Card and JSON-RPC 2.0 `SendMessage` were exercised through the
  official SDK routes with the required `A2A-Version: 1.0` header.
- The 2026-07-20 finite protocol results are recorded in
  `conformance/protocol-validation-evidence-20260720.txt`; the three runtime
  labels are accepted only through the matching
  `conformance/protocol-validation-receipts.json` SHA-256 receipts.
