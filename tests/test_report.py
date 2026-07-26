import json
import tempfile
import unittest
from pathlib import Path

from deep_research.report import generate_html_report


class ReportTest(unittest.TestCase):
    def test_generates_html(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "final.json"
            source.write_text(
                json.dumps(
                    {
                        "run_id": "r1",
                        "status": "completed",
                        "question": "Question?",
                        "plan": {"slots": []},
                        "closure": {"score": 1.0, "closed": True},
                        "verification": {"passed": True, "items": []},
                        "counters": {},
                        "queries": [],
                        "evidence": [],
                        "failures": [],
                        "draft_answer": "Answer.",
                    }
                ),
                encoding="utf-8",
            )
            output = generate_html_report(source, root / "report.html")
            rendered = output.read_text(encoding="utf-8")
            self.assertIn("Question?", rendered)
            self.assertIn('<div class="metric">1.000</div>', rendered)
            self.assertIn('<div class="metric">true</div>', rendered)
            self.assertIn('<div class="metric">not recorded</div>', rendered)

    def test_missing_metrics_are_not_rendered_as_zero_or_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "final.json"
            source.write_text(
                json.dumps(
                    {
                        "run_id": "legacy-r1",
                        "status": "failed",
                        "question": "Legacy question?",
                        "plan": {
                            "slots": [
                                {"description": "Unscored slot", "value": None}
                            ]
                        },
                        "queries": [],
                        "evidence": [
                            {
                                "id": "E1",
                                "slot_id": "s1",
                                "claim": "Claim",
                                "quote": "Quote",
                                "source_url": "https://example.test",
                                "source_title": "Source",
                            }
                        ],
                        "failures": [],
                    }
                ),
                encoding="utf-8",
            )

            rendered = generate_html_report(
                source, root / "report.html"
            ).read_text(encoding="utf-8")

            self.assertGreaterEqual(rendered.count("not recorded"), 7)
            self.assertNotIn('<div class="metric">0.000</div>', rendered)
            self.assertNotIn('<div class="metric">false</div>', rendered)


if __name__ == "__main__":
    unittest.main()
