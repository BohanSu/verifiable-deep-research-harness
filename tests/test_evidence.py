import unittest

from deep_research.config import ClosureConfig
from deep_research.evidence import ClosureEngine, EvidenceLedger
from deep_research.schemas import AnswerSlot, Evidence, ResearchPlan, Subgoal


def make_evidence(identifier: str, slot: str, cluster: str) -> Evidence:
    return Evidence(
        id=identifier,
        subgoal_id=f"sg-{slot}",
        slot_id=slot,
        claim="A supported claim.",
        quote="A supported claim.",
        source_url=f"https://example.org/{identifier}",
        source_title="Source",
        stance="supports",
        reliability=0.9,
        extraction_confidence=0.95,
        content_hash=cluster,
        source_cluster_id=cluster,
        source_id=f"S-{identifier}",
        fetch_record_id=f"F-{identifier}",
        snapshot_sha256="a" * 64,
        snapshot_available=True,
        fetch_binding_status="server_bound",
        fetch_binding_valid=True,
        content_hash_scope="page_text",
        claim_quote_consistency=0.95,
        slot_relevance_score=0.95,
    )


class EvidenceTest(unittest.TestCase):
    def test_ledger_deduplicates_same_source_and_quote(self) -> None:
        ledger = EvidenceLedger()
        first = make_evidence("E1", "answer", "same")
        second = make_evidence("E2", "answer", "same")
        self.assertEqual(len(ledger.merge([first], [second])), 1)

    def test_closure_requires_all_slots(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("a", "A"), AnswerSlot("b", "B")],
            subgoals=[Subgoal("sg-a", "A", ["a"], "done")],
        )
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [make_evidence("E1", "a", "cluster-a")]
        )
        self.assertFalse(report.closed)
        self.assertEqual(report.slot_coverage, 0.5)
        self.assertEqual(len(report.slot_audits), 2)
        self.assertTrue(report.slot_audits[1].failure_reasons)

    def test_empty_required_plan_fails_closed(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("optional", "Optional context", required=False)],
            subgoals=[],
        )
        report = ClosureEngine(ClosureConfig()).evaluate(plan, [])
        self.assertFalse(report.closed)
        self.assertFalse(report.hard_gate_passed)
        self.assertIsNone(report.score)
        self.assertEqual(report.score_status, "invalid")
        self.assertIsNone(report.slot_coverage)
        self.assertEqual([gap.type for gap in report.gaps], ["invalid_plan"])

    def test_numeric_conflict_uses_independent_source_consensus(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("year", "Release year")],
            subgoals=[Subgoal("sg-year", "Release year", ["year"], "done")],
        )
        wrong = make_evidence("E1", "year", "source-a")
        wrong.claim = wrong.quote = "The first release was in 1989."
        correct_a = make_evidence("E2", "year", "source-b")
        correct_a.claim = correct_a.quote = "The first release was in 1991."
        correct_b = make_evidence("E3", "year", "source-c")
        correct_b.claim = correct_b.quote = "Python first appeared in 1991."
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [wrong, correct_a, correct_b], {"year"}
        )
        self.assertTrue(report.closed)
        self.assertEqual(plan.slots[0].value, "The first release was in 1991.")

    def test_different_domains_with_same_origin_do_not_form_false_corroboration(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Answer")],
            subgoals=[Subgoal("sg-answer", "Answer", ["answer"], "done")],
        )
        first = make_evidence("E1", "answer", "site-a.example")
        second = make_evidence("E2", "answer", "site-b.example")
        first.origin_cluster_id = second.origin_cluster_id = "origin:wire-story-1"
        first.independence_status = "weak_host_fallback"
        second.independence_status = "dependent"
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [first, second], {"answer"}
        )
        self.assertFalse(report.hard_gate_passed)
        self.assertEqual(report.slot_audits[0].effective_source_count, 1)
        self.assertEqual(report.slot_audits[0].dependent_evidence_ids, ["E2"])

    def test_missing_origin_metadata_does_not_create_unique_source_per_evidence(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Answer")],
            subgoals=[Subgoal("sg-answer", "Answer", ["answer"], "done")],
        )
        first = make_evidence("E1", "answer", "placeholder-a")
        second = make_evidence("E2", "answer", "placeholder-b")
        for item in (first, second):
            item.origin_cluster_id = ""
            item.source_cluster_id = ""
            item.independence_status = "unknown"
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [first, second], {"answer"}
        )
        self.assertFalse(report.hard_gate_passed)
        self.assertEqual(report.slot_audits[0].effective_source_count, 1)
        self.assertEqual(
            report.slot_audits[0].origin_clusters,
            ["unknown:unresolved-origin"],
        )

    def test_dependent_evidence_never_adds_an_independent_source(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Answer")],
            subgoals=[Subgoal("sg-answer", "Answer", ["answer"], "done")],
        )
        primary = make_evidence("E1", "answer", "origin-a")
        dependent = make_evidence("E2", "answer", "origin-b")
        dependent.independence_status = "dependent"
        dependent.independence_basis = "Syndicated copy of E1"
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [primary, dependent], {"answer"}
        )
        self.assertFalse(report.hard_gate_passed)
        self.assertEqual(report.slot_audits[0].effective_source_count, 1)
        self.assertEqual(report.slot_audits[0].dependent_evidence_ids, ["E2"])

    def test_dependent_numeric_copies_cannot_win_consensus_vote(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("year", "Python release year")],
            subgoals=[Subgoal("sg-year", "Python release year", ["year"], "done")],
        )
        correct = []
        for index, origin in enumerate(("python.org", "docs.example"), start=1):
            item = make_evidence(f"E{index}", "year", origin)
            item.claim = item.quote = "Python was first released in 1991."
            correct.append(item)
        copies = []
        for index in range(3):
            item = make_evidence(f"COPY{index}", "year", f"fake-{index}.example")
            item.claim = item.quote = "Python was first released in 1989."
            item.independence_status = "dependent"
            copies.append(item)
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [*correct, *copies], {"year"}
        )
        self.assertTrue(report.hard_gate_passed)
        self.assertEqual(plan.slots[0].value, "Python was first released in 1991.")
        self.assertTrue(
            {item.id for item in copies}.issubset(
                set(report.slot_audits[0].contradicting_evidence_ids)
            )
        )

    def test_nonnumeric_candidate_cannot_replace_numeric_consensus_value(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("year", "When was Python first released?")],
            subgoals=[Subgoal("sg-year", "Python release year", ["year"], "done")],
        )
        first = make_evidence("E1", "year", "python.org")
        first.claim = first.quote = "Python was first released in 1991."
        second = make_evidence("E2", "year", "docs.example")
        second.claim = second.quote = "The first Python release was in 1991."
        vague = make_evidence("E3", "year", "vague.example")
        vague.claim = vague.quote = "The exact Python release date is unknown."
        vague.reliability = 0.99
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [first, second, vague], {"year"}
        )
        self.assertTrue(report.hard_gate_passed)
        self.assertIn("1991", plan.slots[0].value or "")
        self.assertNotIn("unknown", (plan.slots[0].value or "").casefold())
        self.assertEqual(
            report.slot_audits[0].consensus_excluded_evidence_ids, ["E3"]
        )

    def test_high_source_prior_alone_does_not_trigger_authoritative_exception(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Answer")],
            subgoals=[Subgoal("sg-answer", "Answer", ["answer"], "done")],
        )
        evidence = make_evidence("E1", "answer", "university.example")
        evidence.reliability = 0.99
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [evidence], {"answer"}
        )
        self.assertFalse(report.slot_audits[0].authoritative_exception_used)
        self.assertFalse(report.hard_gate_passed)

    def test_self_declared_primary_source_cannot_trigger_authoritative_exception(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Who created Python?")],
            subgoals=[Subgoal("sg-answer", "Who created Python?", ["answer"], "done")],
        )
        evidence = make_evidence("E1", "answer", "python.org")
        evidence.reliability = 0.99
        evidence.source_role = "primary"
        evidence.authority_scope = "Python language creation and history"
        evidence.independence_status = "declared_publisher"
        report = ClosureEngine(ClosureConfig()).evaluate(plan, [evidence], {"answer"})
        self.assertFalse(report.slot_audits[0].authoritative_exception_used)
        self.assertFalse(report.hard_gate_passed)

    def test_verified_primary_source_requires_matching_authority_scope(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Who created Python?")],
            subgoals=[Subgoal("sg-answer", "Who created Python?", ["answer"], "done")],
        )
        evidence = make_evidence("E1", "answer", "python.org")
        evidence.reliability = 0.99
        evidence.source_role = "primary"
        evidence.independence_status = "verified"
        evidence.authority_scope = "JavaScript package download statistics"
        mismatch = ClosureEngine(ClosureConfig()).evaluate(
            plan, [evidence], {"answer"}
        )
        self.assertFalse(mismatch.slot_audits[0].authoritative_exception_used)
        evidence.authority_scope = "Python creation and language history"
        matched = ClosureEngine(ClosureConfig()).evaluate(plan, [evidence], {"answer"})
        self.assertTrue(matched.slot_audits[0].authoritative_exception_used)
        self.assertTrue(matched.hard_gate_passed)

    def test_explicitly_irrelevant_evidence_cannot_create_false_corroboration(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("answer", "Who created Python?")],
            subgoals=[Subgoal("sg-answer", "Who created Python?", ["answer"], "done")],
        )
        relevant = make_evidence("E1", "answer", "python.org")
        irrelevant = make_evidence("E2", "answer", "example.org")
        irrelevant.claim = irrelevant.quote = (
            "This page describes an unrelated language and contains no facts about Python."
        )
        irrelevant.slot_relevance_score = 0.0
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [relevant, irrelevant], {"answer"}
        )
        self.assertFalse(report.hard_gate_passed)
        self.assertEqual(report.slot_audits[0].supporting_evidence_ids, ["E1"])
        self.assertEqual(report.slot_audits[0].effective_source_count, 1)

    def test_low_relevance_majority_cannot_override_relevant_numeric_consensus(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("year", "Python release year")],
            subgoals=[Subgoal("sg-year", "Python release year", ["year"], "done")],
        )
        correct_a = make_evidence("E1", "year", "python.org")
        correct_a.claim = correct_a.quote = "Python was first released in 1991."
        correct_b = make_evidence("E2", "year", "docs.example")
        correct_b.claim = correct_b.quote = "The first Python release was in 1991."
        irrelevant = []
        for index in range(3):
            item = make_evidence(f"EX{index}", "year", f"noise-{index}.example")
            item.claim = item.quote = "An unrelated product was released in 1989."
            item.slot_relevance_score = 0.2
            irrelevant.append(item)
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [correct_a, correct_b, *irrelevant], {"year"}
        )
        self.assertTrue(report.hard_gate_passed)
        self.assertEqual(plan.slots[0].value, "Python was first released in 1991.")
        self.assertEqual(
            set(report.slot_audits[0].supporting_evidence_ids), {"E1", "E2"}
        )

    def test_numbers_in_synthesis_targets_are_complementary_not_conflicts(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[
                AnswerSlot(
                    "benchmarks",
                    "Summarize benchmark datasets, metrics, and performance trends.",
                )
            ],
            subgoals=[
                Subgoal(
                    "sg-benchmarks",
                    "Summarize benchmark evidence",
                    ["benchmarks"],
                    "done",
                )
            ],
        )
        market = make_evidence("E1", "benchmarks", "paper-market")
        market.claim = market.quote = "Method A reports 95.1 mAP on Market-1501."
        msmt = make_evidence("E2", "benchmarks", "paper-msmt")
        msmt.claim = msmt.quote = "Method B reports 74.2 mAP on MSMT17."

        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [market, msmt], {"benchmarks"}
        )

        self.assertTrue(report.hard_gate_passed)
        audit = report.slot_audits[0]
        self.assertEqual(set(audit.supporting_evidence_ids), {"E1", "E2"})
        self.assertEqual(audit.contradicting_evidence_ids, [])
        self.assertTrue(audit.conflict_gate_passed)

    def test_dates_and_costs_inside_a_synthesis_slot_do_not_force_numeric_consensus(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[
                AnswerSlot(
                    "recent_work",
                    "Provide representative recent papers, surveys, and benchmarks with dates and venues when available; identify deployment challenges including cost and real-time use.",
                )
            ],
            subgoals=[
                Subgoal(
                    "sg-recent-work",
                    "Collect representative papers and deployment evidence",
                    ["recent_work"],
                    "done",
                )
            ],
        )
        paper = make_evidence("E1", "recent_work", "paper-a")
        paper.claim = paper.quote = "Paper A was published in 2025 and studies efficient person re-identification."
        benchmark = make_evidence("E2", "recent_work", "paper-b")
        benchmark.claim = benchmark.quote = "Benchmark B was released in 2026 and evaluates real-time retrieval cost."

        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [paper, benchmark], {"recent_work"}
        )

        self.assertTrue(report.hard_gate_passed)
        audit = report.slot_audits[0]
        self.assertEqual(set(audit.supporting_evidence_ids), {"E1", "E2"})
        self.assertEqual(audit.contradicting_evidence_ids, [])

    def test_conflicting_person_answers_cannot_both_pass_as_support(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("creator", "Who created Python?")],
            subgoals=[Subgoal("sg-creator", "Who created Python?", ["creator"], "done")],
        )
        guido = make_evidence("E1", "creator", "python.org")
        guido.claim = guido.quote = "Python was created by Guido van Rossum."
        alice = make_evidence("E2", "creator", "example.org")
        alice.claim = alice.quote = "Python was created by Alice Example."
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [guido, alice], {"creator"}
        )
        self.assertFalse(report.hard_gate_passed)
        audit = report.slot_audits[0]
        self.assertFalse(audit.conflict_gate_passed)
        self.assertEqual(len(audit.supporting_evidence_ids), 1)
        self.assertEqual(len(audit.contradicting_evidence_ids), 1)

    def test_person_answer_paraphrases_form_one_consensus(self) -> None:
        plan = ResearchPlan(
            answer_type="text",
            slots=[AnswerSlot("creator", "Who was the creator of Python?")],
            subgoals=[Subgoal("sg-creator", "Who created Python?", ["creator"], "done")],
        )
        first = make_evidence("E1", "creator", "python.org")
        first.claim = first.quote = "Python was created by Guido van Rossum."
        second = make_evidence("E2", "creator", "example.org")
        second.claim = second.quote = "The creator of Python was Guido van Rossum."
        report = ClosureEngine(ClosureConfig()).evaluate(
            plan, [first, second], {"creator"}
        )
        self.assertTrue(report.hard_gate_passed)
        self.assertEqual(
            set(report.slot_audits[0].supporting_evidence_ids), {"E1", "E2"}
        )


if __name__ == "__main__":
    unittest.main()
