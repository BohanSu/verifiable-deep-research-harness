import unittest

from deep_research.verification import parse_answer_claims


class VerificationParserTest(unittest.TestCase):
    def test_editorial_answer_scope_note_does_not_require_a_source(self) -> None:
        claims = parse_answer_claims(
            "本回答主要覆盖 2024-2026 年的工作；核心方法使用了可回查证据。 [E1]"
        )

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["claim"], "核心方法使用了可回查证据。")
        self.assertEqual(claims[0]["evidence_ids"], ["E1"])

    def test_uncited_external_claim_still_requires_a_source(self) -> None:
        claims = parse_answer_claims("该方法在 2026 年发布。")

        self.assertEqual(len(claims), 1)
        self.assertEqual(claims[0]["evidence_ids"], [])
