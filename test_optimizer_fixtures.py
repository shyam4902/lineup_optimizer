import unittest
from pathlib import Path

from optimizer_core import optimize_lineup, prepare_pool


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "optimizer_v1"


class OptimizerFixtureTests(unittest.TestCase):
    def test_valid_fixture_supports_every_v1_contest(self):
        cards = FIXTURES / "valid_cards.csv"
        projections = FIXTURES / "valid_projections.csv"
        for contest in ("Spark", "Scorcher", "Wildfire", "Flex Appeal", "Flamethrower", "Inferno"):
            with self.subTest(contest=contest):
                result = optimize_lineup(cards, projections, contest)
                self.assertTrue(result.feasible, result.infeasible_reason)
                self.assertEqual(len(result.lineup), len(result.slots))

    def test_edge_fixture_reports_each_eligibility_reason(self):
        pool = prepare_pool(FIXTURES / "edge_cards.csv", FIXTURES / "edge_projections.csv")
        reasons = pool.diagnostics_by_reason
        self.assertIn("matched", reasons)
        self.assertIn("non_active", reasons)
        self.assertIn("zero_projection", reasons)
        self.assertIn("missing_projection", reasons)
        self.assertIn("name_unmatched", reasons)
        self.assertIn("exact_visible_duplicate_collapsed", reasons)
        self.assertEqual(pool.eligible_cards, 5)

    def test_missing_position_fixture_has_no_partial_lineup(self):
        result = optimize_lineup(
            FIXTURES / "infeasible_missing_te_cards.csv",
            FIXTURES / "infeasible_missing_te_projections.csv",
            "Spark",
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.lineup, [])
        self.assertEqual(result.infeasible_reason, "no_eligible_card_for_required_slot")

    def test_salary_fixture_has_no_over_cap_lineup(self):
        result = optimize_lineup(
            FIXTURES / "infeasible_salary_cards.csv",
            FIXTURES / "infeasible_salary_projections.csv",
            "Spark",
        )
        self.assertFalse(result.feasible)
        self.assertEqual(result.lineup, [])
        self.assertEqual(result.infeasible_reason, "maximum_cap_infeasibility")

    def test_flex_appeal_fixture_has_six_complete_slots(self):
        result = optimize_lineup(
            FIXTURES / "flex_appeal_cards.csv",
            FIXTURES / "flex_appeal_projections.csv",
            "Flex Appeal",
        )
        self.assertTrue(result.feasible, result.infeasible_reason)
        self.assertEqual([row["slot"] for row in result.lineup], ["QB", "Flex", "Flex2", "Flex3", "Flex4", "Flex5"])
        self.assertEqual(result.total_salary, 31000)

    def test_greedy_counterexample_is_solved_exactly(self):
        result = optimize_lineup(
            FIXTURES / "greedy_counterexample_cards.csv",
            FIXTURES / "greedy_counterexample_projections.csv",
            "Spark",
        )
        self.assertTrue(result.feasible, result.infeasible_reason)
        self.assertEqual(result.total_salary, 32000)
        self.assertEqual(next(row for row in result.lineup if row["slot"] == "QB")["player_name"], "Affordable QB")


if __name__ == "__main__":
    unittest.main()
