import unittest

from deep_research.agents import enforce_verification_contract
from deep_research.schemas import Evidence, VerificationItem, VerificationReport


def evidence(identifier: str = "E1234abcd") -> Evidence:
    return Evidence(
        id=identifier,
        subgoal_id="sg-answer",
        slot_id="answer",
        claim="Python was created by Guido van Rossum.",
        quote="Python was created by Guido van Rossum.",
        source_url="https://python.org/history",
        source_title="Python history",
        stance="supports",
        reliability=0.95,
        extraction_confidence=0.95,
        content_hash="hash",
        source_cluster_id="python.org",
    )


class VerificationContractTest(unittest.TestCase):
    def test_passed_empty_report_cannot_bypass_sentence_coverage(self) -> None:
        result = enforce_verification_contract(
            "Python was created by Guido van Rossum [E1234abcd].",
            [evidence()],
            VerificationReport(passed=True, items=[]),
        )
        self.assertFalse(result.passed)
        self.assertTrue(result.provider_passed)
        self.assertEqual(result.expected_item_count, 1)
        self.assertEqual(result.provider_item_count, 0)
        self.assertEqual(result.contract_version, "engine-verification-contract-v6")
        self.assertEqual(result.items[0].status, "unsupported")
        self.assertIn("omitted", result.items[0].reason)

    def test_uncited_second_sentence_cannot_pass(self) -> None:
        first = VerificationItem(
            claim="Python was created by Guido van Rossum.",
            evidence_ids=["E1234abcd"],
            status="entailed",
            reason="supported",
            claim_id="C1",
            expected_evidence_ids=["E1234abcd"],
            verifier_evidence_ids=["E1234abcd"],
            citation_set_match=True,
        )
        second = VerificationItem(
            claim="It was invented on the Moon.",
            evidence_ids=[],
            status="entailed",
            reason="model incorrectly accepted it",
            claim_id="C2",
            citation_set_match=True,
        )
        result = enforce_verification_contract(
            "Python was created by Guido van Rossum [E1234abcd]. "
            "It was invented on the Moon.",
            [evidence()],
            VerificationReport(passed=True, items=[first, second]),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.items[1].status, "unsupported")
        self.assertIn("no citation", result.items[1].reason)

    def test_exact_complete_report_passes(self) -> None:
        item = VerificationItem(
            claim="Python was created by Guido van Rossum.",
            evidence_ids=["E1234abcd"],
            status="entailed",
            reason="supported",
            claim_id="C1",
            expected_evidence_ids=["E1234abcd"],
            verifier_evidence_ids=["E1234abcd"],
            citation_set_match=True,
        )
        result = enforce_verification_contract(
            "Python was created by Guido van Rossum [E1234abcd].",
            [evidence()],
            VerificationReport(passed=True, items=[item]),
        )
        self.assertTrue(result.passed)
        self.assertEqual(result.items[0].status, "entailed")

    def test_semicolon_separates_independent_claims(self) -> None:
        first = VerificationItem(
            claim="Python was created by Guido van Rossum;",
            evidence_ids=["E1234abcd"],
            status="entailed",
            reason="supported",
            claim_id="C1",
            verifier_evidence_ids=["E1234abcd"],
            citation_set_match=True,
        )
        result = enforce_verification_contract(
            "Python was created by Guido van Rossum [E1234abcd]; "
            "it was invented on the Moon.",
            [evidence()],
            VerificationReport(passed=True, items=[first]),
        )
        self.assertFalse(result.passed)
        self.assertEqual(result.expected_item_count, 2)
        self.assertIn("no citation", result.items[1].reason)

    def test_citation_outside_closure_whitelist_is_rejected(self) -> None:
        item = VerificationItem(
            claim="Python was created by Guido van Rossum.",
            evidence_ids=["E1234abcd"],
            status="entailed",
            reason="supported",
            claim_id="C1",
            expected_evidence_ids=["E1234abcd"],
            verifier_evidence_ids=["E1234abcd"],
            citation_set_match=True,
        )
        result = enforce_verification_contract(
            "Python was created by Guido van Rossum [E1234abcd].",
            [evidence()],
            VerificationReport(passed=True, items=[item]),
            allowed_evidence_ids=set(),
        )
        self.assertFalse(result.passed)
        self.assertIn("outside the closure supporting set", result.items[0].reason)


if __name__ == "__main__":
    unittest.main()
