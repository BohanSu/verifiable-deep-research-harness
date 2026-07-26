import unittest

from deep_research.methodology import (
    methodology_contract,
    methodology_snapshot,
    source_prior,
)


class MethodologySnapshotTest(unittest.TestCase):
    def test_run_snapshot_contains_the_complete_current_formula(self) -> None:
        contract = methodology_contract()
        snapshot = methodology_snapshot("Model", "Search")

        self.assertEqual(
            snapshot["metric_definition_hash"],
            contract["metric_definition_hash"],
        )
        self.assertEqual(snapshot["closure_score"], contract["closure_score"])
        self.assertEqual(
            snapshot["metric_contracts"],
            contract["metric_contracts"],
        )
        self.assertTrue(snapshot["limitations"])
        self.assertEqual(snapshot["methodology_version"], "evidence-closure-v4.24")
        self.assertIn(
            "null/invalid",
            snapshot["metric_contracts"][0]["denominator"],
        )
        self.assertEqual(snapshot["model_provider"], "Model")
        self.assertEqual(snapshot["search_provider"], "Search")

    def test_contract_weights_are_normalized(self) -> None:
        contract = methodology_contract()

        self.assertAlmostEqual(sum(contract["closure_score"].values()), 1.0)
        self.assertAlmostEqual(sum(contract["slot_evidence_score"].values()), 1.0)

    def test_source_prior_is_one_shared_uncalibrated_policy(self) -> None:
        contract = methodology_contract()

        self.assertEqual(source_prior("reference"), 0.82)
        self.assertEqual(source_prior("REFERENCE"), 0.82)
        self.assertEqual(source_prior("new-class"), contract["unknown_source_prior"])
        self.assertEqual(
            contract["source_priors"]["reference"],
            source_prior("reference"),
        )


if __name__ == "__main__":
    unittest.main()
