import io
import unittest

from optimizer_core import (
    CONTESTS,
    optimize_lineup,
    parse_projections,
    parse_roster,
)


class OptimizerCoreTests(unittest.TestCase):
    def setUp(self):
        self.cards = [
            {"player_name": "Quarterback One", "team": "KC", "position": "QB", "multiplier": "1.0", "salary": "7000", "status": "Active"},
            {"player_name": "Runner One", "team": "KC", "position": "RB", "multiplier": "1.0", "salary": "7000", "status": "Active"},
            {"player_name": "Receiver One", "team": "KC", "position": "WR", "multiplier": "1.0", "salary": "7000", "status": "Active"},
            {"player_name": "Tight End One", "team": "KC", "position": "TE", "multiplier": "1.0", "salary": "7000", "status": "Active"},
            {"player_name": "Runner Two", "team": "KC", "position": "RB", "multiplier": "1.0", "salary": "6000", "status": "Active"},
            {"player_name": "Receiver Two", "team": "KC", "position": "WR", "multiplier": "1.0", "salary": "6000", "status": "Active"},
            {"player_name": "Tight End Two", "team": "KC", "position": "TE", "multiplier": "1.0", "salary": "6000", "status": "Active"},
            {"player_name": "Quarterback Two", "team": "KC", "position": "QB", "multiplier": "1.0", "salary": "6000", "status": "Active"},
        ]
        self.projections = [
            {"player_name": row["player_name"], "team": row["team"], "position": row["position"], "raw_projection": "15"}
            for row in self.cards
        ]

    def test_alias_and_historical_projection_suffix_parse_with_team_alias(self):
        roster = parse_roster(io.StringIO("Player,Team,Positions,Multiplier,Salary,Status\nD.K. Metcalf,LV,WR,1.2,7200,Active\n"))
        projections = parse_projections(io.StringIO("Player,3D Proj.\nDK Metcalf WR LVR,15.0\n"))
        self.assertEqual(roster.rows[0].display_name, "D.K. Metcalf")
        self.assertEqual(projections.rows[0].team, "LV")
        result = optimize_lineup(roster, projections, "Spark")
        self.assertIn("matched", result.diagnostics_by_reason)
        self.assertEqual(result.matched_cards, 1)

    def test_strict_team_mismatch_is_not_matched(self):
        cards = [dict(self.cards[0], team="LV")]
        projections = [dict(self.projections[0], team="BUF")]
        result = optimize_lineup(cards, projections, "Spark")
        self.assertFalse(result.feasible)
        self.assertEqual(result.diagnostics_by_reason["team_mismatch"][0].reason, "team_mismatch")

    def test_all_v1_contests_return_complete_lineups(self):
        for contest in CONTESTS:
            with self.subTest(contest=contest):
                result = optimize_lineup(self.cards, self.projections, contest)
                self.assertTrue(result.feasible, result.infeasible_reason)
                self.assertEqual(len(result.lineup), len(CONTESTS[contest].slots))
                self.assertEqual(len({row["athlete_key"] for row in result.lineup}), len(result.lineup))
                self.assertLessEqual(result.total_salary, result.maximum_salary)
                self.assertGreaterEqual(result.total_salary, result.minimum_salary)

    def test_flex_and_superflex_eligibility(self):
        flex = optimize_lineup(self.cards, self.projections, "Flex Appeal")
        self.assertTrue(flex.feasible)
        self.assertEqual(flex.lineup[0]["slot"], "QB")
        self.assertTrue(all(row["position"] in {"RB", "WR", "TE"} for row in flex.lineup[1:]))

        superflex = optimize_lineup(self.cards, self.projections, "Flamethrower")
        self.assertTrue(superflex.feasible)
        superflex_rows = [row for row in superflex.lineup if row["slot"] == "Superflex"]
        self.assertEqual(len(superflex_rows), 1)
        self.assertIn(superflex_rows[0]["position"], {"QB", "RB", "WR", "TE"})

    def test_safe_name_and_team_normalization(self):
        cards = [
            dict(self.cards[2], player_name="Deebo Samuel Sr.", team="JAX"),
            dict(self.cards[3], player_name="John Metchie III", team="LV"),
        ]
        projections = [
            dict(self.projections[2], player_name="Deebo Samuel", team="JAC"),
            dict(self.projections[3], player_name="John Metchie", team="LVR"),
        ]
        parsed_cards = parse_roster(cards)
        parsed_projections = parse_projections(projections)
        result = optimize_lineup(parsed_cards, parsed_projections, "Spark")
        self.assertEqual(result.matched_cards, 2)
        self.assertNotIn("team_mismatch", result.diagnostics_by_reason)

    def test_malformed_schema_is_structured_and_has_no_lineup(self):
        cards = [{"player_name": "No Team", "position": "QB", "multiplier": 1, "salary": 7000, "status": "Active"}]
        projections = [{"player_name": "No Team", "team": "KC", "position": "QB", "raw_projection": 10}]
        result = optimize_lineup(cards, projections, "Spark")
        self.assertFalse(result.feasible)
        self.assertEqual(result.lineup, [])
        self.assertEqual(result.infeasible_reason, "malformed_input_schema")
        self.assertIn("malformed_roster_row", result.diagnostics_by_reason)

    def test_ineligible_rows_are_reported(self):
        cards = [
            dict(self.cards[0], status="Injured Reserve"),
            dict(self.cards[1], player_name="Zero", position="RB"),
            dict(self.cards[2], player_name="Missing", position="WR"),
        ]
        projections = [
            dict(self.projections[0], player_name=self.cards[0]["player_name"]),
            dict(self.projections[1], player_name="Zero", raw_projection="0"),
        ]
        result = optimize_lineup(cards, projections, "Spark")
        self.assertIn("non_active", result.diagnostics_by_reason)
        self.assertIn("zero_projection", result.diagnostics_by_reason)
        self.assertIn("missing_projection", result.diagnostics_by_reason)

    def test_variants_are_choices_but_exact_visible_duplicates_collapse(self):
        cards = self.cards[:4] + [
            dict(self.cards[1], multiplier="1.2", salary="8400"),
            dict(self.cards[3]),
        ]
        result = optimize_lineup(cards, self.projections[:4], "Spark")
        self.assertTrue(result.feasible)
        self.assertEqual(len({row["athlete_key"] for row in result.lineup}), 4)
        self.assertIn("exact_visible_duplicate_collapsed", result.diagnostics_by_reason)

    def test_objective_uses_raw_projection_times_multiplier(self):
        cards = [
            dict(self.cards[0], multiplier="1.5", salary="8000"),
            dict(self.cards[1], multiplier="1.0", salary="7000"),
            dict(self.cards[2], multiplier="1.0", salary="7000"),
            dict(self.cards[3], multiplier="1.0", salary="7000"),
        ]
        projections = [dict(row, raw_projection="10") for row in self.projections[:4]]
        result = optimize_lineup(cards, projections, "Spark")
        self.assertTrue(result.feasible)
        quarterback = next(row for row in result.lineup if row["slot"] == "QB")
        self.assertEqual(quarterback["adjusted_projection"], 15.0)
        self.assertEqual(result.total_adjusted_projection, 45.0)

    def test_minimum_salary_can_require_a_more_expensive_card_variant(self):
        cards = []
        projections = []
        for position, name in (("QB", "Min QB"), ("RB", "Min RB"), ("WR", "Min WR"), ("TE", "Min TE")):
            cards.extend([
                {"player_name": name, "team": "KC", "position": position, "multiplier": 1.0, "salary": 5000, "status": "Active"},
                {"player_name": name, "team": "KC", "position": position, "multiplier": 1.0, "salary": 9000, "status": "Active"},
            ])
            projections.append({"player_name": name, "team": "KC", "position": position, "raw_projection": 10.0})
        result = optimize_lineup(cards, projections, "Spark", minimum_salary=32000)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_salary, 32000)

    def test_multiplier_applied_once_and_minimum_maximum_salary_enforced(self):
        cards = [dict(row, multiplier="1.5", salary="8000") for row in self.cards[:4]]
        projections = [dict(row, raw_projection="10") for row in self.projections[:4]]
        result = optimize_lineup(cards, projections, "Spark", minimum_salary=32000)
        self.assertTrue(result.feasible)
        self.assertEqual(result.total_adjusted_projection, 60.0)
        self.assertEqual(result.total_salary, 32000)

        over_cap = optimize_lineup(cards, projections, "Spark", maximum_salary=30000)
        self.assertFalse(over_cap.feasible)
        self.assertEqual(over_cap.infeasible_reason, "maximum_cap_infeasibility")

    def test_missing_position_and_salary_infeasibility_never_return_partial(self):
        missing_te = optimize_lineup(self.cards[:3], self.projections[:3], "Spark")
        self.assertFalse(missing_te.feasible)
        self.assertEqual(missing_te.lineup, [])
        self.assertEqual(missing_te.infeasible_reason, "no_eligible_card_for_required_slot")

        expensive = [dict(row, salary="10000") for row in self.cards[:4]]
        expensive_result = optimize_lineup(expensive, self.projections[:4], "Spark", maximum_salary=30000)
        self.assertFalse(expensive_result.feasible)
        self.assertEqual(expensive_result.lineup, [])
        self.assertEqual(expensive_result.infeasible_reason, "maximum_cap_infeasibility")

    def test_exact_solver_beats_fixed_order_greedy(self):
        cards = [
            {"player_name": "High QB", "team": "KC", "position": "QB", "multiplier": 1, "salary": 12000, "status": "Active"},
            {"player_name": "Affordable QB", "team": "KC", "position": "QB", "multiplier": 1, "salary": 8000, "status": "Active"},
            {"player_name": "Counter RB", "team": "KC", "position": "RB", "multiplier": 1, "salary": 8000, "status": "Active"},
            {"player_name": "Counter WR", "team": "KC", "position": "WR", "multiplier": 1, "salary": 8000, "status": "Active"},
            {"player_name": "Counter TE", "team": "KC", "position": "TE", "multiplier": 1, "salary": 8000, "status": "Active"},
        ]
        projections = [dict(row, raw_projection=20 if row["player_name"] == "High QB" else 19 if row["player_name"] == "Affordable QB" else 15) for row in cards]
        result = optimize_lineup(cards, projections, "Spark")
        self.assertTrue(result.feasible)
        self.assertNotIn("High QB", [row["player_name"] for row in result.lineup])
        self.assertEqual(result.total_salary, 32000)

    def test_tied_results_are_deterministic(self):
        result_one = optimize_lineup(self.cards, self.projections, "Spark")
        result_two = optimize_lineup(self.cards, self.projections, "Spark")
        self.assertEqual(result_one.lineup, result_two.lineup)

    def test_listed_cards_are_eligible(self):
        cards = [dict(row, status="Listed") for row in self.cards[:4]]
        result = optimize_lineup(cards, self.projections[:4], "Spark")
        self.assertTrue(result.feasible)
        self.assertEqual(len(result.lineup), 4)
        self.assertNotIn("non_active", result.diagnostics_by_reason)


if __name__ == "__main__":
    unittest.main()

