import tempfile
import unittest
from pathlib import Path

from deep_research.protocol_index import AgUiProtocolIndex, ProtocolIndexConflict


class AgUiProtocolIndexTest(unittest.TestCase):
    def test_external_run_id_is_global_and_identical_request_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = AgUiProtocolIndex(Path(tmp))
            first = index.register_run(
                thread_id="thread-1",
                run_id="external-run-1",
                durable_run_id="durable-1",
                kind="producer",
                parent_run_id=None,
                request_hash="hash-1",
            )
            replay = index.register_run(
                thread_id="thread-1",
                run_id="external-run-1",
                durable_run_id="ignored-new-durable",
                kind="producer",
                parent_run_id=None,
                request_hash="hash-1",
            )

            self.assertEqual(first["status"], "registered")
            self.assertEqual(replay["status"], "replay")
            self.assertEqual(replay["durable_run_id"], "durable-1")
            with self.assertRaisesRegex(ProtocolIndexConflict, "globally registered"):
                index.register_run(
                    thread_id="thread-2",
                    run_id="external-run-1",
                    durable_run_id="durable-2",
                    kind="producer",
                    parent_run_id=None,
                    request_hash="hash-2",
                )

    def test_parent_lineage_must_remain_within_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = AgUiProtocolIndex(Path(tmp))
            index.register_run(
                thread_id="thread-parent",
                run_id="parent-run",
                durable_run_id="durable-parent",
                kind="producer",
                parent_run_id=None,
                request_hash="parent-hash",
            )
            with self.assertRaisesRegex(ProtocolIndexConflict, "different AG-UI thread"):
                index.register_run(
                    thread_id="thread-child",
                    run_id="child-run",
                    durable_run_id="durable-child",
                    kind="resume",
                    parent_run_id="parent-run",
                    request_hash="child-hash",
                )

    def test_status_is_durable_and_queryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = AgUiProtocolIndex(Path(tmp))
            index.register_run(
                thread_id="thread-status",
                run_id="status-run",
                durable_run_id="durable-status",
                kind="producer",
                parent_run_id=None,
                request_hash="status-hash",
            )
            index.mark_status("status-run", "completed")

            self.assertEqual(index.get_run("status-run")["status"], "completed")
            self.assertEqual(
                index.runs_for_durable("durable-status")[0]["run_id"],
                "status-run",
            )
            transitions = index.status_transitions_for_durable("durable-status")
            self.assertEqual(
                [(item["from_status"], item["to_status"]) for item in transitions],
                [(None, "registered"), ("registered", "completed")],
            )

    def test_status_transition_rejects_unknown_and_terminal_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = AgUiProtocolIndex(Path(tmp))
            index.register_run(
                thread_id="thread-status",
                run_id="status-run",
                durable_run_id="durable-status",
                kind="producer",
                parent_run_id=None,
                request_hash="status-hash",
            )
            with self.assertRaisesRegex(ValueError, "invalid AG-UI protocol run status"):
                index.mark_status("status-run", "made-up")
            index.mark_status("status-run", "completed")
            with self.assertRaisesRegex(ProtocolIndexConflict, "status transition"):
                index.mark_status("status-run", "queued")


if __name__ == "__main__":
    unittest.main()
