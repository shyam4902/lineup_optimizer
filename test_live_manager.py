"""Tests for in-week live lineup manager and pre-kickoff reallocation engine.

Verifies:
1. Separation of kickoff locks from final scores (started players cannot move, in-progress retains variance).
2. Thursday Breakout scenario: surging contender prioritized with premium assets.
3. Monday Player-Choice scenario: floor defense vs cash line.
4. Monday 1st-Place Chase scenario: leading contender properly choosing boom-or-bust upside.
5. In-Progress game handling and dependent multi-lineup card chains.
6. Atomic application of reallocations and constraint preservation.
7. CSV live scores ingestion.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from contest_config import get_default_contests
from live_lineup_manager import (
    GAME_STATUS_FINAL,
    GAME_STATUS_IN_PROGRESS,
    GAME_STATUS_UPCOMING,
    CoordinatedSwap,
    PlayerLiveState,
    apply_coordinated_reallocation,
    identify_promising_lineups,
    load_inprogress_dependent_chain_scenario,
    load_monday_first_place_chase_scenario,
    load_monday_player_choice_scenario,
    load_thursday_breakout_scenario,
    parse_live_scores_csv,
    solve_coordinated_reallocation,
)


class LiveLineupManagerTests(unittest.TestCase):
    def setUp(self):
        self.contests = get_default_contests()

    def test_kickoff_locks_separated_from_score_completion(self):
        """Started players are locked, but in-progress players retain uncertainty while finals have zero variance."""
        now = datetime(2026, 9, 13, 13, 30, tzinfo=timezone.utc)

        # Player whose game is in progress
        p_live = PlayerLiveState(
            athlete_key="joshallen",
            player_name="Josh Allen",
            team="BUF",
            position="QB",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00Z",
            live_points=14.0,
            remaining_projection=8.0,
            remaining_stdev=3.0,
            remaining_is_estimate=True,
        )
        self.assertTrue(p_live.is_locked(now))
        # Total effective mean is live + remaining when the estimate is explicit
        self.assertEqual(p_live.effective_mean(1.0), 22.0)
        # Uncertainty is non-zero
        self.assertGreater(p_live.effective_variance(1.0), 0.0)

        # Without an explicit estimate the seeded projection must not be added again.
        p_unknown = PlayerLiveState(
            athlete_key="joshallen",
            player_name="Josh Allen",
            team="BUF",
            position="QB",
            game_status=GAME_STATUS_IN_PROGRESS,
            live_points=14.0,
            remaining_projection=8.0,
            remaining_stdev=3.0,
        )
        self.assertEqual(p_unknown.effective_mean(1.0), 14.0)

        # Player whose game is finished
        p_final = PlayerLiveState(
            athlete_key="colbyparkinson",
            player_name="Colby Parkinson",
            team="LAR",
            position="TE",
            game_status=GAME_STATUS_FINAL,
            kickoff_time="2026-09-10T20:15:00Z",
            final_points=21.0,
            remaining_projection=0.0,
            remaining_stdev=0.0,
        )
        self.assertTrue(p_final.is_locked(now))
        self.assertEqual(p_final.effective_mean(1.5), 31.5)
        # Variance is strictly 0 for final
        self.assertEqual(p_final.effective_variance(1.5), 0.0)

        # Player whose game is upcoming
        p_upcoming = PlayerLiveState(
            athlete_key="kyrenwilliams",
            player_name="Kyren Williams",
            team="LAR",
            position="RB",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-13T16:25:00Z",
            remaining_projection=18.0,
            remaining_stdev=4.0,
        )
        self.assertFalse(p_upcoming.is_locked(now))
        self.assertEqual(p_upcoming.effective_mean(1.0), 18.0)

    def test_thursday_breakout_scenario(self):
        """Cheap TE breakout on Thursday night identifies surging contender and reallocates premium Sunday assets."""
        plan, live_states = load_thursday_breakout_scenario()

        evals = identify_promising_lineups(plan, live_states)
        self.assertEqual(len(evals), 2)
        top_eval = evals[0]

        # Lineup 1 should be identified as Surging Contender
        self.assertEqual(top_eval.lineup_id, "scorcher_1")
        self.assertEqual(top_eval.leverage_status, "Surging Contender")
        self.assertGreaterEqual(top_eval.banked_score, 30.0)

        # Solve reallocation
        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)
        self.assertGreaterEqual(len(realloc.swaps), 1)

        # Colby Parkinson must NOT be swapped because his Thursday game is FINAL
        swapped_players = [s.current_player_name for s in realloc.swaps]
        self.assertNotIn("Colby Parkinson", swapped_players)

        # Lineup 1 receives upgraded player
        l1_swaps = [s for s in realloc.swaps if s.lineup_id == "scorcher_1"]
        self.assertTrue(len(l1_swaps) > 0)
        self.assertIn("Amon-Ra St. Brown", [s.new_player_name for s in l1_swaps] + [s.new_player_name for s in realloc.swaps])

        # Apply reallocations atomically
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success)
        # Parkinson still in slot
        l1 = next(l for l in plan.lineups if l.lineup_id == "scorcher_1")
        te_slot = next(s for s in l1.slots if s.slot_name == "TE")
        self.assertEqual(te_slot.card.card.player_name, "Colby Parkinson")
        # Lineup remains valid and under cap
        self.assertTrue(l1.is_valid)

    def test_monday_player_choice_scenario_floor_vs_cash_line(self):
        """Monday scenario assigns consistent player to protect Top 10 and volatile player to trailing cash chase."""
        plan, live_states = load_monday_player_choice_scenario()

        # Before reallocation, Lineup 1 has volatile and Lineup 2 has consistent (suboptimal)
        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)

        # Optimizer should swap: assign Tyler Lockett (consistent) to scorcher_1 and Christian Watson to scorcher_2
        l1_swap = next((s for s in realloc.swaps if s.lineup_id == "scorcher_1"), None)
        l2_swap = next((s for s in realloc.swaps if s.lineup_id == "scorcher_2"), None)

        self.assertIsNotNone(l1_swap)
        self.assertEqual(l1_swap.new_player_name, "Tyler Lockett")
        self.assertIsNotNone(l2_swap)
        self.assertEqual(l2_swap.new_player_name, "Christian Watson")

        # Rationale explains decision
        self.assertIn("Tyler Lockett", l1_swap.rationale)

        # Apply and verify
        success, _ = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success)

        evals_post = identify_promising_lineups(plan, live_states)
        ev_l1 = next(e for e in evals_post if e.lineup_id == "scorcher_1")
        # Lineup 1 achieves expected payout protecting Top 10 in Scorcher ($45 payout tier)
        self.assertAlmostEqual(ev_l1.expected_payout, 41.5, places=1)

    def test_monday_first_place_chase_scenario(self):
        """Leading contender properly chooses boom-or-bust player when chasing 1st place."""
        plan, live_states = load_monday_first_place_chase_scenario()

        evals = identify_promising_lineups(plan, live_states)
        self.assertEqual(evals[0].leverage_status, "Top Contender (Chasing 1st)")

        # Before swap: Safe Veteran has 15.0 remaining projection
        self.assertAlmostEqual(evals[0].projected_remaining, 15.0, places=1)
        self.assertAlmostEqual(evals[0].projected_total, 170.0, places=1)
        self.assertEqual(evals[0].top_tier_name, "Top7")

        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)

        # Should recommend swapping Safe Veteran for Boom Rookie
        self.assertEqual(len(realloc.swaps), 1)
        swap = realloc.swaps[0]
        self.assertEqual(swap.new_player_name, "Boom Rookie")
        self.assertIn("upside", swap.rationale)

        # Apply swap locally and verify post-swap evaluation
        success, _ = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success)

        evals_post = identify_promising_lineups(plan, live_states)
        ev_post = evals_post[0]
        # Discrete bimodal outcomes: 0.6 * 5.0 + 0.4 * 33.0 = 16.2 remaining expected points
        self.assertAlmostEqual(ev_post.projected_remaining, 16.2, places=1)
        self.assertAlmostEqual(ev_post.projected_total, 171.2, places=1)
        self.assertEqual(ev_post.top_tier_name, "Top7")
        # Probability of reaching Top7 (187.67 pts) with 155 banked pts:
        # High branch is 155 + 33 = 188.0 >= 187.67 (40% branch prob)
        self.assertAlmostEqual(ev_post.p_top, 0.40, places=2)

    def test_two_lineup_monday_first_place_preserves_multiple_copies(self):
        """Two-lineup Monday scenario with two copies of every card must assign Boom Rookie to both entries for $66 payout.

        Regression check:
        Previously, deduplicating equivalent physical cards during candidate reduction
        collapsed available inventory to 1 copy in the MIP. In a two-lineup version where each
        lineup had Safe Veteran ($20 payout), the solver only allocated 1 Boom Rookie ($33),
        yielding $53 ($33 + $20).
        Preserving physical copy capacities across equivalence classes enables allocating one Boom
        Rookie copy to each lineup, achieving the full legal $66 ($33 + $33) payout.
        """
        from multi_lineup_planner import (
            OwnedCard,
            PlannerCard,
            PlannerLineup,
            LineupSlotAssignment,
            WeeklyPlan,
            recalculate_lineup,
        )

        plan1, live_states = load_monday_first_place_chase_scenario()
        wildfire = self.contests["Wildfire"]

        cards_lineup1 = [slot.card.card for slot in plan1.lineups[0].slots]
        c_boom_1 = next(c.card for c in plan1.roster_cards if c.card.card_id == "c_boom_rookie")

        cards_lineup2 = [
            OwnedCard(
                f"{c.card_id}_copy2",
                c.player_name,
                c.athlete_key,
                c.team,
                c.position,
                c.multiplier,
                c.roster_salary,
                c.status,
                c.source_row + 100,
            )
            for c in cards_lineup1
        ]
        c_boom_2 = OwnedCard(
            "c_boom_rookie_copy2",
            c_boom_1.player_name,
            c_boom_1.athlete_key,
            c_boom_1.team,
            c_boom_1.position,
            c_boom_1.multiplier,
            c_boom_1.roster_salary,
            c_boom_1.status,
            c_boom_1.source_row + 100,
        )

        all_cards = cards_lineup1 + cards_lineup2 + [c_boom_1, c_boom_2]
        pc_dict = {
            c.card_id: PlannerCard(
                card=c,
                raw_projection=15.0,
                adjusted_projection=15.0,
                weekly_salary=c.roster_salary,
                salary_source="dff",
                is_matched=True,
            )
            for c in all_cards
        }

        l1 = PlannerLineup(
            "wildfire_1",
            "Wildfire",
            [
                LineupSlotAssignment(slot.slot_name, slot.slot_kind, pc_dict[slot.card.card.card_id])
                for slot in plan1.lineups[0].slots
            ],
        )
        recalculate_lineup(l1, wildfire)

        l2 = PlannerLineup(
            "wildfire_2",
            "Wildfire",
            [
                LineupSlotAssignment(slot.slot_name, slot.slot_kind, pc_dict[f"{slot.card.card.card_id}_copy2"])
                for slot in plan1.lineups[0].slots
            ],
        )
        recalculate_lineup(l2, wildfire)

        plan2 = WeeklyPlan(
            plan_id="monday_first_place_2lineup",
            name="Monday 1st-Place 2-Lineup",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            slate_id="slate",
            salary_source="dff",
            contests=self.contests,
            lineups=[l1, l2],
            roster_cards=list(pc_dict.values()),
            projection_overrides={},
            excluded_athlete_keys=[],
        )

        realloc = solve_coordinated_reallocation(plan2, live_states)
        self.assertTrue(realloc.is_valid)
        self.assertEqual(realloc.optimality_status, "Proven Optimal")
        self.assertTrue(realloc.is_proven_optimal)
        self.assertAlmostEqual(realloc.portfolio_payout_before, 50.00, places=2)
        # Must achieve $106.00 with Boom Rookie upgrades
        self.assertAlmostEqual(realloc.portfolio_payout_after, 106.00, places=2)
        self.assertAlmostEqual(realloc.portfolio_payout_delta, 56.00, places=2)

        # Both lineups must receive a Boom Rookie copy
        swapped_players = {s.new_player_name for s in realloc.swaps}
        self.assertEqual(swapped_players, {"Boom Rookie"})
        self.assertEqual(len(realloc.swaps), 2)

        # Successfully applies atomically to plan
        success, msg = apply_coordinated_reallocation(plan2, realloc, live_states)
        self.assertTrue(success, msg)
        for l in plan2.lineups:
            self.assertTrue(l.is_valid)
            assigned_card = l.slots[-1].card
            self.assertEqual(assigned_card.card.player_name, "Boom Rookie")

    def test_inprogress_game_and_dependent_chain(self):
        """In-progress games are locked; multi-lineup dependent card chain executes atomically."""
        plan, live_states = load_inprogress_dependent_chain_scenario()

        # Check in-progress slots are locked
        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)

        # None of the in-progress Bills players may be swapped
        locked_athletes = {"joshallen", "jamescook", "stefondiggs", "daltonkincaid", "khalilshakir"}
        for s in realloc.swaps:
            self.assertNotIn(s.current_card_id, ["c_inprog_allen", "c_inprog_cook", "c_inprog_diggs", "c_inprog_kincaid", "c_inprog_shakir"])

        # Jahmyr Gibbs (bench RB) should be utilized in late games
        gibbs_swap = next((s for s in realloc.swaps if s.new_player_name == "Jahmyr Gibbs"), None)
        self.assertIsNotNone(gibbs_swap)

        # Apply reallocations
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success)
        for l in plan.lineups:
            self.assertTrue(l.is_valid)

    def test_parse_live_scores_csv(self):
        """Test parsing live scores CSV stream."""
        csv_data = (
            "Player Name,Team,Position,Status,Live Points,Final Points,Remaining Proj,Kickoff\n"
            "Josh Allen,BUF,QB,IN_PROGRESS,18.5,0.0,6.0,2026-09-13T13:00:00-04:00\n"
            "Colby Parkinson,LAR,TE,FINAL,0.0,21.0,0.0,2026-09-10T20:15:00-04:00\n"
            "Tyler Lockett,SEA,WR,UPCOMING,0.0,0.0,15.0,2026-09-14T20:15:00-04:00\n"
        )
        states = parse_live_scores_csv(csv_data)
        self.assertEqual(len(states), 3)
        self.assertEqual(states[0].player_name, "Josh Allen")
        self.assertEqual(states[0].game_status, GAME_STATUS_IN_PROGRESS)
        self.assertEqual(states[0].live_points, 18.5)
        self.assertEqual(states[1].player_name, "Colby Parkinson")
        self.assertEqual(states[1].game_status, GAME_STATUS_FINAL)
        self.assertEqual(states[1].final_points, 21.0)
        self.assertEqual(states[2].game_status, GAME_STATUS_UPCOMING)

    def test_lower_average_volatile_player_with_higher_expected_payout(self):
        """A volatile player with lower average projection must be chosen if payoff distribution yields higher EV."""
        plan, live_states = load_monday_first_place_chase_scenario()
        c_boom = next(c for c in plan.roster_cards if c.card.card_id == "c_boom_rookie")

        # Configure Boom Rookie to have LOWER average than Safe Veteran (13.6 vs 15.0 pts)
        # 60% chance of 0 pts, 40% chance of 34 pts -> mean = 13.6 pts
        live_states["boomrookie"].bimodal_outcomes = [(0.0, 0.6), (34.0, 0.4)]
        live_states["boomrookie"].remaining_projection = 13.6
        c_boom.raw_projection = 13.6
        c_boom.adjusted_projection = 13.6

        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)
        self.assertEqual(len(realloc.swaps), 1)
        swap = realloc.swaps[0]
        # Despite lower average projection (13.6 < 15.0), the solver chooses Boom Rookie for its higher payout EV
        self.assertEqual(swap.new_player_name, "Boom Rookie")
        self.assertEqual(swap.current_player_name, "Safe Veteran")

    def test_apply_reallocations_rejects_stale_or_invalid_recommendations(self):
        """Reallocation application must reject stale states, locked players, and invalid plans without mutating lineups."""
        plan, live_states = load_thursday_breakout_scenario()
        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)
        self.assertGreater(len(realloc.swaps), 0)

        # 1. Reject if plan is flagged invalid
        realloc.is_valid = False
        realloc.validation_errors = ["Stale solver state"]
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertFalse(success)
        self.assertIn("Cannot apply invalid plan", msg)
        realloc.is_valid = True
        realloc.validation_errors = []

        # 2. Reject stale recommendation if current card does not match expectation
        l1 = next(l for l in plan.lineups if l.lineup_id == "scorcher_1")
        # Target a slot that has an active swap in realloc
        swap_slot = next(s for s in l1.slots if any(sw.lineup_id == "scorcher_1" and sw.slot_name == s.slot_name for sw in realloc.swaps))
        orig_card = swap_slot.card
        swap_slot.card = None  # User manually removed slot in the interim
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertFalse(success)
        self.assertIn("Stale recommendation", msg)
        swap_slot.card = orig_card

        # 3. Reject if incoming card is locked
        first_swap = realloc.swaps[0]
        incoming_akey = next(c.card.athlete_key for c in plan.roster_cards if c.card.card_id == first_swap.new_card_id)
        if incoming_akey in live_states:
            live_states[incoming_akey].game_status = GAME_STATUS_FINAL
            success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
            self.assertFalse(success)
            self.assertIn("already locked", msg)
            live_states[incoming_akey].game_status = GAME_STATUS_UPCOMING

    def test_execution_steps_generate_dependent_sequence_and_flag_cycles(self):
        """Execution steps must sequence dependent swaps legally and flag circular swaps explicitly."""
        plan, live_states = load_monday_player_choice_scenario()
        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)

        # Monday player choice exchanges cards between scorcher_1 and scorcher_2 (a 2-way swap cycle)
        # Because neither can be placed directly without violating copy limits, the steps must flag the cycle
        step_text = " ".join(realloc.execution_steps)
        self.assertIn("Notice: Direct sequential execution blocked by mutual card exchange cycle", step_text)
        self.assertIn("Cycle Move", step_text)

        # Now test a clean sequential chain: bench -> L1, L1's card -> L2, L2's card -> bench
        chain_swaps = [
            CoordinatedSwap("scorcher_2", "Flex", "Flex", "c_davis", "Gabe Davis", "c_gibbs", "Jahmyr Gibbs", 2800, 7.6, 1.2, "Upgrade"),
        ]
        from live_lineup_manager import _generate_execution_steps
        steps = _generate_execution_steps(chain_swaps)
        self.assertEqual(len(steps), 1)
        self.assertIn("Step 1:", steps[0])
        self.assertIn("from bench/pool", steps[0])

    def test_two_flex_joint_distribution_selects_optimal_mixed_pair(self):
        """Two open FLEX slots with guaranteed and volatile choices must select the highest joint expected payout pair.

        Scenario setup:
        - 145 banked points across 4 locked slots in Wildfire.
        - Two open FLEX slots.
        - Two guaranteed 15.0 pt players (G1, G2) with 0 stdev.
        - Two independent 50/50 volatile players (V1, V2) scoring either 0 or 30 pts (mean 15.0 pts).

        Evaluated under evaluate_lineup_live_state():
        - (G1, G2) -> deterministic 175 pts -> clears Top 20 (174.31) paying $30.00.
        - (V1, V2) -> 25% 145 ($10), 50% 175 ($30), 25% 205 ($60) -> EV = $32.50.
        - (G, V) mixed pair -> 50% 160 ($15), 50% 190 ($60) -> EV = $37.50.

        A separable per-slot delta heuristic falsely favored (V1, V2) for $35.00 (+7.50 + 7.50 over 20).
        The joint outcome-weighted solver must select the legal mixed pair (G, V) achieving $37.50.
        """
        from multi_lineup_planner import (
            LineupSlotAssignment,
            OwnedCard,
            PlannerCard,
            PlannerLineup,
            WeeklyPlan,
            recalculate_lineup,
        )
        from live_lineup_manager import evaluate_lineup_live_state

        wildfire = self.contests["Wildfire"]

        locked_cards = [
            PlannerCard(
                OwnedCard(f"c_lock_{i}", f"Locked {i}", f"lock_{i}", "BUF", pos, 1.0, 7000, "Active", i),
                raw_projection=36.25,
                adjusted_projection=36.25,
                weekly_salary=7000,
                salary_source="dff",
                is_matched=True,
            )
            for i, pos in enumerate(["QB", "RB", "WR", "TE"])
        ]

        g1 = PlannerCard(OwnedCard("c_g1", "Guaranteed 1", "g1", "KC", "RB", 1.0, 5000, "Active", 10), 15.0, 15.0, 5000, "dff", True)
        g2 = PlannerCard(OwnedCard("c_g2", "Guaranteed 2", "g2", "DET", "WR", 1.0, 5000, "Active", 11), 15.0, 15.0, 5000, "dff", True)
        v1 = PlannerCard(OwnedCard("c_v1", "Volatile 1", "v1", "MIA", "WR", 1.0, 5000, "Active", 12), 15.0, 15.0, 5000, "dff", True)
        v2 = PlannerCard(OwnedCard("c_v2", "Volatile 2", "v2", "SF", "TE", 1.0, 5000, "Active", 13), 15.0, 15.0, 5000, "dff", True)

        live_states = {
            "lock_0": PlayerLiveState("lock_0", "Locked 0", "BUF", "QB", GAME_STATUS_FINAL, final_points=36.25),
            "lock_1": PlayerLiveState("lock_1", "Locked 1", "BUF", "RB", GAME_STATUS_FINAL, final_points=36.25),
            "lock_2": PlayerLiveState("lock_2", "Locked 2", "BUF", "WR", GAME_STATUS_FINAL, final_points=36.25),
            "lock_3": PlayerLiveState("lock_3", "Locked 3", "BUF", "TE", GAME_STATUS_FINAL, final_points=36.25),
            "g1": PlayerLiveState("g1", "Guaranteed 1", "KC", "RB", GAME_STATUS_UPCOMING, remaining_projection=15.0, remaining_stdev=0.0),
            "g2": PlayerLiveState("g2", "Guaranteed 2", "DET", "WR", GAME_STATUS_UPCOMING, remaining_projection=15.0, remaining_stdev=0.0),
            "v1": PlayerLiveState("v1", "Volatile 1", "MIA", "WR", GAME_STATUS_UPCOMING, remaining_projection=15.0, remaining_stdev=15.0, bimodal_outcomes=[(0.0, 0.5), (30.0, 0.5)]),
            "v2": PlayerLiveState("v2", "Volatile 2", "SF", "TE", GAME_STATUS_UPCOMING, remaining_projection=15.0, remaining_stdev=15.0, bimodal_outcomes=[(0.0, 0.5), (30.0, 0.5)]),
        }

        # Exhaustive verification of all legal 2-card combinations under evaluate_lineup_live_state()
        slots_template = [
            LineupSlotAssignment("QB", "QB", locked_cards[0], is_locked=True),
            LineupSlotAssignment("RB", "RB", locked_cards[1], is_locked=True),
            LineupSlotAssignment("WR", "WR", locked_cards[2], is_locked=True),
            LineupSlotAssignment("TE", "TE", locked_cards[3], is_locked=True),
        ]

        def eval_pair(card_a, card_b):
            lineup = PlannerLineup(
                "wf_test",
                "Wildfire",
                slots_template + [
                    LineupSlotAssignment("Flex", "Flex", card_a),
                    LineupSlotAssignment("Flex2", "Flex", card_b),
                ],
            )
            recalculate_lineup(lineup, wildfire)
            return evaluate_lineup_live_state(lineup, wildfire, live_states).expected_payout

        self.assertAlmostEqual(eval_pair(g1, g2), 55.00, places=2)
        self.assertAlmostEqual(eval_pair(v1, v2), 57.25, places=2)
        self.assertAlmostEqual(eval_pair(g1, v1), 62.50, places=2)
        self.assertAlmostEqual(eval_pair(g1, v2), 62.50, places=2)
        self.assertAlmostEqual(eval_pair(g2, v1), 62.50, places=2)
        self.assertAlmostEqual(eval_pair(g2, v2), 62.50, places=2)

        # Baseline plan starts with (G1, G2)
        lineup_initial = PlannerLineup(
            "wf_1",
            "Wildfire",
            slots_template + [
                LineupSlotAssignment("Flex", "Flex", g1),
                LineupSlotAssignment("Flex2", "Flex", g2),
            ],
        )
        recalculate_lineup(lineup_initial, wildfire)

        plan = WeeklyPlan(
            plan_id="two_flex_joint_plan",
            name="Two FLEX Joint Test",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            slate_id="slate_test",
            salary_source="dff",
            contests=self.contests,
            lineups=[lineup_initial],
            roster_cards=locked_cards + [g1, g2, v1, v2],
            projection_overrides={},
            excluded_athlete_keys=[],
        )

        realloc = solve_coordinated_reallocation(plan, live_states)
        self.assertTrue(realloc.is_valid)
        self.assertAlmostEqual(realloc.portfolio_payout_before, 55.00, places=2)
        self.assertAlmostEqual(realloc.portfolio_payout_after, 62.50, places=2)
        self.assertAlmostEqual(realloc.portfolio_payout_delta, 7.50, places=2)

        # Proposed combination must be a mixed pair (one Guaranteed, one Volatile)
        proposed_names = {
            s.new_player_name for s in realloc.swaps
        }
        # Exactly 1 swap should occur since G1 was already in Flex, or both swapped to another legal mixed pair
        # Verify the post-application lineup has exactly one G and one V
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success, msg)

        final_players = {s.card.card.player_name for s in plan.lineups[0].slots[-2:]}
        g_count = sum(1 for name in final_players if "Guaranteed" in name)
        v_count = sum(1 for name in final_players if "Volatile" in name)
        self.assertEqual(g_count, 1)
        self.assertEqual(v_count, 1)

    def test_saved_roster_runtime_with_multiple_lineups_and_open_slots(self):
        """Demonstrate fast bounded runtime on actual saved roster with multiple entered lineups and 13 open slots."""
        from pathlib import Path
        from multi_lineup_planner import (
            PlannerCard,
            PlannerLineup,
            LineupSlotAssignment,
            WeeklyPlan,
            parse_roster_cards,
            recalculate_lineup,
        )

        roster_path = Path(__file__).resolve().parent.parent / "data" / "raw" / "My_roster.csv"
        self.assertTrue(roster_path.exists(), f"Roster file not found: {roster_path}")

        roster_cards = parse_roster_cards(roster_path.read_text())
        self.assertGreater(len(roster_cards), 300)

        planner_cards = []
        for idx, c in enumerate(roster_cards):
            proj = 10.0 + (idx % 15)
            planner_cards.append(
                PlannerCard(
                    card=c,
                    raw_projection=proj,
                    adjusted_projection=round(proj * c.multiplier, 2),
                    weekly_salary=c.roster_salary,
                    salary_source="roster",
                    is_matched=True,
                    is_eligible=(c.status == "Active" and c.multiplier > 0),
                )
            )

        scorcher = self.contests["Scorcher"]
        wildfire = self.contests["Wildfire"]

        qbs = [c for c in planner_cards if c.is_eligible and c.card.position == "QB"]
        rbs = [c for c in planner_cards if c.is_eligible and c.card.position == "RB"]
        wrs = [c for c in planner_cards if c.is_eligible and c.card.position == "WR"]
        tes = [c for c in planner_cards if c.is_eligible and c.card.position == "TE"]

        # Lineup 1: Scorcher (1 locked QB, 4 open slots)
        l1_slots = [
            LineupSlotAssignment("QB", "QB", qbs[0], is_locked=True),
            LineupSlotAssignment("RB", "RB", rbs[0]),
            LineupSlotAssignment("WR", "WR", wrs[0]),
            LineupSlotAssignment("TE", "TE", tes[0]),
            LineupSlotAssignment("Flex", "Flex", rbs[1]),
        ]
        l1 = PlannerLineup("scorcher_1", "Scorcher", l1_slots)
        recalculate_lineup(l1, scorcher)

        # Lineup 2: Scorcher (1 locked QB, 4 open slots)
        l2_slots = [
            LineupSlotAssignment("QB", "QB", qbs[1], is_locked=True),
            LineupSlotAssignment("RB", "RB", rbs[2]),
            LineupSlotAssignment("WR", "WR", wrs[1]),
            LineupSlotAssignment("TE", "TE", tes[1]),
            LineupSlotAssignment("Flex", "Flex", wrs[2]),
        ]
        l2 = PlannerLineup("scorcher_2", "Scorcher", l2_slots)
        recalculate_lineup(l2, scorcher)

        # Lineup 3: Wildfire (1 locked QB, 5 open slots)
        l3_slots = [
            LineupSlotAssignment("QB", "QB", qbs[2], is_locked=True),
            LineupSlotAssignment("RB", "RB", rbs[3]),
            LineupSlotAssignment("WR", "WR", wrs[3]),
            LineupSlotAssignment("TE", "TE", tes[2]),
            LineupSlotAssignment("Flex", "Flex", wrs[4]),
            LineupSlotAssignment("Flex2", "Flex", rbs[4]),
        ]
        l3 = PlannerLineup("wildfire_1", "Wildfire", l3_slots)
        recalculate_lineup(l3, wildfire)

        self.assertTrue(l1.is_valid)
        self.assertTrue(l2.is_valid)
        self.assertTrue(l3.is_valid)

        plan = WeeklyPlan(
            plan_id="real_roster_runtime_plan",
            name="Real Roster Runtime Plan",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            slate_id="slate_bench",
            salary_source="roster",
            contests=self.contests,
            lineups=[l1, l2, l3],
            roster_cards=planner_cards,
            projection_overrides={},
            excluded_athlete_keys=[],
        )

        live_states = {
            pcard.card.athlete_key: PlayerLiveState(
                athlete_key=pcard.card.athlete_key,
                player_name=pcard.card.player_name,
                team=pcard.card.team,
                position=pcard.card.position,
                game_status=GAME_STATUS_FINAL,
                live_points=0.0,
                final_points=pcard.adjusted_projection,
                remaining_projection=0.0,
            )
            for pcard in [qbs[0], qbs[1], qbs[2]]
        }

        # Solve with 10.0s hard wall-clock budget
        realloc = solve_coordinated_reallocation(plan, live_states, total_time_limit=10.0)

        # Runtime assertions
        self.assertLess(realloc.elapsed_seconds, 10.0)
        self.assertLess(realloc.elapsed_seconds, 3.5)  # Expected ~1.0-1.5s
        self.assertTrue(realloc.is_valid)
        self.assertEqual(realloc.optimality_status, "Best Found within Budget (Candidate Pool Filtered)")
        self.assertFalse(realloc.is_proven_optimal)
        self.assertGreaterEqual(realloc.portfolio_payout_delta, 0.0)

        # Verify recommendation applies cleanly to lineups
        success, msg = apply_coordinated_reallocation(plan, realloc, live_states)
        self.assertTrue(success, msg)
        for l in plan.lineups:
            self.assertTrue(l.is_valid, f"{l.lineup_id} invalidated: {l.validation_errors}")


if __name__ == "__main__":
    unittest.main()

