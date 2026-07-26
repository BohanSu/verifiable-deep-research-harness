import { EventSchemas } from "@ag-ui/core";
import {
  runHttpRequest,
  transformHttpEventStream,
  verifyEvents,
} from "@ag-ui/client";
import { lastValueFrom, of, toArray } from "rxjs";

const baseUrl = (process.argv[2] || "http://127.0.0.1:8000").replace(/\/$/, "");
const threadId = `ts-conformance-thread-${Date.now()}`;

async function collectEvents(body) {
  const fetchStream = async () => {
    const response = await fetch(`${baseUrl}/api/ag-ui`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Origin": baseUrl,
        "Sec-Fetch-Site": "same-origin",
      },
      body: JSON.stringify(body),
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok || !contentType.startsWith("text/event-stream")) {
      const detail = await response.text();
      throw new Error(
        `Expected HTTP 200 text/event-stream, received ${response.status} ${contentType}: ${detail}`,
      );
    }
    const adapter = response.headers.get("x-deep-research-protocol");
    if (adapter !== "ag-ui-python-sdk-validated-adapter-v4") {
      throw new Error(`Unexpected adapter header: ${adapter}`);
    }
    return response;
  };

  return lastValueFrom(
    transformHttpEventStream(runHttpRequest(fetchStream)).pipe(
      verifyEvents(),
      toArray(),
    ),
  );
}

function summarizeRun(name, runId, events, expectedOutcome, expectedResult) {
  if (events[0]?.type !== "RUN_STARTED" || events.at(-1)?.type !== "RUN_FINISHED") {
    throw new Error(
      `AG-UI stream does not have the required lifecycle boundaries: first=${events[0]?.type || "none"}, last=${events.at(-1)?.type || "none"}, types=${events.map((event) => event.type).join(",")}, terminal=${JSON.stringify(events.at(-1))}`,
    );
  }
  if (events.some((event) => event.runId && event.runId !== runId)) {
    throw new Error("Server did not preserve the client runId in lifecycle events");
  }
  const terminal = events.at(-1);
  if (terminal?.outcome?.type !== expectedOutcome) {
    throw new Error(
      `Expected ${expectedOutcome} outcome, got ${JSON.stringify(terminal?.outcome)}`,
    );
  }
  if (terminal?.result?.outcome !== expectedResult) {
    throw new Error(`Expected result ${expectedResult}, got ${terminal?.result?.outcome}`);
  }
  if (expectedOutcome === "interrupt") {
    const interrupt = terminal.outcome.interrupts?.[0];
    if (!interrupt?.responseSchema?.required?.includes("action")) {
      throw new Error("Interrupt does not expose an actionable responseSchema");
    }
    const stateIndex = events.findLastIndex((event) => event.type === "STATE_SNAPSHOT");
    const messagesIndex = events.findLastIndex(
      (event) => event.type === "MESSAGES_SNAPSHOT",
    );
    if (stateIndex < 0 || messagesIndex < 0 || stateIndex > events.length - 2 || messagesIndex > events.length - 2) {
      throw new Error("Interrupt was not preceded by state and message snapshots");
    }
  }
  const audit = events.find(
    (event) =>
      event.type === "CUSTOM" &&
      event.name === "deep_research_audit" &&
      event.value?.deep_research_run_id,
  );
  if (!audit) {
    throw new Error("Stream does not expose the internal durable run correlation");
  }
  const messageSnapshot = [...events]
    .reverse()
    .find((event) => event.type === "MESSAGES_SNAPSHOT");
  return {
    name,
    validated_events: events.length,
    event_types: events.map((event) => event.type),
    protocol_run_id: runId,
    deep_research_run_id: audit.value.deep_research_run_id,
    result: terminal.result.outcome,
    outcome: terminal.outcome.type,
    interrupt_id: terminal.outcome.interrupts?.[0]?.id || null,
    resumed_from_interrupt: audit.value.resumed_from_interrupt || null,
    message_ids: (messageSnapshot?.messages || []).map((message) => message.id),
  };
}

async function validateLiveRun({ name, question, expectedOutcome, expectedResult }) {
  const runId = `ts-${name}-${Date.now()}`;
  const events = await collectEvents({
    threadId,
    runId,
    messages: [
      {
        id: `ts-${name}-message`,
        role: "user",
        content: question,
      },
    ],
    state: {},
    tools: [],
    context: [],
    forwardedProps: { offline: true },
  });
  return summarizeRun(name, runId, events, expectedOutcome, expectedResult);
}

async function validateResume(interruptedRun) {
  const runId = `ts-resume-${Date.now()}`;
  const request = {
    threadId,
    runId,
    parentRunId: interruptedRun.protocol_run_id,
    messages: [],
    state: {},
    tools: [],
    context: [],
    forwardedProps: {},
    resume: [
      {
        interruptId: interruptedRun.interrupt_id,
        status: "resolved",
        payload: {
          action: "continue_research",
          additionalIterations: 1,
          additionalSearchCalls: 2,
          additionalPages: 3,
        },
      },
    ],
  };
  const events = await collectEvents(request);
  const resumed = summarizeRun(
    "resume-insufficient-evidence",
    runId,
    events,
    "interrupt",
    "evidence_incomplete",
  );
  if (resumed.deep_research_run_id !== interruptedRun.deep_research_run_id) {
    throw new Error("AG-UI resume created a new durable run instead of reusing checkpoint");
  }
  if (resumed.resumed_from_interrupt !== interruptedRun.interrupt_id) {
    throw new Error("AG-UI resume audit does not identify the resolved interrupt");
  }
  if (!resumed.message_ids.includes("ts-insufficient-evidence-message")) {
    throw new Error("AG-UI resume did not restore the original message snapshot");
  }
  const budgetBeforeReplay = await fetch(
    `${baseUrl}/api/runs/${resumed.deep_research_run_id}`,
  ).then((response) => response.json()).then((value) => value.state.budget_limits);
  const replayEvents = await collectEvents(request);
  const replay = summarizeRun(
    "resume-idempotent-replay",
    runId,
    replayEvents,
    "interrupt",
    "evidence_incomplete",
  );
  const budgetAfterReplay = await fetch(
    `${baseUrl}/api/runs/${resumed.deep_research_run_id}`,
  ).then((response) => response.json()).then((value) => value.state.budget_limits);
  if (JSON.stringify(budgetBeforeReplay) !== JSON.stringify(budgetAfterReplay)) {
    throw new Error("Idempotent AG-UI resume replay changed the approved budget");
  }
  if (replay.interrupt_id !== resumed.interrupt_id) {
    throw new Error("Idempotent AG-UI resume replay created another interrupt");
  }
  return [resumed, replay];
}

async function validateCancellation(interruptedRun) {
  const runId = `ts-cancel-interrupt-${Date.now()}`;
  const events = await collectEvents({
    threadId,
    runId,
    parentRunId: interruptedRun.protocol_run_id,
    messages: [],
    state: {},
    tools: [],
    context: [],
    forwardedProps: {},
    resume: [
      {
        interruptId: interruptedRun.interrupt_id,
        status: "cancelled",
      },
    ],
  });
  const cancelled = summarizeRun(
    "cancel-interrupt",
    runId,
    events,
    "success",
    "interrupt_cancelled",
  );
  if (cancelled.deep_research_run_id !== interruptedRun.deep_research_run_id) {
    throw new Error("Interrupt cancellation lost durable run correlation");
  }
  if (!cancelled.message_ids.includes("ts-insufficient-evidence-message")) {
    throw new Error("Interrupt cancellation did not preserve message history");
  }
  const cancellationAudit = events.find(
    (event) =>
      event.type === "CUSTOM" &&
      event.name === "deep_research_audit" &&
      event.value?.worker_started === false,
  );
  if (!cancellationAudit) {
    throw new Error("Cancellation stream did not prove that no worker was started");
  }
  if (
    cancellationAudit.value.checkpoint_id_before !==
    cancellationAudit.value.checkpoint_id_after
  ) {
    throw new Error("Interrupt cancellation changed the durable checkpoint");
  }
  if (cancellationAudit.value.open_interrupt_count !== 0) {
    throw new Error("Interrupt cancellation left open interrupts behind");
  }
  return cancelled;
}

const successfulRun = await validateLiveRun({
  name: "success",
  question: "Who created Python and when was it first released?",
  expectedOutcome: "success",
  expectedResult: "completed",
});
const interruptedRun = await validateLiveRun({
  name: "insufficient-evidence",
  question: "What is the exact serial number of the private qzxv unpublished prototype?",
  expectedOutcome: "interrupt",
  expectedResult: "evidence_incomplete",
});
const resumedRuns = await validateResume(interruptedRun);
const cancelledRun = await validateCancellation(resumedRuns[0]);

let duplicateRunIdRejected = false;
try {
  await collectEvents({
    threadId: `${threadId}-other`,
    runId: successfulRun.protocol_run_id,
    messages: [
      {
        id: "duplicate-run-message",
        role: "user",
        content: "This is a different request reusing an existing runId",
      },
    ],
    state: {},
    tools: [],
    context: [],
    forwardedProps: { offline: true },
  });
} catch (error) {
  duplicateRunIdRejected = String(error).includes("globally registered");
}
if (!duplicateRunIdRejected) {
  throw new Error("Global external runId registry accepted a conflicting request");
}

const missingRunId = EventSchemas.safeParse({
  type: "RUN_STARTED",
  threadId: "thread-only",
});
if (missingRunId.success) {
  throw new Error("Official EventSchemas unexpectedly accepted missing runId");
}

let orphanRejected = false;
try {
  await lastValueFrom(
    of({
      type: "TEXT_MESSAGE_CONTENT",
      messageId: "orphan-message",
      delta: "orphan content",
    }).pipe(verifyEvents(), toArray()),
  );
} catch {
  orphanRejected = true;
}
if (!orphanRejected) {
  throw new Error("Official verifyEvents accepted orphan message content");
}

console.log(
  JSON.stringify({
    live_runs: [successfulRun, interruptedRun, ...resumedRuns, cancelledRun],
    negative_schema_case: "rejected",
    negative_sequence_case: "rejected",
    duplicate_external_run_id_case: "rejected",
  }),
);
