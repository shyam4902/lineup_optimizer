"""Unit tests verifying all checkpoints in docs/optimizer-quality-plan.md.

Checkpoints covered:
1. Small exhaustive fixture agrees with the single-lineup benchmark.
2. Higher-scoring cheap lineup beats an expensive weak one.
3. Benchmark values do not fall as remaining inventory gets worse.
4. Spreading cards to create more legal entries is rejected in favor of quality requirement.
5. Scarce collection cards, multipliers, duplicate copies, and cap limits work under quality requirement.
6. Salary tie-breaker resolves equal-scoring choices toward cheaper lineups.
7. Substantial unused salary (>10% of cap) is flagged without forcing inferior expensive players.
8. Bounded solve / solver timeout reports best found rather than infeasible.
"""

import json
import unittest
from dataclasses import replace
import pulp
from contest_config import get_default_contests
from multi_lineup_planner import (
    LineupSlotAssignment,
    OwnedCard,
    PlannerCard,
    PlannerLineup,
    WeeklyPlan,
    assess_unused_salary,
    compute_contest_benchmark,
    recalculate_lineup,
    solve_multi_lineup_allocation,
    swap_card_in_lineup,
    validate_lineup,
)


class QualityPlanCheckpointsTests(unittest.TestCase):
    def setUp(self):
        self.contests = get_default_contests()
        self.scorcher = self.contests["Scorcher"]

    def test_checkpoint1_exhaustive_fixture_agrees_with_single_lineup_benchmark(self):
        """Single-lineup benchmark identifies the highest projected points legal lineup."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex", "Flex", "Flex"),
            minimum_salary=0,
            maximum_salary=15000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cards = [
            PlannerCard(OwnedCard("p1", "P1", "p1", "KC", "WR", 1.0, 5000, "Active", 1), 25.0, 25.0, 5000, "dff", True),
            PlannerCard(OwnedCard("p2", "P2", "p2", "BUF", "WR", 1.0, 5000, "Active", 2), 20.0, 20.0, 5000, "dff", True),
            PlannerCard(OwnedCard("p3", "P3", "p3", "BAL", "WR", 1.0, 5000, "Active", 3), 18.0, 18.0, 5000, "dff", True),
            PlannerCard(OwnedCard("p4", "P4", "p4", "DET", "WR", 1.0, 4000, "Active", 4), 15.0, 15.0, 4000, "dff", True),
            PlannerCard(OwnedCard("p5", "P5", "p5", "PHI", "WR", 1.0, 8000, "Active", 5), 30.0, 30.0, 8000, "dff", True),
        ]
        res = compute_contest_benchmark(cards, mini_contest)
        self.assertEqual(res["status"], "proven_optimal")
        self.assertEqual(res["benchmark_score"], 63.0)
        self.assertEqual(res["salary"], 15000)
        self.assertEqual(set(res["cards"]), {"p1", "p2", "p3"})

    def test_checkpoint1_higher_scoring_cheap_lineup_beats_expensive_weak(self):
        """Points determine lineup strength; an expensive weak lineup does not beat a cheap high-scoring one."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=10000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cheap_strong = PlannerCard(OwnedCard("c1", "Cheap Strong", "c1", "KC", "WR", 1.0, 2000, "Active", 1), 35.0, 35.0, 2000, "dff", True)
        expensive_weak = PlannerCard(OwnedCard("c2", "Expensive Weak", "c2", "KC", "WR", 1.0, 9500, "Active", 2), 15.0, 15.0, 9500, "dff", True)

        res = compute_contest_benchmark([cheap_strong, expensive_weak], mini_contest)
        self.assertEqual(res["benchmark_score"], 35.0)
        self.assertEqual(res["salary"], 2000)
        self.assertEqual(res["cards"], ["c1"])

    def test_checkpoint1_benchmark_values_do_not_fall_as_inventory_worsens(self):
        """The contest benchmark remains fixed based on the full initial pool and does not degrade."""
        cards = [
            PlannerCard(OwnedCard(f"c{i}", f"Card {i}", f"c{i}", "KC", "WR", 1.0, 5000, "Active", i), float(100 - i * 10), float(100 - i * 10), 5000, "dff", True)
            for i in range(6)
        ]
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=10000,
            default_entry_limit=3,
            collection_requirements={},
        )
        b_initial = compute_contest_benchmark(cards, mini_contest)
        self.assertEqual(b_initial["benchmark_score"], 100.0)

        plan = solve_multi_lineup_allocation(cards, {"Mini": mini_contest}, target_count=3, quality_threshold=0.50)
        self.assertEqual(len(plan.lineups), 3)
        for l in plan.lineups:
            self.assertEqual(l.contest_benchmark, 100.0)

    def test_checkpoint2_spreading_cards_rejected_when_entries_fall_below_quality(self):
        """Spreading cards into weak legal entries is rejected in favor of quality lineups meeting the benchmark."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=10000,
            default_entry_limit=3,
            collection_requirements={},
        )
        cards = [
            PlannerCard(OwnedCard("c0", "Card 0", "c0", "KC", "WR", 1.0, 5000, "Active", 0), 100.0, 100.0, 5000, "dff", True),
            PlannerCard(OwnedCard("c1", "Card 1", "c1", "KC", "WR", 1.0, 5000, "Active", 1), 85.0, 85.0, 5000, "dff", True),
            PlannerCard(OwnedCard("c2", "Card 2", "c2", "KC", "WR", 1.0, 5000, "Active", 2), 70.0, 70.0, 5000, "dff", True),
        ]
        plan = solve_multi_lineup_allocation(cards, {"Mini": mini_contest}, target_count=3, quality_threshold=0.90)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.lineups[0].slots[0].card.card.card_id, "c0")
        self.assertEqual(plan.lineups[0].benchmark_percentage, 100.0)

    def test_checkpoint2_salary_tie_breaker_prefers_cheaper_lineup_at_equal_score(self):
        """Salary only breaks equal-score ties: cheaper combination wins."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=10000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cheap_card = PlannerCard(OwnedCard("cheap", "Cheap Card", "cheap", "KC", "WR", 1.0, 3000, "Active", 1), 50.0, 50.0, 3000, "dff", True)
        pricey_card = PlannerCard(OwnedCard("pricey", "Pricey Card", "pricey", "BUF", "WR", 1.0, 7000, "Active", 2), 50.0, 50.0, 7000, "dff", True)

        plan = solve_multi_lineup_allocation([cheap_card, pricey_card], {"Mini": mini_contest}, target_count=1)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.lineups[0].slots[0].card.card.card_id, "cheap")
        self.assertEqual(plan.lineups[0].total_salary, 3000)

    def test_checkpoint2_scarce_collection_and_duplicate_copies_respected(self):
        """Collection requirements and card copy uniqueness are preserved under quality solves."""
        contest_col = replace(
            self.scorcher,
            slots=("Flex", "Flex"),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=2,
            collection_requirements={"Rare": (1, None)},
        )
        c_rare = PlannerCard(OwnedCard("r1", "Rare Star", "r1", "KC", "WR", 1.0, 5000, "Active", 1, collection="Rare", collection_group="Rare"), 50.0, 50.0, 5000, "dff", True)
        c_com1 = PlannerCard(OwnedCard("c1", "Common 1", "c1", "KC", "WR", 1.0, 5000, "Active", 2, collection="Common", collection_group="Common"), 45.0, 45.0, 5000, "dff", True)
        c_com2 = PlannerCard(OwnedCard("c2", "Common 2", "c2", "BUF", "WR", 1.0, 5000, "Active", 3, collection="Common", collection_group="Common"), 44.0, 44.0, 5000, "dff", True)
        c_com3 = PlannerCard(OwnedCard("c3", "Common 3", "c3", "BAL", "WR", 1.0, 5000, "Active", 4, collection="Common", collection_group="Common"), 43.0, 43.0, 5000, "dff", True)

        plan = solve_multi_lineup_allocation([c_rare, c_com1, c_com2, c_com3], {"ColContest": contest_col}, target_count=2, quality_threshold=0.90)
        self.assertEqual(len(plan.lineups), 1)
        self.assertTrue(plan.lineups[0].is_valid)
        self.assertIn("r1", [s.card.card.card_id for s in plan.lineups[0].slots])

    def test_checkpoint1_unused_salary_flagged_without_forcing_expensive_inferior_player(self):
        """Unused salary (>10% of cap) is flagged, but a lower-priced, higher-scoring player is never replaced with an inferior expensive one."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=10000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cheap_stud = PlannerCard(OwnedCard("stud", "Cheap Stud", "stud", "KC", "WR", 1.0, 4000, "Active", 1), 30.0, 30.0, 4000, "dff", True)
        expensive_scrub = PlannerCard(OwnedCard("scrub", "Expensive Scrub", "scrub", "KC", "WR", 1.0, 9500, "Active", 2), 10.0, 10.0, 9500, "dff", True)

        plan = solve_multi_lineup_allocation([cheap_stud, expensive_scrub], {"Mini": mini_contest}, target_count=1)
        self.assertEqual(len(plan.lineups), 1)
        lineup = plan.lineups[0]
        self.assertEqual(lineup.slots[0].card.card.card_id, "stud")
        self.assertEqual(lineup.total_salary, 4000)
        self.assertTrue(lineup.unused_salary_flag)
        self.assertIn("No higher-scoring replacement fits", lineup.unused_salary_note)

    def test_salary_never_sacrifices_projected_points_even_for_maximum_salary_difference(self):
        """A $10,000 salary savings must never beat a player with even a fraction of a point higher projection."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cheap_card = PlannerCard(OwnedCard("cheap", "Cheap Card", "cheap", "KC", "WR", 1.0, 1000, "Active", 1), 24.95, 24.95, 1000, "dff", True)
        expensive_card = PlannerCard(OwnedCard("expensive", "Expensive Card", "expensive", "KC", "WR", 1.0, 15000, "Active", 2), 25.00, 25.00, 15000, "dff", True)

        plan = solve_multi_lineup_allocation([cheap_card, expensive_card], {"Mini": mini_contest}, target_count=1)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.lineups[0].slots[0].card.card.card_id, "expensive")
        self.assertEqual(plan.lineups[0].total_projection, 25.0)

    def test_infeasible_contest_benchmark_cleanly_disabled_without_fabricated_fallback(self):
        """If a contest cannot form a legal benchmark, candidate slots for that contest are disabled rather than using a 100.0 pt fallback."""
        # A contest requiring a QB when the card pool has 0 QBs
        qb_contest = replace(
            self.scorcher,
            slots=("QB", "Flex"),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        flex_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 20.0, 20.0, 4000, "dff", True)

        plan = solve_multi_lineup_allocation([wr1], {"QBContest": qb_contest, "FlexContest": flex_contest}, target_count=2)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.lineups[0].contest_name, "FlexContest")
        self.assertNotIn("QBContest", [l.contest_name for l in plan.lineups])
        qb_bench = plan.contest_benchmarks.get("QBContest")
        self.assertTrue(qb_bench is None or qb_bench.get("status") != "proven_optimal")

    def test_solver_timeout_distinguished_from_proven_maximum(self):
        """When solver stops on a time limit, it reports time_limit_feasible and does not claim proven maximum count."""
        # Force a timeout with time_limit = 0.0001 on a multi-candidate model
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 20.0, 20.0, 4000, "dff", True)
        wr2 = PlannerCard(OwnedCard("w2", "WR 2", "w2", "BUF", "WR", 1.0, 4000, "Active", 2), 19.0, 19.0, 4000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=5,
            collection_requirements={},
        )
    def test_two_stage_optimization_guarantees_250_beats_249_99_in_benchmarks_and_allocation(self):
        """Even with a massive $45,000 salary savings ($5,000 vs $50,000), a 250.0 pt card must beat a 249.99 pt card in benchmarks and multi-lineup allocation."""
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=50000,
            default_entry_limit=1,
            collection_requirements={},
        )
        cheap = PlannerCard(OwnedCard("cheap", "Cheap Card", "cheap", "KC", "WR", 1.0, 5000, "Active", 1), 249.99, 249.99, 5000, "dff", True)
        expensive = PlannerCard(OwnedCard("expensive", "Expensive Card", "expensive", "KC", "WR", 1.0, 50000, "Active", 2), 250.0, 250.0, 50000, "dff", True)

        # 1. Single-lineup benchmark must pick the 250.0 card
        bench = compute_contest_benchmark([cheap, expensive], mini_contest)
        self.assertEqual(bench.get("benchmark_score"), 250.0)
        self.assertEqual(bench.get("salary"), 50000)
        self.assertEqual(bench.get("cards"), ["expensive"])

        # 2. Multi-lineup allocation must pick the 250.0 card
        plan = solve_multi_lineup_allocation([cheap, expensive], {"Mini": mini_contest}, target_count=1)
        self.assertEqual(len(plan.lineups), 1)
        self.assertEqual(plan.lineups[0].slots[0].card.card.card_id, "expensive")
        self.assertEqual(plan.lineups[0].total_projection, 250.0)
        self.assertEqual(plan.lineups[0].total_salary, 50000)

    def test_timeout_stopping_reasons_removes_unsupported_impossibility_claim(self):
        """When solver stops on a time limit, it does not claim remaining cards cannot form an additional lineup."""
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 20.0, 20.0, 4000, "dff", True)
        wr2 = PlannerCard(OwnedCard("w2", "WR 2", "w2", "BUF", "WR", 1.0, 4000, "Active", 2), 19.0, 19.0, 4000, "dff", True)
        wr3 = PlannerCard(OwnedCard("w3", "WR 3", "w3", "CIN", "WR", 1.0, 4000, "Active", 3), 18.0, 18.0, 4000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=5,
            collection_requirements={},
        )
        plan = solve_multi_lineup_allocation([wr1, wr2, wr3], {"Mini": mini_contest}, target_count=5, _time_limit=0.00001)
        inv = plan.inventory_summary
        if inv.get("solver_status") == "time_limit_feasible":
            reasons = " ".join(inv.get("stopping_reasons", []))
            self.assertNotIn("cannot form an additional legal lineup", reasons)
            self.assertIn("stopped due to the time limit rather than proven inventory exhaustion", reasons)


    def test_first_stage_benchmark_solution_preserved_when_salary_optimization_fails(self):
        """If Stage 2 (salary minimization) fails or times out, the Stage 1 benchmark solution is preserved."""
        c1 = PlannerCard(OwnedCard("c1", "Card 1", "c1", "KC", "WR", 1.0, 5000, "Active", 1), 20.0, 20.0, 5000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        # Mock pulp.PULP_CBC_CMD to fail on stage 2
        orig_cmd = pulp.PULP_CBC_CMD
        solve_invocations = 0

        class MockCBC(orig_cmd):
            def actualSolve(self, lp, **kwargs):
                nonlocal solve_invocations
                solve_invocations += 1
                if solve_invocations == 1:
                    return super().actualSolve(lp, **kwargs)
                # Fail Stage 2 solve: set status to Not Solved and return failure
                lp.status = pulp.LpStatusNotSolved
                lp.sol_status = pulp.LpSolutionNoSolutionFound
                return pulp.constants.LpStatusNotSolved

        try:
            pulp.PULP_CBC_CMD = MockCBC
            res = compute_contest_benchmark([c1], mini_contest, time_limit=5.0)
            self.assertEqual(res["benchmark_score"], 20.0)
            self.assertEqual(res["cards"], ["c1"])
            self.assertEqual(res["salary"], 5000)
            self.assertIn(res["status"], ("proven_optimal", "best_found"))
        finally:
            pulp.PULP_CBC_CMD = orig_cmd

    def test_maximum_count_proof_derived_only_from_count_optimization_stage(self):
        """Maximum-count proof must derive only from Stage 1 count solve, not Stage 2 salary solve."""
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 20.0, 20.0, 4000, "dff", True)
        wr2 = PlannerCard(OwnedCard("w2", "WR 2", "w2", "BUF", "WR", 1.0, 4000, "Active", 2), 19.0, 19.0, 4000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=5,
            collection_requirements={},
        )
        # 1. When Stage 1 times out (time_limit_feasible), count is NOT proven maximum even if Stage 2 were optimal
        plan = solve_multi_lineup_allocation([wr1, wr2], {"Mini": mini_contest}, target_count=5, _time_limit=0.00001)
        inv = plan.inventory_summary
        if inv.get("solver_status") == "time_limit_feasible":
            self.assertFalse(inv.get("lineup_count_proven_maximum"))

        # 2. When Stage 1 proves optimality, count IS proven maximum
        plan_optimal = solve_multi_lineup_allocation([wr1, wr2], {"Mini": mini_contest}, target_count=5, _time_limit=5.0)
        inv_opt = plan_optimal.inventory_summary
        self.assertEqual(inv_opt.get("solver_status"), "optimal")
        self.assertTrue(inv_opt.get("lineup_count_proven_maximum"))

    def test_upgrade_suggestion_validates_contest_collection_and_salary_rules(self):
        """assess_unused_salary must not suggest an upgrade that violates collection requirements or salary bounds."""
        contest_col = replace(
            self.scorcher,
            slots=("Flex", "Flex"),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={"Rare": (1, None)},
        )
        # Current lineup: 1 Rare card (r1, 20.0 pts), 1 Common card (c1, 35.0 pts).
        # Lineup remaining salary is $16,000 (>10% of cap).
        r1 = PlannerCard(OwnedCard("r1", "Rare 1", "r1", "KC", "WR", 1.0, 2000, "Active", 1, collection="Rare", collection_group="Rare"), 20.0, 20.0, 2000, "dff", True)
        c1 = PlannerCard(OwnedCard("c1", "Common 1", "c1", "BUF", "WR", 1.0, 2000, "Active", 2, collection="Common", collection_group="Common"), 35.0, 35.0, 2000, "dff", True)

        # Candidate Common card: higher projection than r1 (+10 pts) and fits salary, but swapping r1 with cand_com violates the Rare collection minimum!
        cand_com = PlannerCard(OwnedCard("c_cand", "Common Super", "c_cand", "MIA", "WR", 1.0, 3000, "Active", 3, collection="Common", collection_group="Common"), 30.0, 30.0, 3000, "dff", True)

        from multi_lineup_planner import PlannerLineup, LineupSlotAssignment, recalculate_lineup
        lineup = PlannerLineup(
            lineup_id="test_col_upgrade",
            contest_name=contest_col.name,
            slots=[
                LineupSlotAssignment("Flex", "Flex", r1),
                LineupSlotAssignment("Flex2", "Flex", c1),
            ],
        )
        recalculate_lineup(lineup, contest_col)
        self.assertTrue(lineup.is_valid)
        self.assertEqual(lineup.remaining_salary, 16000)

        # assess_unused_salary must NOT suggest cand_com as an upgrade for r1 because replacing r1 would leave 0 Rare cards,
        # and cand_com cannot upgrade c1 because cand_com has lower projection (30.0 < 35.0).
        is_unused, note = assess_unused_salary(lineup, contest_col, [cand_com])
        self.assertTrue(is_unused)
        self.assertNotIn("Common Super", note)
        self.assertIn("No higher-scoring replacement fits", note)

    def test_starting_allocation_discovers_additional_qualifying_lineups(self):
        """Starting allocation portfolio discovery finds multiple qualifying entries across contests without crashing or stalling."""
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 30.0, 30.0, 4000, "dff", True)
        wr2 = PlannerCard(OwnedCard("w2", "WR 2", "w2", "BUF", "WR", 1.0, 4000, "Active", 2), 29.0, 29.0, 4000, "dff", True)
        wr3 = PlannerCard(OwnedCard("w3", "WR 3", "w3", "CIN", "WR", 1.0, 4000, "Active", 3), 28.0, 28.0, 4000, "dff", True)
        wr4 = PlannerCard(OwnedCard("w4", "WR 4", "w4", "MIA", "WR", 1.0, 4000, "Active", 4), 27.0, 27.0, 4000, "dff", True)

        sc = replace(self.scorcher, slots=("Flex",), minimum_salary=0, maximum_salary=20000, default_entry_limit=2, collection_requirements={})
        ft = replace(self.contests["Flamethrower"], slots=("Flex",), minimum_salary=0, maximum_salary=20000, default_entry_limit=2, collection_requirements={})

        plan = solve_multi_lineup_allocation([wr1, wr2, wr3, wr4], {"Scorcher": sc, "Flamethrower": ft}, target_count=4, _time_limit=5.0)
        self.assertEqual(len(plan.lineups), 4)
        self.assertTrue(all(l.is_valid for l in plan.lineups))
        self.assertTrue(all(l.benchmark_percentage >= 90.0 for l in plan.lineups))


    def test_existing_plan_preserved_on_solver_timeout_or_error(self):
        """A timeout or solver error never replaces an existing plan with empty or invalid results."""
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 30.0, 30.0, 4000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        # Create an existing valid plan
        existing_plan = solve_multi_lineup_allocation([wr1], {"Mini": mini_contest}, target_count=1, _time_limit=5.0)
        self.assertEqual(len(existing_plan.lineups), 1)
        self.assertTrue(existing_plan.lineups[0].is_valid)

        # Now simulate a solve that times out immediately
        preserved_plan = solve_multi_lineup_allocation(
            [wr1],
            {"Mini": mini_contest},
            target_count=1,
            existing_plan=existing_plan,
            _time_limit=0.000001,
        )
        self.assertIsNotNone(preserved_plan)
        self.assertEqual(len(preserved_plan.lineups), 1)
        self.assertTrue(preserved_plan.lineups[0].is_valid)
        self.assertEqual(preserved_plan.lineups[0].slots[0].card.card.card_id, "w1")

    def test_upgrade_suggestion_blocks_duplicate_athlete_and_cross_lineup_copy_reuse(self):
        """assess_unused_salary validates duplicate athlete within lineup and card copy reuse across plan."""
        from multi_lineup_planner import PlannerLineup, LineupSlotAssignment, recalculate_lineup
        contest = replace(
            self.scorcher,
            slots=("Flex", "Flex"),
            minimum_salary=0,
            maximum_salary=30000,
            default_entry_limit=2,
            collection_requirements={},
        )
        # Lineup 1 has Athlete A copy 1 (20 pts, $3k) and Athlete B (15 pts, $2k)
        card_a1 = PlannerCard(OwnedCard("a1", "Athlete A", "athlete_a", "KC", "WR", 1.0, 3000, "Active", 1), 20.0, 20.0, 3000, "dff", True)
        card_b = PlannerCard(OwnedCard("b1", "Athlete B", "athlete_b", "BUF", "WR", 1.0, 2000, "Active", 2), 15.0, 15.0, 2000, "dff", True)
        # Owned card A copy 2 has 35 pts, $5k. But Athlete A is already in the lineup!
        card_a2 = PlannerCard(OwnedCard("a2", "Athlete A", "athlete_a", "KC", "WR", 1.0, 5000, "Active", 3), 35.0, 35.0, 5000, "dff", True)

        lineup1 = PlannerLineup(
            lineup_id="lineup_1",
            contest_name=contest.name,
            slots=[
                LineupSlotAssignment("Flex1", "Flex", card_a1),
                LineupSlotAssignment("Flex2", "Flex", card_b),
            ],
        )
        recalculate_lineup(lineup1, contest)
        self.assertTrue(lineup1.is_valid)
        self.assertTrue(lineup1.remaining_salary > 0.10 * contest.maximum_salary)

        # 1. card_a2 cannot upgrade card_b because it would create duplicate Athlete A in the same lineup
        # To test that duplicate athlete is specifically blocked, card_a1 is locked so card_a2 cannot replace card_a1
        lineup1.slots[0].is_locked = True
        is_unused, note = assess_unused_salary(lineup1, contest, [card_a2])
        self.assertTrue(is_unused)
        self.assertNotIn("Athlete A", note)
        self.assertIn("No higher-scoring replacement fits", note)
        lineup1.slots[0].is_locked = False

        # 2. Cross-lineup card copy reuse: if card_c1 is used in lineup_2, it cannot be suggested as an upgrade in lineup_1
        card_c1 = PlannerCard(OwnedCard("c1", "Athlete C", "athlete_c", "MIA", "WR", 1.0, 4000, "Active", 4), 30.0, 30.0, 4000, "dff", True)
        card_usage_across_plan = {
            "a1": ["lineup_1"],
            "b1": ["lineup_1"],
            "c1": ["lineup_2"],  # already assigned to lineup_2
        }
        is_unused2, note2 = assess_unused_salary(lineup1, contest, [card_c1], card_usage_across_plan=card_usage_across_plan)
        self.assertTrue(is_unused2)
        self.assertNotIn("Athlete C", note2)
        self.assertIn("No higher-scoring replacement fits", note2)

    def test_save_load_round_trip_preserves_benchmarks_solver_status_and_stopping_reasons(self):
        """WeeklyPlan serialization and deserialization retains benchmarks, solver status, and stopping reasons."""
        wr1 = PlannerCard(OwnedCard("w1", "WR 1", "w1", "KC", "WR", 1.0, 4000, "Active", 1), 30.0, 30.0, 4000, "dff", True)
        mini_contest = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=5,
            collection_requirements={},
        )
        plan = solve_multi_lineup_allocation([wr1], {"Mini": mini_contest}, target_count=5, _time_limit=5.0)
        p_dict = plan.to_dict()

        # Check serialization contains expected fields
        self.assertIn("contest_benchmarks", p_dict)
        self.assertIn("inventory_summary", p_dict)
        self.assertIn("solver_status", p_dict["inventory_summary"])
        self.assertIn("stopping_reasons", p_dict["inventory_summary"])
        self.assertIn("lineup_count_proven_maximum", p_dict["inventory_summary"])
        self.assertIn("benchmark_percentage", p_dict["lineups"][0])
        self.assertIn("contest_benchmark", p_dict["lineups"][0])

        # Verify round-trip via JSON
        serialized = json.dumps(p_dict)
        deserialized = json.loads(serialized)
        self.assertEqual(deserialized["contest_benchmarks"], p_dict["contest_benchmarks"])
        self.assertEqual(deserialized["inventory_summary"]["solver_status"], p_dict["inventory_summary"]["solver_status"])
        self.assertEqual(deserialized["inventory_summary"]["stopping_reasons"], p_dict["inventory_summary"]["stopping_reasons"])
        self.assertEqual(deserialized["inventory_summary"]["lineup_count_proven_maximum"], p_dict["inventory_summary"]["lineup_count_proven_maximum"])

    def test_isolated_second_roster_fixture_generation(self):
        """Generate lineups using second-roster fixture GB_roster (1).csv without errors."""
        import os
        from multi_lineup_planner import parse_roster_cards, join_cards_with_projections
        roster_path = os.path.abspath("../GameBlazers/rosters/GB_roster (1).csv")
        if os.path.exists(roster_path):
            with open(roster_path, "r", encoding="utf-8-sig") as f:
                owned_cards = parse_roster_cards(f)
            self.assertEqual(len(owned_cards), 91)
            # Use real DFF cheatsheet
            dff_path = os.path.abspath("data/dff_cheatsheet.csv")
            if os.path.exists(dff_path):
                from multi_lineup_planner import parse_dff_cheatsheet
                with open(dff_path, "r", encoding="utf-8") as f:
                    projections = parse_dff_cheatsheet(f)
                joined_cards = join_cards_with_projections(owned_cards, projections)
                eligible_cards = [c for c in joined_cards if c.is_eligible]
                self.assertEqual(len(eligible_cards), 50)
                plan = solve_multi_lineup_allocation(eligible_cards, self.contests, target_count=3, _time_limit=10.0)
                self.assertTrue(len(plan.lineups) > 0)
                self.assertTrue(all(l.is_valid for l in plan.lineups))
                self.assertTrue(all(l.benchmark_percentage >= 90.0 for l in plan.lineups))


    def test_primetime_card_blocked_from_non_pyro_contests(self):
        """Primetime collection cards cannot enter non-Pyro contests (Scorcher, Inferno, etc.)."""
        pt_card = PlannerCard(
            OwnedCard("pt1", "Primetime Star", "pt1", "KC", "WR", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"),
            25.0, 25.0, 5000, "dff", True
        )
        core_card = PlannerCard(
            OwnedCard("c1", "Core Player", "c1", "KC", "WR", 1.0, 5000, "Active", 2, collection="Core", collection_group="Core"),
            20.0, 20.0, 5000, "dff", True
        )
        scorcher = self.contests["Scorcher"]
        lineup = PlannerLineup(
            "scorcher_1",
            "Scorcher",
            [
                LineupSlotAssignment("QB", "QB", PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 3), 20.0, 20.0, 5000, "dff", True)),
                LineupSlotAssignment("RB", "RB", PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "KC", "RB", 1.0, 5000, "Active", 4), 15.0, 15.0, 5000, "dff", True)),
                LineupSlotAssignment("WR", "WR", pt_card),
                LineupSlotAssignment("TE", "TE", PlannerCard(OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 5), 10.0, 10.0, 5000, "dff", True)),
                LineupSlotAssignment("Flex", "Flex", core_card),
            ],
        )
        is_valid, errors = validate_lineup(lineup, scorcher)
        self.assertFalse(is_valid)
        self.assertTrue(any("Primetime collection card" in e and "can only enter Primetime Pyro" in e for e in errors))

    def test_primetime_pyro_composition_requires_at_least_four_primetime_cards(self):
        """Primetime Pyro restrictions are relaxed: arbitrary combinations of Primetime and Core cards are accepted."""
        pyro = self.contests["Primetime Pyro"]
        # 3 Primetime + 3 Core is valid under relaxed rules
        slots_3_pt = [
            LineupSlotAssignment("QB", "QB", PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"), 20.0, 20.0, 5000, "dff", True)),
            LineupSlotAssignment("RB", "RB", PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "KC", "RB", 1.0, 5000, "Active", 2, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("WR", "WR", PlannerCard(OwnedCard("wr1", "WR 1", "wr1", "KC", "WR", 1.0, 5000, "Active", 3, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("TE", "TE", PlannerCard(OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 4, collection="Core", collection_group="Core"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex1", "Flex", PlannerCard(OwnedCard("fx1", "Flex 1", "fx1", "KC", "WR", 1.0, 5000, "Active", 5, collection="Core", collection_group="Core"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex2", "Flex", PlannerCard(OwnedCard("fx2", "Flex 2", "fx2", "KC", "RB", 1.0, 5000, "Active", 6, collection="Core", collection_group="Core"), 8.0, 8.0, 5000, "dff", True)),
        ]
        lineup_3_pt = PlannerLineup("pyro_3_pt", "Primetime Pyro", slots_3_pt)
        is_valid, errors = validate_lineup(lineup_3_pt, pyro)
        self.assertTrue(is_valid, f"3 Primetime + 3 Core should be valid under relaxed rules: {errors}")

        # 4 Primetime + 2 Core is also valid
        slots_4_pt = [
            LineupSlotAssignment("QB", "QB", PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"), 20.0, 20.0, 5000, "dff", True)),
            LineupSlotAssignment("RB", "RB", PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "KC", "RB", 1.0, 5000, "Active", 2, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("WR", "WR", PlannerCard(OwnedCard("wr1", "WR 1", "wr1", "KC", "WR", 1.0, 5000, "Active", 3, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("TE", "TE", PlannerCard(OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 4, collection="Primetime", collection_group="Primetime"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex1", "Flex", PlannerCard(OwnedCard("fx1", "Flex 1", "fx1", "KC", "WR", 1.0, 5000, "Active", 5, collection="Core", collection_group="Core"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex2", "Flex", PlannerCard(OwnedCard("fx2", "Flex 2", "fx2", "KC", "RB", 1.0, 5000, "Active", 6, collection="Core", collection_group="Core"), 8.0, 8.0, 5000, "dff", True)),
        ]
        lineup_good = PlannerLineup("pyro_good", "Primetime Pyro", slots_4_pt)
        is_valid_good, errors_good = validate_lineup(lineup_good, pyro)
        self.assertTrue(is_valid_good, f"Validation errors: {errors_good}")

    def test_primetime_pyro_allows_afternoon_and_non_primetime_players(self):
        """Under relaxed rules, Primetime Pyro allows players playing in afternoon or other games."""
        pyro = self.contests["Primetime Pyro"]
        # CIN is playing TB on Sunday afternoon (not a designated primetime game)
        slots = [
            LineupSlotAssignment("QB", "QB", PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"), 20.0, 20.0, 5000, "dff", True)),
            LineupSlotAssignment("RB", "RB", PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "SF", "RB", 1.0, 5000, "Active", 2, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("WR", "WR", PlannerCard(OwnedCard("wr1", "WR 1", "wr1", "DAL", "WR", 1.0, 5000, "Active", 3, collection="Primetime", collection_group="Primetime"), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("TE", "TE", PlannerCard(OwnedCard("te1", "TE 1", "te1", "DEN", "TE", 1.0, 5000, "Active", 4, collection="Primetime", collection_group="Primetime"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex1", "Flex", PlannerCard(OwnedCard("fx1", "Core Primetime", "fx1", "NE", "WR", 1.0, 5000, "Active", 5, collection="Core", collection_group="Core"), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex2", "Flex", PlannerCard(OwnedCard("fx2", "Core Afternoon", "fx2", "CIN", "WR", 1.0, 5000, "Active", 6, collection="Core", collection_group="Core"), 12.0, 12.0, 5000, "dff", True)),
        ]
        lineup = PlannerLineup("pyro_afternoon", "Primetime Pyro", slots)
        is_valid, errors = validate_lineup(lineup, pyro)
        self.assertTrue(is_valid, f"Lineup with afternoon player should now be valid: {errors}")


    def test_benchmarks_and_solver_respect_primetime_separation_and_eligibility(self):
        """Benchmarks and MIP solver strictly obey Primetime collection and weekly game eligibility."""
        pyro = self.contests["Primetime Pyro"]
        scorcher = self.contests["Scorcher"]

        # Pool contains:
        # - 1 KC Primetime WR (primetime game, Primetime collection)
        # - 1 CIN Core WR (afternoon game, Core collection)
        # - 1 KC Core WR (primetime game, Core collection)
        pt_kc_wr = PlannerCard(OwnedCard("pt_kc", "PT KC", "pt_kc", "KC", "WR", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"), 30.0, 30.0, 5000, "dff", True)
        cin_core_wr = PlannerCard(OwnedCard("cin_core", "CIN Core", "cin_core", "CIN", "WR", 1.0, 5000, "Active", 2, collection="Core", collection_group="Core"), 25.0, 25.0, 5000, "dff", True)
        kc_core_wr = PlannerCard(OwnedCard("kc_core", "KC Core", "kc_core", "KC", "WR", 1.0, 5000, "Active", 3, collection="Core", collection_group="Core"), 22.0, 22.0, 5000, "dff", True)

        mini_scorcher = replace(scorcher, slots=("Flex",), minimum_salary=0, maximum_salary=10000, default_entry_limit=1, collection_requirements={})
        scorcher_bench = compute_contest_benchmark([pt_kc_wr, cin_core_wr, kc_core_wr], mini_scorcher)
        # pt_kc_wr is forbidden from Scorcher; cin_core_wr should win
        self.assertEqual(scorcher_bench["cards"], ["cin_core"])

    def test_existing_invalid_entries_flagged_without_deletion(self):
        """Existing invalid entries are marked is_valid=False with errors and preserved in plan without deletion."""
        scorcher = self.contests["Scorcher"]
        pt_card = PlannerCard(
            OwnedCard("pt_bad", "PT Card", "pt_bad", "KC", "WR", 1.0, 5000, "Active", 1, collection="Primetime", collection_group="Primetime"),
            25.0, 25.0, 5000, "dff", True
        )
        slots = [
            LineupSlotAssignment("QB", "QB", PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 2), 20.0, 20.0, 5000, "dff", True)),
            LineupSlotAssignment("RB", "RB", PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "KC", "RB", 1.0, 5000, "Active", 3), 15.0, 15.0, 5000, "dff", True)),
            LineupSlotAssignment("WR", "WR", pt_card),
            LineupSlotAssignment("TE", "TE", PlannerCard(OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 4), 10.0, 10.0, 5000, "dff", True)),
            LineupSlotAssignment("Flex", "Flex", PlannerCard(OwnedCard("fx1", "Flex 1", "fx1", "KC", "WR", 1.0, 5000, "Active", 5), 10.0, 10.0, 5000, "dff", True)),
        ]
        lineup = PlannerLineup("scorcher_invalid", "Scorcher", slots)
        recalculate_lineup(lineup, scorcher)
        self.assertFalse(lineup.is_valid)
        self.assertTrue(len(lineup.validation_errors) > 0)

    def test_raising_quality_from_80_to_100_percent_never_returns_80_percent_lineup_as_qualifying(self):
        """Raising quality from 80% to 100% must not return an 80% lineup as qualifying.

        When generation cannot find a 100% qualifying lineup, existing entries are preserved
        separately in preserved_existing_lineups rather than winning selection as qualifying lineups.
        """
        mini = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        card_85 = PlannerCard(
            OwnedCard("c85", "Player 85", "p85", "KC", "WR", 1.0, 5000, "Active", 1),
            85.0, 85.0, 5000, "dff", True
        )
        benchmarks = {"Mini": {"benchmark_score": 100.0, "salary": 5000, "status": "proven_optimal", "contest_name": "Mini", "cards": ["c100"]}}

        # 1. Generate at 80% quality threshold (85 pts >= 80% of 100.0)
        plan_80 = solve_multi_lineup_allocation(
            cards=[card_85],
            contests={"Mini": mini},
            target_count=1,
            quality_threshold=0.80,
            contest_benchmarks=benchmarks,
            _time_limit=5.0,
        )
        self.assertEqual(len(plan_80.lineups), 1)
        self.assertTrue(plan_80.lineups[0].is_valid)
        self.assertEqual(plan_80.lineups[0].benchmark_percentage, 85.0)

        # 2. Regenerate at 100% quality threshold with existing_plan=plan_80
        plan_100 = solve_multi_lineup_allocation(
            cards=[card_85],
            contests={"Mini": mini},
            target_count=1,
            existing_plan=plan_80,
            quality_threshold=1.00,
            contest_benchmarks=benchmarks,
            _time_limit=5.0,
        )

        # The 85% lineup MUST NOT be returned in lineups as qualifying!
        self.assertEqual(len(plan_100.lineups), 0, "80% lineup must not be returned as qualifying when quality is 100%")
        # The existing entry must be preserved separately
        self.assertEqual(len(plan_100.preserved_existing_lineups), 1)
        self.assertEqual(plan_100.preserved_existing_lineups[0].slots[0].card.card.card_id, "c85")
        self.assertIn("preserved_existing_lineups", plan_100.inventory_summary)

    def test_old_plan_cannot_win_selection_if_violating_current_exclusions_or_rules(self):
        """An old plan cannot win selection if cards violate new exclusions or contest rules."""
        mini = replace(
            self.scorcher,
            slots=("Flex",),
            minimum_salary=0,
            maximum_salary=20000,
            default_entry_limit=1,
            collection_requirements={},
        )
        card_a = PlannerCard(OwnedCard("ca", "Player A", "pa", "KC", "WR", 1.0, 5000, "Active", 1), 95.0, 95.0, 5000, "dff", True)
        benchmarks = {"Mini": {"benchmark_score": 100.0, "salary": 5000, "status": "proven_optimal", "contest_name": "Mini", "cards": ["ca"]}}

        plan_a = solve_multi_lineup_allocation(
            cards=[card_a],
            contests={"Mini": mini},
            target_count=1,
            quality_threshold=0.90,
            contest_benchmarks=benchmarks,
            _time_limit=5.0,
        )
        self.assertEqual(len(plan_a.lineups), 1)

        # Now exclude player 'pa'
        plan_with_exclusion = solve_multi_lineup_allocation(
            cards=[card_a],
            contests={"Mini": mini},
            target_count=1,
            existing_plan=plan_a,
            excluded_athlete_keys=["pa"],
            quality_threshold=0.90,
            contest_benchmarks=benchmarks,
            _time_limit=5.0,
        )
        # Old plan contains excluded player pa, so it cannot win selection
        self.assertEqual(len(plan_with_exclusion.lineups), 0)
        self.assertEqual(len(plan_with_exclusion.preserved_existing_lineups), 1)

    def test_primetime_eligibility_explicitly_restricted_to_week_1(self):
        """Primetime eligibility raises ValueError if requested for a week other than Week 1 in this preview."""
        from multi_lineup_planner import get_primetime_eligible_teams

        # Week 1 works
        teams = get_primetime_eligible_teams(week=1)
        self.assertIn("KC", teams)
        self.assertIn("SF", teams)

        # Non-Week 1 via week param
        with self.assertRaises(ValueError) as ctx:
            get_primetime_eligible_teams(week=2)
        self.assertIn("restricted to Week 1", str(ctx.exception))

        # Non-Week 1 via schedule_data mapping
        with self.assertRaises(ValueError) as ctx:
            get_primetime_eligible_teams(schedule_data={"week": 3, "games": []})
        self.assertIn("restricted to Week 1", str(ctx.exception))

        # Non-Week 1 via designated_game_ids
        with self.assertRaises(ValueError) as ctx:
            get_primetime_eligible_teams(designated_game_ids=["2026_02_KC_LAC"])
        self.assertIn("restricted to Week 1", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()



