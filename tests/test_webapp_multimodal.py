import io
import json
import tempfile
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from deep_research import webapp
from deep_research.config import AppConfig
from deep_research.storage import RunStore


def multipart_body(
    boundary: str,
    fields: dict[str, str],
    files: list[tuple[str, str, str, bytes]],
) -> bytes:
    chunks: list[bytes] = []
    marker = f"--{boundary}\r\n".encode("ascii")
    for name, value in fields.items():
        chunks.extend(
            [
                marker,
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("ascii"),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )
    for field, filename, media_type, data in files:
        chunks.extend(
            [
                marker,
                (
                    f'Content-Disposition: form-data; name="{field}"; '
                    f'filename="{filename}"\r\n'
                ).encode("ascii"),
                f"Content-Type: {media_type}\r\n\r\n".encode("ascii"),
                data,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


def invoke_handler(
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    handler = object.__new__(webapp.ResearchRequestHandler)
    message = Message()
    message["Host"] = "127.0.0.1:8000"
    for name, value in (headers or {}).items():
        message[name] = value
    handler.command = method
    handler.path = path
    handler.request_version = "HTTP/1.1"
    handler.requestline = f"{method} {path} HTTP/1.1"
    handler.headers = message
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.server = SimpleNamespace(server_port=8000)
    handler.client_address = ("127.0.0.1", 50000)
    handler.close_connection = True
    getattr(handler, f"do_{method}")()
    raw = handler.wfile.getvalue()
    header_bytes, response_body = raw.split(b"\r\n\r\n", 1)
    lines = header_bytes.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    response_headers = {
        name.strip(): value.strip()
        for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
    }
    return status, response_headers, response_body


class MultipartRunApiTest(unittest.TestCase):
    def test_home_csp_allows_local_blob_previews_without_relaxing_scripts(self) -> None:
        status, headers, _ = invoke_handler("GET", "/index.html")

        policy = headers["Content-Security-Policy"]
        self.assertEqual(status, 200)
        self.assertIn("img-src 'self' data: blob:", policy)
        self.assertIn("media-src 'self' blob:", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)

    def test_config_names_declared_and_model_gateway_bound_capability_evidence(self) -> None:
        config = AppConfig()
        with patch.object(AppConfig, "from_env", return_value=config):
            status, _, response_body = invoke_handler("GET", "/api/config")

        payload = json.loads(response_body)
        self.assertEqual(status, 200)
        self.assertEqual(
            payload["multimodal"]["capability_source"],
            "operator-declared-plus-exact-model-and-gateway-bound-probe-receipt",
        )
        self.assertEqual(
            payload["multimodal"]["capability_binding"],
            "model_id + SHA-256(normalized gateway base URL)",
        )
        self.assertTrue(payload["search_configured"])

    def test_multipart_creation_persists_and_serves_auditable_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(
                runs_dir=root / "runs",
                replay_corpus=Path("examples/replay_corpus.json"),
                model_provider="mock",
                search_provider="replay",
                model_profile="team",
            )
            captured: dict[str, object] = {}
            worker_finished = threading.Event()
            created_run_id: str | None = None

            def fake_worker(*args, **kwargs):
                captured["args"] = args
                captured["kwargs"] = kwargs
                lease = args[4]
                store = RunStore(config.runs_dir, args[0])
                store.release_execution_lease(
                    str(lease["owner_token"]), int(lease["fence"])
                )
                webapp._worker_slots.release()
                worker_finished.set()

            boundary = "deep-research-boundary"
            raw_attachment = b"A durable user supplied note."
            body = multipart_body(
                boundary,
                {
                    "question": "What does the supplied note contribute?",
                    "offline": "true",
                    "profile": "team",
                },
                [("attachments", "note.txt", "text/plain", raw_attachment)],
            )

            try:
                with patch.object(AppConfig, "from_env", return_value=config), patch.object(
                    webapp, "_run_in_background", side_effect=fake_worker
                ):
                    status, _, response_body = invoke_handler(
                        "POST",
                        "/api/runs",
                        body=body,
                        headers={
                            "Content-Type": f"multipart/form-data; boundary={boundary}",
                            "Content-Length": str(len(body)),
                        },
                    )
                    payload = json.loads(response_body)
                    self.assertEqual(status, 202)
                    self.assertTrue(worker_finished.wait(5))

                    run_id = payload["run_id"]
                    created_run_id = run_id
                    attachment = payload["attachments"][0]
                    self.assertEqual(payload["profile"], "offline")
                    self.assertEqual(attachment["media_type"], "text/plain")
                    self.assertEqual(
                        captured["kwargs"]["model_profile"], "team"
                    )

                    attachment_status, attachment_headers, attachment_body = invoke_handler(
                        "GET",
                        f"/api/runs/{run_id}/attachments/{attachment['id']}",
                    )
                    self.assertEqual(attachment_status, 200)
                    self.assertEqual(attachment_body, raw_attachment)
                    self.assertEqual(
                        attachment_headers["X-Content-Type-Options"],
                        "nosniff",
                    )

                    audit_status, _, audit_body = invoke_handler(
                        "GET", f"/api/runs/{run_id}?limit=5"
                    )
                    run_projection = json.loads(audit_body)
                    self.assertEqual(audit_status, 200)
                    audit = run_projection["audit"]
                    audit_attachment = audit["input_attachments"][0]
                    self.assertNotIn("content_uri", audit_attachment)
                    self.assertEqual(
                        audit_attachment["content_url"],
                        f"/api/runs/{run_id}/attachments/{attachment['id']}",
                    )
                    public_attachment = run_projection["state"]["input_attachments"][0]
                    self.assertNotIn("content_uri", public_attachment)
                    self.assertEqual(
                        public_attachment["content_url"],
                        audit_attachment["content_url"],
                    )
            finally:
                with webapp._jobs_lock:
                    if created_run_id:
                        webapp._jobs.pop(created_run_id, None)
                        webapp._cancel_events.pop(created_run_id, None)


if __name__ == "__main__":
    unittest.main()
