# Protocol Support Matrix

Reviewed against official specifications, repositories, package registries, the pinned live harness, and hash-anchored protocol receipts on 2026-07-20.

## Current Runtime Boundary

| Protocol | Current status | What exists | What must not be claimed |
|---|---|---|---|
| Internal handoff | Implemented | `deep-research-handoff/1.1`, planned routes, consumption receipts, canonical artifacts and quality gates | It is not an A2A wire object; historical 1.0 records lack receipts and recomputable artifact locations |
| AG-UI | `validated-adapter` for the tested finite adapter path (receipt checked 2026-07-20) | Custom `GET /api/runs/{id}/stream` plus a finite `POST /api/ag-ui` adapter; Python `ag-ui-protocol 0.1.19` constructs events and official TypeScript core/client `0.0.57` validate live success, interrupt, resume, cancellation, idempotency, snapshots, schema/order and negative paths | GET stream is custom; the validated path is not a claim of all AG-UI events, transports, state/tools/context semantics or formal certification |
| MCP | `validated-adapter` for the stdio search/fetch boundary (receipt checked 2026-07-20) | Official Python SDK 1.28.1 stdio server exposing `search` and `fetch` | The complete Agent runtime is not an MCP server; SSRF guarantees are provider-specific |
| A2A v1.0 | `validated-adapter` for one JSON-RPC binding (receipt checked 2026-07-20) | Official `a2a-sdk 1.1.1` gateway; one Task maps to one durable run | The six in-process roles are not six A2A Agents; streaming, push and same-Task `input-required` continuation are not implemented |

`/api/system-contract` separates SDK runtime status from adapter verification status.
`validated-adapter` is emitted only when
`conformance/protocol-validation-receipts.json` contains a fresh receipt whose
protocol/SDK versions, command and SHA-256 match
`conformance/protocol-validation-evidence-20260720.txt`; a matching installed
package alone is `adapter-blocked`. If the receipt is absent, expired or stale
after an SDK change, the UI must show the blocker instead of reusing this table's
finite-path result.

## AG-UI Target

The existing GET stream remains `ag-ui-shaped-v0`. The separate
`POST /api/ag-ui` adapter normalizes legacy-minimal requests and then validates
them as official `RunAgentInput` using `ag-ui-protocol 0.1.19`. Every emitted
event is constructed by the official Python SDK and validates against its
`Event` discriminated union. The client-provided `runId` remains the standard
lifecycle correlation ID; a separate server-generated `deep_research_run_id`
is used for filesystem-safe durable state and is exposed through `CUSTOM` audit
data and the terminal result.

```text
RUN_STARTED {threadId, runId}
STATE_SNAPSHOT {snapshot}
MESSAGES_SNAPSHOT {messages}
CUSTOM {name, value}          # optional internal audit data
RUN_FINISHED | RUN_ERROR
```

State deltas, if added, must use RFC 6902 JSON Patch in `STATE_DELTA.delta`.
The adapter maps the public research state to `STATE_SNAPSHOT.snapshot` and
places job/event audit data in `CUSTOM.value`. Its response header is
`X-Deep-Research-Protocol: ag-ui-python-sdk-validated-adapter-v4`.

`conformance/agui` pins official `@ag-ui/core` and `@ag-ui/client` `0.0.57`.
The retained harness performs real HTTP POSTs, checks `text/event-stream`, parses
framing with `transformHttpEventStream`, validates event order with
`verifyEvents`, and contains assertions for success, interrupt, resolved resume,
cancelled resume, disconnect cancellation and negative request cases. On
2026-07-20 the current service passed the complete finite run, resolved/cancelled
resume, durable-run reuse, same-runId idempotent replay, message snapshot,
schema/order and negative-case checks. The hash-anchored evidence bundle is
`conformance/protocol-validation-evidence-20260720.txt`, referenced by the
current protocol receipt. This is evidence for the pinned finite
adapter path, not every AG-UI event/transport or formal certification.
Server-side unit and transaction tests for opaque interrupt IDs, response
schemas, complete-open-set admission and worker cancellation complement the
cross-language result, but they do not expand its finite protocol boundary.

The official project does not publish a standalone conformance CLI. This harness
proves only one pinned JSON/SSE path, not all event types, CRLF framing, all
transports, or formal certification. Official-SDK-validated input messages,
including their original message IDs, are stored privately with the durable run
and replayed in later `MESSAGES_SNAPSHOT` events. The ResearchEngine still uses
only the last user message as its research question; shared `state`, client
`tools`, and `context` are schema-accepted but not yet applied. Optional
`parentRunId` is checked against the external run that produced the interrupt
and persisted with the resume receipt; resume correlation itself remains based
on `interruptId`. Interrupt IDs are random opaque tokens resolved through the
server-side SQLite mapping rather than parsed into checkpoint identities.
The exact response schema sent with an interrupt is stored beside that mapping,
so a later deployment validates an old interrupt against its original contract.

The parser and transaction layer require `resume[]` to cover the complete open
interrupt set for the thread and accept `resolved` or payloadless `cancelled`.
The current ResearchEngine still emits only one actionable interrupt per
terminal generation, so parallel heterogeneous interrupts are not yet produced.
Resolved payloads must satisfy the advertised action and bounded budget fields;
ambiguous paid retries additionally require explicit confirmation. The loopback
service has no multi-user identity layer and therefore does not claim Internet
multi-tenant owner authorization.

Worker admission uses a persistent SQLite execution lease keyed by durable run.
Each acquisition receives a random owner token and monotonically increasing
fence, with a 15-second TTL renewed by a 5-second heartbeat. Another process can
reclaim only after expiry. Heartbeat and release require the exact token/fence;
`Thread.start()` failure and worker `finally` release the lease. The in-memory job
reservation remains a local fast-path, while the existing `fcntl` run lock is a
second same-host execution guard. Execution-worker checkpoint, outbox, operation,
usage, final, and source-snapshot writes validate owner/fence inside SQLite write
transactions. This is still not a multi-host distributed scheduler: the design
uses per-run local SQLite and filesystem locks, not distributed consensus or
shared-network-filesystem locking semantics.

`runs/agui_protocol.sqlite3` is the thread-level control-plane registry for
external AG-UI runs. `run_id` is a global primary key; an identical request hash
can replay the registered durable run, while any different request reusing that
ID receives `409`. The registry also records thread, producer/resume kind,
declared `parentRunId`, durable correlation and terminal status. Actual consumed
interrupt lineage remains in the per-run receipt and interrupt records.

The remaining atomicity boundary is explicit: thread-wide open interrupts are
still discovered across per-run durable databases before the single-run resume
transaction. This prevents partial resume in normal operation but is not an
atomic cross-database snapshot. A thread with open interrupts in multiple
durable runs is rejected rather than partially resumed; cross-run full cancel or
coordinated multi-run resume requires moving interrupts/receipts into the central
control plane or an outbox coordinator.

Official references:

- https://docs.ag-ui.com/concepts/architecture
- https://docs.ag-ui.com/concepts/events
- https://github.com/ag-ui-protocol/ag-ui
- https://www.npmjs.com/package/@ag-ui/core/v/0.0.57
- https://www.npmjs.com/package/@ag-ui/client/v/0.0.57

## MCP Target

`deep-research-mcp` starts an official FastMCP stdio server exposing only
`search` and `fetch`. Integration tests launch it as a subprocess, negotiate
`protocolVersion: 2025-11-25`, execute `tools/list`, call `search`, validate
`structuredContent`, and verify a clean process exit. Stdout is owned by the SDK
protocol transport; tool implementation logging must remain on stderr.

The MCP adapter validates bounded tool inputs. Network egress protection belongs
to the selected provider: the default web provider performs public-address
validation, validates every redirect, pins the checked address to the socket
connection, blocks proxy tunnelling and rejects private or abnormal targets.
Replay is offline. A custom provider does not inherit the default provider's
SSRF guarantee; it must declare `supports_ssrf_guard=true` and provide
`validate_public_url` before the adapter will call `fetch`.

Tool inputs have explicit query/URL/title/strategy/source-type limits. `fetch`
never returns an unbounded document: it defaults to 6,000 characters, caps each
chunk at 12,000, and returns `cursor`, `returned_chars`, `total_chars`,
`truncated`, and `next_cursor`. Every chunk carries `untrusted_content=true`;
clients must treat it as source data rather than instructions.

This is the intended MCP boundary. Planner, Critic, Writer and the complete
ResearchEngine are not exposed as MCP tools. A future Streamable HTTP transport
requires separate Origin/session/version tests and is not currently claimed.

Official references:

- https://modelcontextprotocol.io/specification/2025-11-25
- https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle
- https://modelcontextprotocol.io/specification/2025-11-25/basic/transports
- https://modelcontextprotocol.io/specification/2025-11-25/server/tools

## A2A Boundary

`deep-research-a2a` exposes the complete `ResearchEngine` as one remote Agent
through the official `a2a-sdk 1.1.1` protobuf types and Starlette JSON-RPC
routes. The Agent Card advertises one `JSONRPC` interface at protocol version
`1.0`, with `streaming=false` and `pushNotifications=false`. One A2A Task maps
deterministically to one durable `run_id`; Planner through Verifier handoffs
remain private implementation details.

The integration test fetches `/.well-known/agent-card.json`, sends a real
`SendMessage` request with `A2A-Version: 1.0`, and verifies the terminal Task,
Artifact, status message, internal run ID, and non-probability metadata. A
project SQLite implementation of the official `TaskStore` abstraction stores
owner-scoped Task protobufs. A second application instance then retrieves and
lists the completed Task through the official client, proving gateway-restart
persistence for the tested JSON-RPC path. The claim remains limited to this
A2A 1.0 binding, not every transport or a full conformance suite. Same-Task
`input-required` continuation remains an explicit `adapter-blocked` capability
gap; clients must submit a new Task instead of treating a new Task as a resume.

Official references:

- https://a2a-protocol.org/latest/specification/
- https://a2a-protocol.org/latest/whats-new-v1/
- https://github.com/a2aproject/A2A

## Claim Rules

Until official SDK types and conformance tests are present, documentation and UI
must use these exact labels:

- `GET custom SSE; POST pinned Python + TypeScript finite adapter, fixed resume conformance validated`
- `MCP official Python SDK stdio boundary (search/fetch only)`
- `A2A 1.0 JSON-RPC gateway (official SDK, non-streaming)`
- `deep-research-handoff/1.1 internal schema (planned route + receipt + canonical artifact)`

The project must not use `compatible` or `compliant` for AG-UI, MCP, or A2A
based only on similarly named fields or endpoints. Claims must name the exact
SDK, binding, protocol version, transport, and tested behavior.
