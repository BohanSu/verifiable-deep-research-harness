# AG-UI TypeScript Conformance

This check pins the official `@ag-ui/core` and `@ag-ui/client` packages to
`0.0.57`. It posts a real offline research request, parses the SSE framing with
`transformHttpEventStream`, validates event order with `verifyEvents`, and
checks client `runId` correlation and the terminal outcome for both a completed
run (`success`) and an evidence-incomplete run (`interrupt`). It also verifies
that a standard `resume[]` request reuses the interrupted durable run, and that
malformed lifecycle and orphan message events are rejected.

Run the Python web server first, then install the pinned packages and execute:

```bash
npm ci --prefix conformance/agui
npm test --prefix conformance/agui -- http://127.0.0.1:8000
```

This proves the tested JSON/SSE path against one pinned TypeScript release. It
does not claim every transport, every event type, CRLF framing behavior, or a
formal AG-UI certification suite.
