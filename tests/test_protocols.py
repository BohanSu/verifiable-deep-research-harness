import unittest
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import AsyncMock, patch
import httpx
from pydantic import TypeAdapter
from google.protobuf.timestamp_pb2 import Timestamp
from ag_ui.core import Event as AgUiEvent
from a2a.client.client import ClientConfig
from a2a.client.client_factory import create_client
from a2a.server.context import ServerCallContext
from a2a.types import (
    GetTaskRequest,
    ListTasksRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.types import a2a_pb2
from a2a.utils.errors import InvalidParamsError

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from deep_research.protocols.agui import (
    custom_audit,
    messages_snapshot,
    parse_run_agent_input,
    parse_run_agent_input_detailed,
    run_finished,
    run_started,
    state_snapshot,
)
from deep_research.protocols.a2a_gateway import create_app, run_id_for_task
from deep_research.protocols.a2a_task_store import SQLiteTaskStore
from deep_research.schemas import Page
from deep_research.state import ResearchState
from deep_research.storage import RunStore


class AgUiAdapterTest(unittest.TestCase):
    def test_parses_minimal_run_agent_input(self) -> None:
        thread_id, run_id, question = parse_run_agent_input(
            {
                "threadId": "thread-1",
                "runId": "run-1",
                "messages": [{"role": "user", "content": "Research this"}],
            }
        )
        self.assertEqual((thread_id, run_id, question), ("thread-1", "run-1", "Research this"))

    def test_event_mapping_uses_agui_field_names(self) -> None:
        events = [
            run_started("t", "r"),
            state_snapshot({"status": "running"}),
            messages_snapshot(
                [{"id": "message-1", "role": "user", "content": "Research this"}]
            ),
            custom_audit({"events": 2}),
            run_finished("t", "r", {"outcome": "completed"}, success=True),
        ]
        adapter = TypeAdapter(AgUiEvent)
        validated = [adapter.validate_python(event) for event in events]
        self.assertEqual(events[0]["type"], "RUN_STARTED")
        self.assertEqual(events[1]["snapshot"]["status"], "running")
        self.assertEqual(events[2]["type"], "MESSAGES_SNAPSHOT")
        self.assertEqual(events[3]["name"], "deep_research_audit")
        self.assertEqual(events[4]["outcome"]["type"], "success")
        self.assertEqual(len(validated), 5)

    def test_non_successful_research_uses_interrupt_outcome(self) -> None:
        event = run_finished(
            "thread-1",
            "client-run-1",
            {"outcome": "evidence_incomplete"},
            interrupt_reason="evidence_incomplete",
            interrupt_message="More independent evidence is required.",
        )
        validated = TypeAdapter(AgUiEvent).validate_python(event)
        self.assertEqual(event["runId"], "client-run-1")
        self.assertEqual(event["outcome"]["type"], "interrupt")
        self.assertEqual(
            event["outcome"]["interrupts"][0]["reason"],
            "evidence_incomplete",
        )
        self.assertEqual(
            event["outcome"]["interrupts"][0]["responseSchema"]["required"],
            ["action"],
        )
        self.assertEqual(validated.type, "RUN_FINISHED")

    def test_rejects_missing_question(self) -> None:
        with self.assertRaises(ValueError):
            parse_run_agent_input({"threadId": "thread-1", "messages": []})

    def test_malformed_messages_raise_input_error_not_type_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "RunAgentInput.runId is required"):
            parse_run_agent_input({"threadId": "thread-1", "messages": None})

    def test_resume_input_does_not_require_a_new_user_message(self) -> None:
        thread_id, run_id, question = parse_run_agent_input(
            {
                "threadId": "thread-1",
                "runId": "resume-run-1",
                "resume": [
                    {
                        "interruptId": "int:v1:opaque",
                        "status": "resolved",
                    }
                ],
            }
        )
        self.assertEqual((thread_id, run_id, question), ("thread-1", "resume-run-1", ""))

    def test_detailed_input_preserves_validated_message_history_and_ids(self) -> None:
        parsed = parse_run_agent_input_detailed(
            {
                "threadId": "thread-history",
                "runId": "run-history",
                "messages": [
                    {"id": "user-1", "role": "user", "content": "Question"},
                    {"id": "assistant-1", "role": "assistant", "content": "Draft"},
                    {"id": "user-2", "role": "user", "content": "Research this"},
                ],
            }
        )

        self.assertEqual(parsed.question, "Research this")
        self.assertEqual(
            [message["id"] for message in parsed.messages],
            ["user-1", "assistant-1", "user-2"],
        )
        self.assertEqual(parsed.messages[1]["role"], "assistant")


class McpServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["DR_SEARCH_PROVIDER"] = "replay"
        from deep_research.protocols import mcp_server

        mcp_server._search_provider = None
        self.server = mcp_server

    async def asyncTearDown(self) -> None:
        os.environ.pop("DR_SEARCH_PROVIDER", None)
        self.server._search_provider = None

    async def test_official_sdk_lists_search_and_fetch_tools(self) -> None:
        tools = await self.server.mcp.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"search", "fetch"})
        self.assertTrue(all(tool.inputSchema for tool in tools))

    async def test_official_sdk_calls_search_tool(self) -> None:
        result = await self.server.mcp.call_tool(
            "search",
            {"query": "Python creator official history", "limit": 2},
        )
        self.assertIsInstance(result, tuple)
        _, structured = result
        self.assertEqual(structured["count"], 2)

    async def test_fetch_returns_bounded_untrusted_chunks_with_cursor(self) -> None:
        class LongPageProvider:
            supports_ssrf_guard = True

            @staticmethod
            def validate_public_url(url: str) -> None:
                if not url.startswith("https://example.org/"):
                    raise ValueError("unexpected URL")

            async def fetch(self, _result):
                return Page(
                    url="https://example.org/long",
                    title="Long source",
                    text="A" * 13_500,
                    content_hash="hash",
                )

        self.server._search_provider = LongPageProvider()
        first = await self.server.mcp.call_tool(
            "fetch",
            {"url": "https://example.org/long", "max_chars": 5_000},
        )
        _, first_page = first
        self.assertEqual(len(first_page["text"]), 5_000)
        self.assertTrue(first_page["untrusted_content"])
        self.assertTrue(first_page["truncated"])
        self.assertEqual(first_page["next_cursor"], 5_000)
        self.assertEqual(first_page["total_chars"], 13_500)

        second = await self.server.mcp.call_tool(
            "fetch",
            {
                "url": "https://example.org/long",
                "cursor": first_page["next_cursor"],
                "max_chars": 5_000,
            },
        )
        _, second_page = second
        self.assertEqual(second_page["cursor"], 5_000)
        self.assertEqual(second_page["next_cursor"], 10_000)

    async def test_mcp_inputs_reject_unknown_strategy_and_oversized_chunk(self) -> None:
        with self.assertRaises(ToolError):
            await self.server.mcp.call_tool(
                "search", {"query": "test", "strategy": "invented"}
            )
        with self.assertRaises(ToolError):
            await self.server.mcp.call_tool(
                "fetch",
                {"url": "https://example.org", "max_chars": 99_999},
            )

    async def test_stdio_lifecycle_and_tool_call(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = "src"
        environment["DR_SEARCH_PROVIDER"] = "replay"
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "deep_research.protocols.mcp_server"],
            env=environment,
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                self.assertEqual(initialized.protocolVersion, "2025-11-25")
                tools = await session.list_tools()
                self.assertEqual({tool.name for tool in tools.tools}, {"search", "fetch"})
                result = await session.call_tool(
                    "search", {"query": "Python creator", "limit": 1}
                )
                self.assertFalse(result.isError)
                self.assertEqual(result.structuredContent["count"], 1)


class A2AGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.task_store_path = Path(self.tempdir.name) / "a2a-tasks.sqlite3"
        self.runs_dir = Path(self.tempdir.name) / "runs"
        self.env_patch = patch.dict(
            os.environ,
            {"DR_RUNS_DIR": str(self.runs_dir)},
        )
        self.env_patch.start()

    async def asyncTearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    async def test_official_sdk_routes_card_and_jsonrpc_task(self) -> None:
        app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            card_response = await client.get("/.well-known/agent-card.json")
            self.assertEqual(card_response.status_code, 200)
            card = card_response.json()
            self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
            self.assertEqual(card["supportedInterfaces"][0]["protocolBinding"], "JSONRPC")
            self.assertFalse(card["capabilities"]["streaming"])

            response = await client.post(
                "/a2a",
                headers={"Content-Type": "application/json", "A2A-Version": "1.0"},
                json={
                    "jsonrpc": "2.0",
                    "id": "test-1",
                    "method": "SendMessage",
                    "params": {
                        "message": {
                            "messageId": "message-1",
                            "role": "ROLE_USER",
                            "parts": [{"text": "Who created Python and when was it first released?"}],
                        }
                    },
                },
            )
            self.assertEqual(response.status_code, 200)
            result = response.json()["result"]["task"]
            self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
            self.assertTrue(result["artifacts"])
            self.assertEqual(result["status"]["message"]["metadata"]["outcome"], "completed")
            self.assertEqual(
                result["status"]["message"]["metadata"]["closure_score_is_probability"],
                False,
            )

    def test_task_mapping_is_safe_and_deterministic(self) -> None:
        mapped = run_id_for_task("task-1")
        self.assertNotEqual(mapped, "task-1")
        self.assertTrue(mapped.startswith("a2a-"))
        self.assertLessEqual(len(mapped), 64)
        self.assertEqual(
            run_id_for_task("task with spaces"),
            run_id_for_task("task with spaces"),
        )
        self.assertNotEqual(
            run_id_for_task("task-1", "owner-a"),
            run_id_for_task("task-1", "owner-b"),
        )

    async def test_app_boundary_rejects_host_origin_and_fetch_metadata(self) -> None:
        app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            wrong_host = await client.get(
                "/.well-known/agent-card.json",
                headers={"Host": "attacker.example"},
            )
            wrong_origin = await client.get(
                "/.well-known/agent-card.json",
                headers={
                    "Origin": "http://attacker.example",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            same_origin = await client.get(
                "/.well-known/agent-card.json",
                headers={
                    "Origin": "http://testserver",
                    "Sec-Fetch-Site": "same-origin",
                },
            )

        self.assertEqual(wrong_host.status_code, 403)
        self.assertEqual(wrong_origin.status_code, 403)
        self.assertEqual(same_origin.status_code, 200)

    async def test_rpc_bearer_boundary_is_explicit(self) -> None:
        app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
            auth_token="test-token",
        )
        transport = httpx.ASGITransport(app=app)
        request = {
            "jsonrpc": "2.0",
            "id": "auth-check",
            "method": "GetTask",
            "params": {"id": "missing-task"},
        }
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            card = await client.get("/.well-known/agent-card.json")
            missing = await client.post("/a2a", json=request)
            wrong = await client.post(
                "/a2a",
                headers={"Authorization": "Bearer wrong-token"},
                json=request,
            )
            authenticated = await client.post(
                "/a2a",
                headers={"Authorization": "Bearer test-token"},
                json=request,
            )

        self.assertEqual(card.status_code, 200)
        self.assertIn("bearer", card.json()["securitySchemes"])
        self.assertEqual(
            card.json()["securityRequirements"][0]["schemes"],
            {"bearer": {}},
        )
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(authenticated.status_code, 200)

    def test_remote_app_requires_https_and_authentication(self) -> None:
        with self.assertRaisesRegex(ValueError, "authentication"):
            create_app(
                base_url="https://agent.example",
                task_store_path=self.task_store_path,
                auth_token="",
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            create_app(
                base_url="http://agent.example",
                task_store_path=self.task_store_path,
                auth_token="token",
            )
        app = create_app(
            base_url="https://agent.example",
            task_store_path=self.task_store_path,
            auth_token="token",
        )
        self.assertIsNotNone(app)

    async def test_official_sdk_client_resolves_card_and_sends_message(self) -> None:
        app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
            client = await create_client(
                "http://testserver",
                ClientConfig(httpx_client=http_client, streaming=False),
            )
            request = SendMessageRequest(
                message=Message(
                    message_id="official-client-message",
                    role=Role.ROLE_USER,
                    parts=[Part(text="Who created Python?")],
                ),
                configuration=SendMessageConfiguration(return_immediately=False),
            )
            responses = [response async for response in client.send_message(request)]
            self.assertEqual(len(responses), 1)
            self.assertEqual(responses[0].task.status.state, TaskState.TASK_STATE_COMPLETED)
            self.assertTrue(responses[0].task.artifacts)
            self.assertEqual(
                responses[0].task.status.message.metadata["outcome"],
                "completed",
            )
            await client.close()

    async def test_evidence_boundary_is_input_required_without_same_task_resume(self) -> None:
        async def incomplete_run(question: str, run_id: str) -> ResearchState:
            return ResearchState(
                run_id=run_id,
                question=question,
                status="evidence_incomplete",
                next_node="done",
                suspension={
                    "reason": "evidence_incomplete",
                    "resume_node": "generate_queries",
                },
            )

        fake_engine = type("FakeEngine", (), {})()
        fake_engine.run = AsyncMock(side_effect=incomplete_run)
        with patch(
            "deep_research.protocols.a2a_gateway.ResearchEngine",
            return_value=fake_engine,
        ) as engine_type:
            app = create_app(
                base_url="http://testserver",
                offline=True,
                task_store_path=self.task_store_path,
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                first = await client.post(
                    "/a2a",
                    headers={"A2A-Version": "1.0"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "input-required-first",
                        "method": "SendMessage",
                        "params": {
                            "message": {
                                "messageId": "input-required-message",
                                "role": "ROLE_USER",
                                "parts": [{"text": "Research an unknowable claim"}],
                            }
                        },
                    },
                )
                task = first.json()["result"]["task"]
                continuation = await client.post(
                    "/a2a",
                    headers={"A2A-Version": "1.0"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "input-required-continuation",
                        "method": "SendMessage",
                        "params": {
                            "message": {
                                "messageId": "input-required-follow-up",
                                "taskId": task["id"],
                                "contextId": task["contextId"],
                                "role": "ROLE_USER",
                                "parts": [{"text": "Continue this same task"}],
                            }
                        },
                    },
                )
                restored = await client.post(
                    "/a2a",
                    headers={"A2A-Version": "1.0"},
                    json={
                        "jsonrpc": "2.0",
                        "id": "input-required-get",
                        "method": "GetTask",
                        "params": {"id": task["id"]},
                    },
                )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            task["status"]["state"],
            "TASK_STATE_INPUT_REQUIRED",
        )
        self.assertNotIn("artifacts", task)
        metadata = task["status"]["message"]["metadata"]
        self.assertEqual(metadata["outcome"], "evidence_incomplete")
        self.assertTrue(metadata["requires_input"])
        self.assertFalse(metadata["same_task_resume_supported"])
        self.assertIn("new A2A Task", metadata["resume_boundary"])
        continuation_task = continuation.json()["result"]["task"]
        self.assertEqual(
            continuation_task["status"]["state"],
            "TASK_STATE_INPUT_REQUIRED",
        )
        continuation_metadata = continuation_task["status"]["message"][
            "metadata"
        ]
        self.assertEqual(
            continuation_metadata["unsupported_operation"],
            "same_task_input_required_resume",
        )
        self.assertFalse(
            continuation_metadata["same_task_resume_supported"]
        )
        self.assertEqual(
            restored.json()["result"]["status"]["state"],
            "TASK_STATE_INPUT_REQUIRED",
        )
        execution_lease = engine_type.call_args.kwargs["execution_lease"]
        self.assertGreater(execution_lease["fence"], 0)
        run_id = metadata["deep_research_run_id"]
        self.assertFalse(
            RunStore(self.runs_dir, run_id).execution_lease_audit()["active"]
        )

    async def test_task_survives_gateway_recreation_and_remains_listable(self) -> None:
        first_app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
        )
        first_transport = httpx.ASGITransport(app=first_app)
        async with httpx.AsyncClient(
            transport=first_transport,
            base_url="http://testserver",
        ) as http_client:
            client = await create_client(
                "http://testserver",
                ClientConfig(httpx_client=http_client, streaming=False),
            )
            request = SendMessageRequest(
                message=Message(
                    message_id="durable-task-message",
                    role=Role.ROLE_USER,
                    parts=[Part(text="Who created Python?")],
                ),
                configuration=SendMessageConfiguration(return_immediately=False),
            )
            responses = [response async for response in client.send_message(request)]
            task_id = responses[0].task.id
            await client.close()

        second_app = create_app(
            base_url="http://testserver",
            offline=True,
            task_store_path=self.task_store_path,
        )
        second_transport = httpx.ASGITransport(app=second_app)
        async with httpx.AsyncClient(
            transport=second_transport,
            base_url="http://testserver",
        ) as http_client:
            client = await create_client(
                "http://testserver",
                ClientConfig(httpx_client=http_client, streaming=False),
            )
            restored = await client.get_task(
                GetTaskRequest(id=task_id, history_length=20)
            )
            listed = await client.list_tasks(
                ListTasksRequest(page_size=20, include_artifacts=True)
            )
            self.assertEqual(restored.id, task_id)
            self.assertEqual(restored.status.state, TaskState.TASK_STATE_COMPLETED)
            self.assertTrue(restored.artifacts)
            self.assertIn(task_id, {task.id for task in listed.tasks})
            await client.close()


class A2ATaskStoreSecurityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "tasks.sqlite3"
        self.context = ServerCallContext()

    async def asyncTearDown(self) -> None:
        self.tempdir.cleanup()

    async def test_first_terminal_task_state_cannot_be_overwritten(self) -> None:
        store = SQLiteTaskStore(self.path)
        await store.save(
            Task(
                id="race-task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_WORKING),
            ),
            self.context,
        )
        await store.save(
            Task(
                id="race-task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            ),
            self.context,
        )
        await store.save(
            Task(
                id="race-task",
                context_id="context",
                status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
            ),
            self.context,
        )

        restored = await store.get("race-task", self.context)
        self.assertEqual(restored.status.state, TaskState.TASK_STATE_CANCELED)

    async def test_task_list_pushes_filters_and_uses_stable_cursor(self) -> None:
        class RecordingTaskStore(SQLiteTaskStore):
            def __init__(self, path):
                self.statements: list[str] = []
                super().__init__(path)

            def _connect(self):
                connection = super()._connect()
                connection.set_trace_callback(self.statements.append)
                return connection

        store = RecordingTaskStore(self.path)
        target_ids = []
        for index in range(1_205):
            context_id = "target" if index % 2 == 0 else "other"
            task_id = f"task-{index:04d}"
            if context_id == "target":
                target_ids.append(task_id)
            await store.save(
                Task(
                    id=task_id,
                    context_id=context_id,
                    status=TaskStatus(
                        state=TaskState.TASK_STATE_WORKING,
                        timestamp=Timestamp(seconds=1_000_000 + index),
                    ),
                ),
                self.context,
            )
        store.statements.clear()
        params = a2a_pb2.ListTasksRequest(
            context_id="target",
            status=TaskState.TASK_STATE_WORKING,
            page_size=37,
        )
        response = await store.list(params, self.context)
        collected = [task.id for task in response.tasks]
        while response.next_page_token:
            params = a2a_pb2.ListTasksRequest(
                context_id="target",
                status=TaskState.TASK_STATE_WORKING,
                page_size=37,
                page_token=response.next_page_token,
            )
            response = await store.list(params, self.context)
            collected.extend(task.id for task in response.tasks)

        self.assertEqual(response.total_size, len(target_ids))
        self.assertEqual(len(collected), len(target_ids))
        self.assertEqual(len(collected), len(set(collected)))
        self.assertEqual(set(collected), set(target_ids))
        list_queries = [
            statement
            for statement in store.statements
            if "FROM a2a_tasks" in statement and "SELECT" in statement
        ]
        self.assertTrue(list_queries)
        self.assertTrue(any("context_id = 'target'" in item for item in list_queries))
        status_clause = f"status_state = {int(TaskState.TASK_STATE_WORKING)}"
        self.assertTrue(any(status_clause in item for item in list_queries))
        self.assertTrue(any("LIMIT 38" in item for item in list_queries))

        first_page = await store.list(
            a2a_pb2.ListTasksRequest(
                context_id="target",
                page_size=1,
            ),
            self.context,
        )
        with self.assertRaises(InvalidParamsError):
            await store.list(
                a2a_pb2.ListTasksRequest(
                    context_id="other",
                    page_size=1,
                    page_token=first_page.next_page_token,
                ),
                self.context,
            )


if __name__ == "__main__":
    unittest.main()
