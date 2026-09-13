"""Comprehensive test suite for GameBlazers Weekly Lineup Planner.

Checks:
- Multiplier applied exactly once to raw projection, never to salary.
- Weekly salary joins and selection (DFF weekly salary vs roster card salary).
- Card copy counts and strict prevention of cross-lineup reuse of the same card copy.
- Athlete uniqueness within single lineups.
- Manual card replacement and immediate recalculation of validity, totals, and payouts.
- Lock preservation across regeneration (both card-level and lineup-level locks).
- Projection overrides surviving DFF projection refreshes.
- Payout bands evaluate mutually exclusively without double-counting overlapping tiers.
- Synthetic portfolio allocation proving that solving lineups jointly beats greedy single-lineup selection.
- Clear distinction between deterministic payout-at-projected-score estimates and calibrated probabilistic EV.
"""

from __future__ import annotations

import io
import unittest
from pathlib import Path

from contest_config import ContestConfig, PayoutBand, get_default_contests
from dff_client import (
    DFFPlayerProjection,
    ProjectionsSnapshot,
    compare_snapshots,
    parse_dff_players,
    parse_projections_csv_stream,
)
from multi_lineup_planner import (
    LineupSlotAssignment,
    OwnedCard,
    PlannerCard,
    PlannerLineup,
    WeeklyPlan,
    check_inventory_feasibility,
    export_plan_to_csv,
    join_cards_with_projections,
    parse_roster_cards,
    recalculate_lineup,
    solve_multi_lineup_allocation,
    swap_card_in_lineup,
    validate_lineup,
)
from payout_evaluator import (
    evaluate_deterministic_tier,
    evaluate_probabilistic_payout,
    get_probabilistic_ranking_requirements,
)


class WeeklyPlannerTests(unittest.TestCase):
    def setUp(self):
        self.contests = get_default_contests()
        self.scorcher = self.contests["Scorcher"]
        self.wildfire = self.contests["Wildfire"]

    def test_auto_generation_supports_flex_only_contest_without_qbs(self):
        from dataclasses import replace
        contest = replace(self.scorcher, slots=("Flex",) * 5, minimum_salary=0,
                          default_entry_limit=3, collection_requirements={})
        cards = [PlannerCard(OwnedCard(f"wr{i}", f"Player {i}", f"player{i}", "KC", "WR", 1, 5000, "Active", i),
                             10, 10, 5000, "roster", True) for i in range(10)]
        plan = solve_multi_lineup_allocation(cards, {"Scorcher": contest})
        self.assertEqual(len(plan.lineups), 2)
        self.assertTrue(all(lineup.is_valid for lineup in plan.lineups))
        self.assertEqual(len({s.card.card.card_id for l in plan.lineups for s in l.slots}), 10)

    def test_default_generation_uses_lower_projected_cards_without_quality_gate(self):
        """Every positive projection remains usable; choose the highest scores within the cap."""
        from dataclasses import replace
        contest = replace(self.scorcher, slots=("Flex",), minimum_salary=0,
                          maximum_salary=6000, default_entry_limit=2, collection_requirements={})
        # Card 0: 200 pts (benchmark = 200 pts)
        # Card 1: 2 pts (1% of benchmark)
        # Card 2: 1 pt (0.5% of benchmark)
        cards = [
            PlannerCard(OwnedCard("c0", "Star WR", "c0", "KC", "WR", 1.0, 3000, "Active", 0), 200.0, 200.0, 3000, "roster", True),
            PlannerCard(OwnedCard("c1", "Weak WR 1", "c1", "KC", "WR", 1.0, 5900, "Active", 1), 2.0, 2.0, 5900, "roster", True),
            PlannerCard(OwnedCard("c2", "Weak WR 2", "c2", "KC", "WR", 1.0, 6000, "Active", 2), 1.0, 1.0, 6000, "roster", True),
        ]
        # No implicit quality gate may discard the second valid entry.
        for distribution in (None, {"Scorcher": 2}):
            plan = solve_multi_lineup_allocation(cards, {"Scorcher": contest},
                                                 contest_distribution=distribution)
            self.assertEqual(len(plan.lineups), 2)
            self.assertEqual({l.slots[0].card.card.card_id for l in plan.lineups}, {"c0", "c1"})
            self.assertTrue(all(l.is_valid for l in plan.lineups))

    def test_timeout_is_not_reported_as_infeasible(self):
        from unittest.mock import patch
        with patch("multi_lineup_planner.pulp.LpProblem.solve", return_value=0):
            with self.assertRaisesRegex(ValueError, "time limit"):
                solve_multi_lineup_allocation([], {"Scorcher": self.scorcher},
                                             target_count=1, contest_distribution={"Scorcher": 1})

    def test_exported_salary_is_authoritative_and_multiplier_applies_only_to_points(self):
        """A legacy DFF preference cannot replace or multiply an exported card salary."""
        from dataclasses import replace
        card = OwnedCard(
            card_id="c1",
            player_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            multiplier=1.3,
            roster_salary=11050,
            status="Active",
            source_row=2,
        )
        proj = DFFPlayerProjection(
            player_name="Josh Allen",
            athlete_key="joshallen",
            team="BUF",
            position="QB",
            salary=8500,
            raw_projection=22.0,
            source_projection=22.0,
        )

        for source in ("dff", "roster"):
            for projection_salary in (8500, 0):
                joined = join_cards_with_projections([card], [replace(proj, salary=projection_salary)], salary_source=source)[0]
                self.assertEqual(joined.weekly_salary, 11050)
                self.assertEqual(joined.salary_source, "roster")
                self.assertTrue(joined.is_eligible)
                self.assertAlmostEqual(joined.raw_projection, 22.0)
                self.assertAlmostEqual(joined.adjusted_projection, 22.0 * 1.3)
        for invalid_salary in (0, -100):
            joined = join_cards_with_projections([replace(card, roster_salary=invalid_salary)], [proj])[0]
            self.assertFalse(joined.is_eligible)
            self.assertIn("Roster salary", joined.ineligibility_reason)
        joined_listed = join_cards_with_projections([replace(card, status="Listed")], [proj])[0]
        self.assertTrue(joined_listed.is_matched)
        self.assertTrue(joined_listed.is_eligible)
        with self.assertRaisesRegex(ValueError, "required columns"):
            parse_roster_cards(io.StringIO("Player,Position,Multiplier\nJosh Allen,QB,1.3\n"))

    def test_projection_overrides_survive_refresh(self):
        """User projection override modifies projection and survives a fresh DFF pull."""
        overrides = {"joshallen": 25.5}
        raw_feed_v1 = [
            {"first_name": "Josh", "last_name": "Allen", "team": "BUF", "position_code": "QB", "salary": 8000, "ppg": "20.0"}
        ]
        snap_v1 = parse_dff_players(raw_feed_v1, "slate1", user_overrides=overrides, min_offensive_players=1)
        player_v1 = snap_v1.players[0]
        self.assertTrue(player_v1.is_overridden)
        self.assertEqual(player_v1.raw_projection, 25.5)
        self.assertEqual(player_v1.source_projection, 20.0)

        # Fresh pull with updated DFF projection (e.g. 21.0)
        raw_feed_v2 = [
            {"first_name": "Josh", "last_name": "Allen", "team": "BUF", "position_code": "QB", "salary": 8200, "ppg": "21.0"}
        ]
        snap_v2 = parse_dff_players(raw_feed_v2, "slate1", user_overrides=overrides, min_offensive_players=1)
        player_v2 = snap_v2.players[0]
        self.assertTrue(player_v2.is_overridden)
        # Override is preserved
        self.assertEqual(player_v2.raw_projection, 25.5)
        # Source projection reflects the new update
        self.assertEqual(player_v2.source_projection, 21.0)

    def test_copy_counts_and_cross_lineup_reuse_prevention(self):
        """Two copies of the same athlete can be in separate lineups, but a single copy cannot be reused."""
        c1 = OwnedCard("c1", "Josh Allen", "joshallen", "BUF", "QB", 1.2, 8000, "Active", 2)
        c2 = OwnedCard("c2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 3)

        pcard1 = PlannerCard(c1, 20.0, 24.0, 8000, "roster", True)
        pcard2 = PlannerCard(c2, 20.0, 20.0, 8000, "roster", True)

        # Lineup 1 uses c1
        l1 = PlannerLineup(
            lineup_id="L1",
            contest_name="Scorcher",
            slots=[LineupSlotAssignment("QB", "QB", pcard1)],
        )
        # Lineup 2 uses c2 (allowed because separate copy)
        l2 = PlannerLineup(
            lineup_id="L2",
            contest_name="Scorcher",
            slots=[LineupSlotAssignment("QB", "QB", pcard2)],
        )
        used_map = {"c1": "L1", "c2": "L2"}
        valid1, _ = validate_lineup(l1, self.scorcher, used_map)
        valid2, _ = validate_lineup(l2, self.scorcher, used_map)
        # Slots are incomplete in this unit snippet, but check specifically for reuse error:
        self.assertFalse(any("already used" in err for err in l1.validation_errors))
        self.assertFalse(any("already used" in err for err in l2.validation_errors))

        # Now test illegal reuse of c1 in Lineup 2
        l2_illegal = PlannerLineup(
            lineup_id="L2",
            contest_name="Scorcher",
            slots=[LineupSlotAssignment("QB", "QB", pcard1)],
        )
        valid, errors = validate_lineup(l2_illegal, self.scorcher, {"c1": "L1"})
        self.assertFalse(valid)
        self.assertTrue(any("already used in L1" in err for err in errors))

    def test_athlete_uniqueness_within_single_lineup(self):
        """Even if user owns 2 copies, the same athlete cannot be placed twice in one lineup."""
        c1 = OwnedCard("c1", "Josh Allen", "joshallen", "BUF", "QB", 1.2, 8000, "Active", 2)
        c2 = OwnedCard("c2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 3)
        pcard1 = PlannerCard(c1, 20.0, 24.0, 8000, "roster", True)
        pcard2 = PlannerCard(c2, 20.0, 20.0, 8000, "roster", True)

        lineup = PlannerLineup(
            lineup_id="L1",
            contest_name="Wildfire",
            slots=[
                LineupSlotAssignment("QB", "QB", pcard1),
                LineupSlotAssignment("Flex1", "Flex", pcard2),
            ],
        )
        valid, errors = validate_lineup(lineup, self.wildfire)
        self.assertFalse(valid)
        self.assertTrue(any("Duplicate athlete in lineup" in err for err in errors))

    def test_manual_swap_immediately_recalculates(self):
        """Swapping a player recalculates salary, points, and validity instantly."""
        qb1 = PlannerCard(OwnedCard("qb1", "QB A", "qba", "KC", "QB", 1.0, 7000, "Active", 2), 18.0, 18.0, 7000, "dff", True)
        qb2 = PlannerCard(OwnedCard("qb2", "QB B", "qbb", "BUF", "QB", 1.2, 8000, "Active", 3), 22.0, 26.4, 8000, "dff", True)
        rb = PlannerCard(OwnedCard("rb1", "RB A", "rba", "DET", "RB", 1.0, 6000, "Active", 4), 15.0, 15.0, 6000, "dff", True)
        wr = PlannerCard(OwnedCard("wr1", "WR A", "wra", "DAL", "WR", 1.0, 6000, "Active", 5), 14.0, 14.0, 6000, "dff", True)
        te = PlannerCard(OwnedCard("te1", "TE A", "tea", "SF", "TE", 1.0, 5000, "Active", 6), 12.0, 12.0, 5000, "dff", True)
        flx = PlannerCard(OwnedCard("flx1", "WR B", "wrb", "PHI", "WR", 1.0, 5000, "Active", 7), 10.0, 10.0, 5000, "dff", True)

        lineup = PlannerLineup(
            lineup_id="L1",
            contest_name="Scorcher",
            slots=[
                LineupSlotAssignment("QB", "QB", qb1),
                LineupSlotAssignment("RB", "RB", rb),
                LineupSlotAssignment("WR", "WR", wr),
                LineupSlotAssignment("TE", "TE", te),
                LineupSlotAssignment("Flex", "Flex", flx),
            ],
        )
        plan = WeeklyPlan(
            plan_id="p1",
            name="Plan 1",
            created_at="now",
            updated_at="now",
            slate_id="s1",
            salary_source="dff",
            contests=self.contests,
            lineups=[lineup],
            roster_cards=[qb1, qb2, rb, wr, te, flx],
            projection_overrides={},
            excluded_athlete_keys=[],
        )
        recalculate_lineup(lineup, self.scorcher)
        initial_salary = lineup.total_salary
        initial_proj = lineup.total_projection
        self.assertEqual(initial_salary, 29000)
        self.assertEqual(initial_proj, 69.0)

        # Swap QB A with QB B
        success, msg = swap_card_in_lineup(plan, "L1", "QB", "qb2")
        self.assertTrue(success)
        self.assertEqual(lineup.total_salary, 30000)
        self.assertEqual(lineup.total_projection, 77.4)
        self.assertTrue(lineup.is_valid)

    def test_locks_preserved_during_regeneration(self):
        """Locked cards and lineups must not be replaced during regeneration."""
        qb1 = PlannerCard(OwnedCard("qb1", "QB Locked", "qblocked", "KC", "QB", 1.0, 7000, "Active", 2), 15.0, 15.0, 7000, "dff", True)
        qb2 = PlannerCard(OwnedCard("qb2", "QB Better", "qbbetter", "BUF", "QB", 1.0, 7000, "Active", 3), 25.0, 25.0, 7000, "dff", True)
        rb = PlannerCard(OwnedCard("rb1", "RB A", "rba", "DET", "RB", 1.0, 6000, "Active", 4), 15.0, 15.0, 6000, "dff", True)
        wr = PlannerCard(OwnedCard("wr1", "WR A", "wra", "DAL", "WR", 1.0, 6000, "Active", 5), 14.0, 14.0, 6000, "dff", True)
        te = PlannerCard(OwnedCard("te1", "TE A", "tea", "SF", "TE", 1.0, 5000, "Active", 6), 12.0, 12.0, 5000, "dff", True)
        flx = PlannerCard(OwnedCard("flx1", "WR B", "wrb", "PHI", "WR", 1.0, 5000, "Active", 7), 10.0, 10.0, 5000, "dff", True)

        cards = [qb1, qb2, rb, wr, te, flx]
        initial_lineup = PlannerLineup(
            lineup_id="lineup_1_scorcher",
            contest_name="Scorcher",
            slots=[
                LineupSlotAssignment("QB", "QB", qb1, is_locked=True),
                LineupSlotAssignment("RB", "RB", rb),
                LineupSlotAssignment("WR", "WR", wr),
                LineupSlotAssignment("TE", "TE", te),
                LineupSlotAssignment("Flex", "Flex", flx),
            ],
        )
        plan = WeeklyPlan(
            plan_id="p1",
            name="Plan",
            created_at="now",
            updated_at="now",
            slate_id="s1",
            salary_source="dff",
            contests=self.contests,
            lineups=[initial_lineup],
            roster_cards=cards,
            projection_overrides={},
            excluded_athlete_keys=[],
        )

        new_plan = solve_multi_lineup_allocation(
            cards=cards,
            contests=self.contests,
            target_count=1,
            contest_distribution={"Scorcher": 1},
            existing_plan=plan,
        )
        assigned_qb = next(s.card for s in new_plan.lineups[0].slots if s.slot_name == "QB")
        # QB Locked must remain assigned even though QB Better has 25 pts vs 15 pts!
        self.assertEqual(assigned_qb.card.card_id, "qb1")

    def test_payout_bands_evaluate_without_double_counting(self):
        """Probabilistic payout decomposes overlapping thresholds into disjoint bands."""
        res = evaluate_probabilistic_payout(125.0, self.scorcher, lineup_stdev=15.0)
        # Sum of mutually exclusive band probabilities + out of money must equal exactly 1.0
        band_probs = sum(b.probability for b in res.band_outcomes)
        total_prob = band_probs + res.out_of_the_money_probability
        self.assertAlmostEqual(total_prob, 1.0, places=4)
        for b in res.band_outcomes:
            self.assertGreaterEqual(b.probability, 0.0)
            self.assertEqual(round(b.expected_value, 4), round(b.probability * b.prize_per_entry, 4))

    def test_synthetic_allocation_proves_joint_solver_beats_greedy(self):
        """A synthetic portfolio allocation where solving Lineup 1 greedily yields lower weekly payout than joint Payout MIP."""
        # Scorcher tiers:
        # Top 100 cutoff: 110.45, prize: $9
        # Top 50 cutoff: 122.02, prize: $15
        # Top 20 cutoff: 132.30, prize: $25
        # Top 200 cutoff: 99.66, prize: $4
        # Below 99.66: $0
        #
        # Available cards:
        # QBs: qb1 (30 pts, $5000), qb2 (20 pts, $5000)
        # RBs: rb1 (25 pts, $5000), rb2 (20 pts, $5000)
        # WRs: wr1 (25 pts, $5000), wr2 (20 pts, $5000)
        # TEs: te1 (23 pts, $5000), te2 (19 pts, $5000)
        # Flex (WRs): flx1 (22 pts, $5000), flx2 (19 pts, $5000)
        qb1 = PlannerCard(OwnedCard("qb1", "QB 1", "qb1", "KC", "QB", 1.0, 5000, "Active", 2), 30.0, 30.0, 5000, "dff", True)
        qb2 = PlannerCard(OwnedCard("qb2", "QB 2", "qb2", "BUF", "QB", 1.0, 5000, "Active", 3), 20.0, 20.0, 5000, "dff", True)
        rb1 = PlannerCard(OwnedCard("rb1", "RB 1", "rb1", "DET", "RB", 1.0, 5000, "Active", 4), 25.0, 25.0, 5000, "dff", True)
        rb2 = PlannerCard(OwnedCard("rb2", "RB 2", "rb2", "SF", "RB", 1.0, 5000, "Active", 5), 20.0, 20.0, 5000, "dff", True)
        wr1 = PlannerCard(OwnedCard("wr1", "WR 1", "wr1", "DAL", "WR", 1.0, 5000, "Active", 6), 25.0, 25.0, 5000, "dff", True)
        wr2 = PlannerCard(OwnedCard("wr2", "WR 2", "wr2", "MIA", "WR", 1.0, 5000, "Active", 7), 20.0, 20.0, 5000, "dff", True)
        te1 = PlannerCard(OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 8), 23.0, 23.0, 5000, "dff", True)
        te2 = PlannerCard(OwnedCard("te2", "TE 2", "te2", "BAL", "TE", 1.0, 5000, "Active", 9), 19.0, 19.0, 5000, "dff", True)
        flx1 = PlannerCard(OwnedCard("flx1", "FLX 1", "flx1", "PHI", "WR", 1.0, 5000, "Active", 10), 22.0, 22.0, 5000, "dff", True)
        flx2 = PlannerCard(OwnedCard("flx2", "FLX 2", "flx2", "NYG", "WR", 1.0, 5000, "Active", 11), 19.0, 19.0, 5000, "dff", True)

        all_cards = [qb1, qb2, rb1, rb2, wr1, wr2, te1, te2, flx1, flx2]

        # Greedy simulation:
        # Lineup 1 picks best cards: 30 + 25 + 25 + 23 + 22 = 125.0 pts (Top 50 -> $10)
        # Lineup 2 picks remaining: 20 + 20 + 20 + 19 + 19 = 98.0 pts (Top 360 -> $2)
        # Greedy total payout: $12.00
        tier_l1 = evaluate_deterministic_tier(125.0, self.scorcher)
        tier_l2 = evaluate_deterministic_tier(98.0, self.scorcher)
        greedy_total_payout = tier_l1.estimated_payout + tier_l2.estimated_payout
        self.assertEqual(greedy_total_payout, 12.0)

        # Joint Payout MIP solver (with quality_threshold=0.0 to focus on joint payout vs greedy tradeoff across full inventory):
        plan = solve_multi_lineup_allocation(
            cards=all_cards,
            contests=self.contests,
            target_count=2,
            contest_distribution={"Scorcher": 2},
            quality_threshold=0.0,
        )
        self.assertEqual(len(plan.lineups), 2)
        # Joint solver balances the two lineups to achieve Top 100 on both L1 and L2: $8 + $8 = $16
        self.assertEqual(plan.total_weekly_estimated_payout, 16.0)
        self.assertGreater(plan.total_weekly_estimated_payout, greedy_total_payout)

    def test_untouched_dff_cheatsheet_parse(self):
        """Parse untouched DFF cheatsheet CSV and verify offensive player counts and metadata."""
        cheatsheet_path = Path("/Users/shyampatel/Desktop/GB/Projections/DFF_NFL_cheatsheet_2026-09-09.csv")
        if not cheatsheet_path.exists():
            self.skipTest(f"Cheatsheet file not found at {cheatsheet_path}")

        snapshot = parse_projections_csv_stream(
            io.StringIO(cheatsheet_path.read_text(encoding="utf-8-sig"))
        )
        self.assertEqual(snapshot.offensive_player_count, 443)
        self.assertEqual(snapshot.dst_excluded_count, 32)
        self.assertEqual(snapshot.slate_id, "Wed-Mon")
        self.assertIsNotNone(snapshot.slate_info)
        self.assertIn("Week 1", snapshot.slate_info.start_string)
        # Ensure Patrick Mahomes is present
        mahomes = next((p for p in snapshot.players if p.athlete_key == "patrickmahomes"), None)
        self.assertIsNotNone(mahomes)
        self.assertEqual(mahomes.team, "KC")
        self.assertEqual(mahomes.position, "QB")

    def test_cross_lineup_copy_duplication_swap_invalidates_plan(self):
        """A manual swap that duplicates a card copy across lineups invalidates both lineups and removes payout."""
        c1 = OwnedCard("c1", "Card One", "cardone", "KC", "QB", 1.0, 5000, "Active", 1)
        c2 = OwnedCard("c2", "Card Two", "cardtwo", "BUF", "QB", 1.0, 5000, "Active", 2)
        rb1 = OwnedCard("rb1", "RB 1", "rb1", "DET", "RB", 1.0, 5000, "Active", 3)
        rb2 = OwnedCard("rb2", "RB 2", "rb2", "SF", "RB", 1.0, 5000, "Active", 4)
        wr1 = OwnedCard("wr1", "WR 1", "wr1", "DAL", "WR", 1.0, 5000, "Active", 5)
        wr2 = OwnedCard("wr2", "WR 2", "wr2", "MIA", "WR", 1.0, 5000, "Active", 6)
        te1 = OwnedCard("te1", "TE 1", "te1", "KC", "TE", 1.0, 5000, "Active", 7)
        te2 = OwnedCard("te2", "TE 2", "te2", "BAL", "TE", 1.0, 5000, "Active", 8)
        flx1 = OwnedCard("flx1", "FLX 1", "flx1", "PHI", "WR", 1.0, 5000, "Active", 9)
        flx2 = OwnedCard("flx2", "FLX 2", "flx2", "NYG", "WR", 1.0, 5000, "Active", 10)

        pc1 = PlannerCard(c1, 25.0, 25.0, 5000, "dff", True)
        pc2 = PlannerCard(c2, 24.0, 24.0, 5000, "dff", True)
        prb1 = PlannerCard(rb1, 20.0, 20.0, 5000, "dff", True)
        prb2 = PlannerCard(rb2, 20.0, 20.0, 5000, "dff", True)
        pwr1 = PlannerCard(wr1, 20.0, 20.0, 5000, "dff", True)
        pwr2 = PlannerCard(wr2, 20.0, 20.0, 5000, "dff", True)
        pte1 = PlannerCard(te1, 20.0, 20.0, 5000, "dff", True)
        pte2 = PlannerCard(te2, 20.0, 20.0, 5000, "dff", True)
        pflx1 = PlannerCard(flx1, 20.0, 20.0, 5000, "dff", True)
        pflx2 = PlannerCard(flx2, 20.0, 20.0, 5000, "dff", True)

        l1 = PlannerLineup(
            lineup_id="L1",
            contest_name="Scorcher",
            slots=[
                LineupSlotAssignment("QB", "QB", pc1),
                LineupSlotAssignment("RB", "RB", prb1),
                LineupSlotAssignment("WR", "WR", pwr1),
                LineupSlotAssignment("TE", "TE", pte1),
                LineupSlotAssignment("Flex", "Flex", pflx1),
            ],
        )
        l2 = PlannerLineup(
            lineup_id="L2",
            contest_name="Scorcher",
            slots=[
                LineupSlotAssignment("QB", "QB", pc2),
                LineupSlotAssignment("RB", "RB", prb2),
                LineupSlotAssignment("WR", "WR", pwr2),
                LineupSlotAssignment("TE", "TE", pte2),
                LineupSlotAssignment("Flex", "Flex", pflx2),
            ],
        )
        all_pc = [pc1, pc2, prb1, prb2, pwr1, pwr2, pte1, pte2, pflx1, pflx2]
        plan = WeeklyPlan(
            plan_id="p1",
            name="Plan",
            created_at="now",
            updated_at="now",
            slate_id="s1",
            salary_source="dff",
            contests=self.contests,
            lineups=[l1, l2],
            roster_cards=all_pc,
            projection_overrides={},
            excluded_athlete_keys=[],
        )
        recalculate_lineup(l1, self.scorcher)
        recalculate_lineup(l2, self.scorcher)
        plan.total_weekly_estimated_payout = round(sum(l.estimated_payout for l in plan.lineups if l.is_valid), 2)
        self.assertTrue(l1.is_valid)
        self.assertTrue(l2.is_valid)
        self.assertGreater(plan.total_weekly_estimated_payout, 0.0)

        # Illegal swap: swap L2's QB to pc1 (which is already used in L1)
        success, msg = swap_card_in_lineup(plan, "L2", "QB", "c1")
        self.assertTrue(success)
        # Both lineups must be flagged invalid because copy c1 is reused
        self.assertFalse(l1.is_valid)
        self.assertFalse(l2.is_valid)
        self.assertTrue(any("already used" in err for err in l1.validation_errors))
        self.assertTrue(any("already used" in err for err in l2.validation_errors))
        # Total weekly estimated payout excludes invalid lineups, so it must be 0.0
        self.assertEqual(plan.total_weekly_estimated_payout, 0.0)

    def test_uncalibrated_data_explicitly_labeled_not_invented_ev(self):
        """Deterministic tier comparisons must be labeled as payout_at_projected_score, not expected earnings."""
        eval_res = evaluate_deterministic_tier(135.0, self.scorcher)
        self.assertEqual(eval_res.estimate_type, "payout_at_projected_score")
        self.assertIn("Deterministic tier estimate", eval_res.uncertainty_note)
        self.assertEqual(eval_res.achieved_tier, "Top20")
        self.assertEqual(eval_res.estimated_payout, 25.0)

        # Requirement checklist for live EV is complete and documented
        reqs = get_probabilistic_ranking_requirements()
        self.assertEqual(len(reqs), 4)
        for req in reqs:
            self.assertIn("requirement", req)
            self.assertIn("what_is_needed", req)


if __name__ == "__main__":
    unittest.main()
