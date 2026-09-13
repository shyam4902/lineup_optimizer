"""In-week football live lineup manager and pre-kickoff reallocation engine.

Manages entered lineups throughout the week:
- Models game status: UPCOMING, IN_PROGRESS, FINAL.
- Separates kickoff locks from score completion:
  - Started players cannot move.
  - In-progress players accumulate live scores while retaining remaining uncertainty.
  - Completed games lock with zero remaining uncertainty.
- Evaluates payout opportunities across contest threshold bands without rigid rules:
  - Supports floor protection when guarding large banked gains.
  - Supports boom-or-bust upside when chasing 1st place or climbing into the money.
- Formulates and applies atomic, multi-way card reallocations across entries.
- Provides working demonstration scenarios:
  1. Thursday Breakout (surging contender prioritized with premium assets).
  2. Monday Player-Choice: Floor Defense vs Cash Line.
  3. Monday 1st-Place Chase (leading lineup benefits from boom-or-bust upside).
  4. In-Progress Games with Dependent Multi-Lineup Exchanges.
"""

from __future__ import annotations

import csv
from collections import defaultdict
import io
import itertools
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

import pulp

from contest_config import ContestConfig, HistoricalThreshold, PayoutBand, get_default_contests
from multi_lineup_planner import (
    LineupSlotAssignment,
    OwnedCard,
    PlannerCard,
    PlannerLineup,
    WeeklyPlan,
    allowed_positions_for_slot,
    get_primetime_eligible_teams,
    recalculate_lineup,
    validate_lineup,
)
from optimizer_core import normalize_name, normalize_position, normalize_team
from payout_evaluator import normal_cdf


GAME_STATUS_UPCOMING = "UPCOMING"
GAME_STATUS_IN_PROGRESS = "IN_PROGRESS"
GAME_STATUS_FINAL = "FINAL"


@dataclass
class PlayerLiveState:
    athlete_key: str
    player_name: str
    team: str
    position: str
    game_status: str = GAME_STATUS_UPCOMING
    kickoff_time: str = ""  # ISO 8601 string, e.g. "2026-09-10T20:15:00-04:00"
    live_points: float = 0.0
    final_points: float = 0.0
    remaining_projection: float = 0.0
    remaining_stdev: float = 0.0
    # True only when the remaining value came from an explicit user estimate.
    # The seeded pregame projection is not an estimate, so an in-progress player
    # with no estimate reads as unknown instead of the pregame number.
    remaining_is_estimate: bool = False
    profile_type: str = "standard"  # "reliable", "volatile", "standard"
    bimodal_outcomes: list[tuple[float, float]] | None = None  # [(points, probability), ...]
    game_clock: str = ""  # e.g. "Q3 04:12" or "Final"

    def is_locked(self, current_time: datetime | None = None) -> bool:
        if self.game_status in (GAME_STATUS_IN_PROGRESS, GAME_STATUS_FINAL):
            return True
        effective_time = current_time if current_time is not None else datetime.now(timezone.utc)
        if self.kickoff_time:
            try:
                ko = datetime.fromisoformat(self.kickoff_time)
                # If timezone naive, make UTC
                if ko.tzinfo is None:
                    ko = ko.replace(tzinfo=timezone.utc)
                cur = effective_time if effective_time.tzinfo is not None else effective_time.replace(tzinfo=timezone.utc)
                if cur >= ko:
                    return True
            except Exception:
                pass
        return False

    def effective_mean(self, multiplier: float) -> float:
        if self.game_status == GAME_STATUS_FINAL:
            return round(self.final_points * multiplier, 2)
        if self.game_status == GAME_STATUS_IN_PROGRESS:
            if not self.remaining_is_estimate:
                # No estimate supplied: banked live points only, never the seeded projection again.
                return round(self.live_points * multiplier, 2)
            return round((self.live_points + self.remaining_projection) * multiplier, 2)
        if self.bimodal_outcomes:
            expected_raw = sum(pts * prob for pts, prob in self.bimodal_outcomes)
            return round(expected_raw * multiplier, 2)
        return round(self.remaining_projection * multiplier, 2)

    def effective_variance(self, multiplier: float) -> float:
        if self.game_status == GAME_STATUS_FINAL:
            return 0.0
        if self.bimodal_outcomes:
            mean_raw = sum(pts * prob for pts, prob in self.bimodal_outcomes)
            var_raw = sum(prob * ((pts - mean_raw) ** 2) for pts, prob in self.bimodal_outcomes)
            return var_raw * (multiplier ** 2)
        return ((self.remaining_stdev or 0.0) * multiplier) ** 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "athlete_key": self.athlete_key,
            "player_name": self.player_name,
            "team": self.team,
            "position": self.position,
            "game_status": self.game_status,
            "kickoff_time": self.kickoff_time,
            "live_points": self.live_points,
            "final_points": self.final_points,
            "remaining_projection": self.remaining_projection,
            "remaining_stdev": self.remaining_stdev,
            "remaining_is_estimate": self.remaining_is_estimate,
            "profile_type": self.profile_type,
            "bimodal_outcomes": self.bimodal_outcomes,
            "game_clock": self.game_clock,
            "is_locked": self.is_locked(),
        }


@dataclass
class LineupLiveEvaluation:
    lineup_id: str
    contest_name: str
    banked_score: float  # Points with zero uncertainty (final + live)
    projected_remaining: float
    projected_total: float
    uncertainty_stdev: float
    target_tier: str | None
    target_cutoff: float | None
    gap_to_target: float
    expected_payout: float
    leverage_status: str
    leverage_explanation: str
    locked_slots_count: int
    open_slots_count: int
    p_cash: float = 0.0
    p_top: float = 0.0
    top_tier_name: str = "Top Tier"
    is_valid: bool = True
    validation_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoordinatedSwap:
    lineup_id: str
    slot_name: str
    slot_kind: str
    current_card_id: str | None
    current_player_name: str
    new_card_id: str | None
    new_player_name: str
    salary_delta: int
    score_delta: float
    expected_payout_delta: float
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CoordinatedReallocationPlan:
    plan_id: str
    created_at: str
    swaps: list[CoordinatedSwap]
    is_valid: bool
    validation_errors: list[str]
    portfolio_payout_before: float
    portfolio_payout_after: float
    portfolio_payout_delta: float
    execution_steps: list[str]
    optimality_status: str = "Proven Optimal"
    is_proven_optimal: bool = True
    elapsed_seconds: float = 0.0
    disclaimer: str = (
        "Notice: Applying reallocations updates the local planner state only. "
        "GameBlazers does not provide a public automated submission API. "
        "Use this validated plan as your pre-kickoff checklist inside the GameBlazers app."
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "created_at": self.created_at,
            "swaps": [s.to_dict() for s in self.swaps],
            "is_valid": self.is_valid,
            "validation_errors": self.validation_errors,
            "portfolio_payout_before": self.portfolio_payout_before,
            "portfolio_payout_after": self.portfolio_payout_after,
            "portfolio_payout_delta": self.portfolio_payout_delta,
            "execution_steps": self.execution_steps,
            "optimality_status": self.optimality_status,
            "is_proven_optimal": self.is_proven_optimal,
            "elapsed_seconds": self.elapsed_seconds,
            "disclaimer": self.disclaimer,
        }


def evaluate_lineup_live_state(
    lineup: PlannerLineup,
    contest: ContestConfig,
    live_states: Mapping[str, PlayerLiveState],
    current_time: datetime | None = None,
) -> LineupLiveEvaluation:
    """Evaluate a single lineup's current banked score, remaining uncertainty, and expected payout."""
    banked_score = 0.0
    rem_mean = 0.0
    rem_variance = 0.0
    locked_count = 0
    open_count = 0

    discrete_branches: list[list[tuple[float, float]]] = []

    for slot in lineup.slots:
        if not slot.card:
            open_count += 1
            continue

        c = slot.card
        akey = c.card.athlete_key
        p_state = live_states.get(akey)

        if not p_state:
            # Fallback to planner card info
            rem_mean += c.adjusted_projection
            rem_variance += (0.35 * c.adjusted_projection) ** 2
            open_count += 1
            continue

        mult = c.card.multiplier
        if p_state.is_locked(current_time):
            locked_count += 1
            if p_state.game_status == GAME_STATUS_FINAL:
                banked_score += p_state.final_points * mult
            else:
                # In progress: live points banked. Only an explicit estimate adds remaining.
                banked_score += p_state.live_points * mult
                if p_state.remaining_is_estimate:
                    rem_mean += p_state.remaining_projection * mult
                    rem_variance += (p_state.remaining_stdev * mult) ** 2
        else:
            open_count += 1
            if p_state.bimodal_outcomes:
                # Scale outcomes by multiplier
                scaled_bimodal = [(pts * mult, prob) for pts, prob in p_state.bimodal_outcomes]
                discrete_branches.append(scaled_bimodal)
            else:
                rem_mean += p_state.remaining_projection * mult
                rem_variance += (p_state.remaining_stdev * mult) ** 2

    # Compute expected payout and threshold probabilities across contest payout bands
    thresholds = sorted(contest.historical_thresholds, key=lambda th: th.estimate, reverse=True)
    exp_payout = 0.0
    p_cash = 0.0
    p_top = 0.0
    top_tier_name = thresholds[0].tier if thresholds else "Top Tier"

    # Total remaining expected points includes continuous projections plus discrete branch expectation
    discrete_expected_pts = 0.0
    if discrete_branches:
        branch_combinations = _combine_discrete_branches(discrete_branches)
        discrete_expected_pts = sum(b_pts * b_pr for b_pts, b_pr in branch_combinations)
    total_remaining_expected = rem_mean + discrete_expected_pts

    if not discrete_branches:
        total_mean = banked_score + rem_mean
        total_std = math.sqrt(rem_variance)
        exp_payout = _compute_band_expected_payout(total_mean, total_std, contest)
        if thresholds:
            top_thresh = thresholds[0].estimate
            cash_thresh = thresholds[-1].estimate
            p_top = max(0.0, min(1.0, 1.0 - normal_cdf(top_thresh, total_mean, total_std)))
            p_cash = max(0.0, min(1.0, 1.0 - normal_cdf(cash_thresh, total_mean, total_std)))
    else:
        # Branch over discrete outcomes
        total_std = math.sqrt(rem_variance)
        weighted_payout = 0.0
        weighted_p_top = 0.0
        weighted_p_cash = 0.0
        for branch_pts, branch_prob in branch_combinations:
            branch_mean = banked_score + rem_mean + branch_pts
            branch_ev = _compute_band_expected_payout(branch_mean, total_std, contest)
            weighted_payout += branch_prob * branch_ev
            if thresholds:
                top_thresh = thresholds[0].estimate
                cash_thresh = thresholds[-1].estimate
                br_p_top = max(0.0, min(1.0, 1.0 - normal_cdf(top_thresh, branch_mean, total_std)))
                br_p_cash = max(0.0, min(1.0, 1.0 - normal_cdf(cash_thresh, branch_mean, total_std)))
                weighted_p_top += branch_prob * br_p_top
                weighted_p_cash += branch_prob * br_p_cash
        exp_payout = weighted_payout
        p_top = weighted_p_top
        p_cash = weighted_p_cash
        total_mean = banked_score + total_remaining_expected

    # Determine target tier and leverage status
    target_tier = None
    target_cutoff = None
    gap = 0.0

    for th in reversed(thresholds):
        if total_mean < th.estimate:
            target_tier = th.tier
            target_cutoff = th.estimate
            gap = round(th.estimate - total_mean, 1)
            break

    if target_tier is None and thresholds:
        target_tier = thresholds[0].tier
        target_cutoff = thresholds[0].estimate
        gap = 0.0

    # Categorize leverage status
    leverage_status = "On Pace"
    explanation = "Tracking near expected baseline."

    if not thresholds:
        leverage_status = "Standard"
        explanation = "No historical threshold data available."
    else:
        top_cutoff = thresholds[0].estimate
        cash_cutoff = thresholds[-1].estimate

        slots_total = len(lineup.slots) or 1
        expected_banked_pace = (cash_cutoff / slots_total) * locked_count
        is_breakout = locked_count > 0 and (banked_score - expected_banked_pace >= 8.0 or banked_score >= cash_cutoff * 0.28)

        if total_mean >= (top_cutoff - 20.0) or (open_count > 0 and (total_mean + 1.8 * math.sqrt(rem_variance or 1.0)) >= top_cutoff and total_mean >= cash_cutoff and (locked_count >= slots_total - 2)):
            leverage_status = "Top Contender (Chasing 1st)"
            explanation = f"Projected {total_mean:.1f} pts is within reach of top tier ({top_cutoff:.1f}). Ceiling upside needed to capture top placement."
        elif is_breakout and open_count > 0:
            leverage_status = "Surging Contender"
            explanation = f"Early games generated {banked_score:.1f} banked pts (outperforming pace by {banked_score - expected_banked_pace:+.1f}). Prioritize remaining slots to capture top prize."
        elif total_mean < cash_cutoff:
            if (total_mean + 1.8 * math.sqrt(rem_variance or 1.0)) >= cash_cutoff:
                leverage_status = "Needs Boom"
                explanation = f"Baseline {total_mean:.1f} pts falls short of cash line ({cash_cutoff:.1f}). Requires ceiling variance to cash."
            else:
                leverage_status = "Eliminated"
                explanation = f"Even maximum ceiling cannot reach cash cutoff ({cash_cutoff:.1f})."
        else:
            leverage_status = "On Pace"
            explanation = f"Projected {total_mean:.1f} pts is on pace for cash line ({cash_cutoff:.1f})."

    return LineupLiveEvaluation(
        lineup_id=lineup.lineup_id,
        contest_name=contest.name,
        banked_score=round(banked_score, 2),
        projected_remaining=round(total_remaining_expected, 2),
        projected_total=round(total_mean, 2),
        uncertainty_stdev=round(math.sqrt(rem_variance), 2),
        target_tier=target_tier,
        target_cutoff=target_cutoff,
        gap_to_target=gap,
        expected_payout=round(exp_payout, 2),
        leverage_status=leverage_status,
        leverage_explanation=explanation,
        locked_slots_count=locked_count,
        open_slots_count=open_count,
        p_cash=round(p_cash, 3),
        p_top=round(p_top, 3),
        top_tier_name=top_tier_name,
        is_valid=lineup.is_valid,
        validation_errors=lineup.validation_errors,
    )


def _compute_band_expected_payout(mean: float, std: float, contest: ContestConfig) -> float:
    thresholds = sorted(contest.historical_thresholds, key=lambda th: th.estimate, reverse=True)
    if not thresholds:
        return 0.0

    cum_probs: list[tuple[HistoricalThreshold, float]] = []
    for th in thresholds:
        prob_ge = 1.0 - normal_cdf(th.estimate, mean, std)
        cum_probs.append((th, max(0.0, min(1.0, prob_ge))))

    total_ev = 0.0
    for idx, (th, prob_ge) in enumerate(cum_probs):
        if idx == 0:
            band_prob = prob_ge
        else:
            prev_prob_ge = cum_probs[idx - 1][1]
            band_prob = max(0.0, prob_ge - prev_prob_ge)

        payout = 0.0
        for b in contest.payout_bands:
            if b.start_rank <= th.rank_cutoff <= b.end_rank:
                payout = b.payout_per_entry
                break
        if payout == 0.0 and contest.payout_bands:
            eligible = [b for b in contest.payout_bands if b.end_rank >= th.rank_cutoff]
            if eligible:
                payout = min(eligible, key=lambda b: b.end_rank).payout_per_entry

        total_ev += band_prob * payout

    return total_ev


def _combine_discrete_branches(branches: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    current = [(0.0, 1.0)]
    for branch in branches:
        nxt = []
        for base_pts, base_prob in current:
            for pts, prob in branch:
                nxt.append((base_pts + pts, base_prob * prob))
        current = nxt
    return current


def identify_promising_lineups(
    plan: WeeklyPlan,
    live_states: Mapping[str, PlayerLiveState],
    current_time: datetime | None = None,
) -> list[LineupLiveEvaluation]:
    """Evaluate and rank all entered lineups in descending order of payout opportunity."""
    evaluations: list[LineupLiveEvaluation] = []
    for lineup in plan.lineups:
        contest = plan.contests.get(lineup.contest_name)
        if not contest:
            continue
        ev = evaluate_lineup_live_state(lineup, contest, live_states, current_time)
        evaluations.append(ev)

    # Rank by priority: Surging Contenders and Top Contenders first, then by expected payout
    priority_map = {
        "Top Contender (Chasing 1st)": 4,
        "Surging Contender": 3,
        "On Pace": 2,
        "Needs Boom": 1,
        "Eliminated": 0,
        "Standard": 1,
    }
    evaluations.sort(
        key=lambda e: (priority_map.get(e.leverage_status, 1), e.expected_payout, e.projected_total),
        reverse=True,
    )
    return evaluations


def solve_coordinated_reallocation(
    plan: WeeklyPlan,
    live_states: Mapping[str, PlayerLiveState],
    current_time: datetime | None = None,
    total_time_limit: float = 12.0,
) -> CoordinatedReallocationPlan:
    """Solve multi-lineup card reallocation across unlocked slots to maximize total portfolio expected payout.

    Enforces:
    1. Zero movement of kickoff-locked or final players.
    2. Card ownership: each physical card copy is used at most once across the entire portfolio.
    3. Athlete uniqueness: each athlete appears at most once per lineup.
    4. Contest minimum and maximum salary caps.
    5. Slot position eligibility.
    6. Expected payout maximization under player outcome distributions.
    7. Hard execution time bounding across pattern generation and solver with valid fallback.
    """
    start_time = time.perf_counter()
    deadline = start_time + total_time_limit
    now_iso = (current_time or datetime.now(timezone.utc)).isoformat()
    plan_id = f"realloc_{now_iso[:19].replace(':', '').replace('-', '')}"

    # Identify locked slots and available cards
    locked_slot_cards: dict[tuple[str, str], PlannerCard] = {}
    open_slots: list[tuple[str, str, str, ContestConfig]] = []  # (lineup_id, slot_name, slot_kind, contest)
    used_locked_cids: set[str] = set()

    for l in plan.lineups:
        contest = plan.contests[l.contest_name]
        for s in l.slots:
            if s.card:
                akey = s.card.card.athlete_key
                p_state = live_states.get(akey)
                is_slot_locked = s.is_locked or l.is_locked or (p_state is not None and p_state.is_locked(current_time))
                if is_slot_locked:
                    locked_slot_cards[(l.lineup_id, s.slot_name)] = s.card
                    used_locked_cids.add(s.card.card.card_id)
                else:
                    open_slots.append((l.lineup_id, s.slot_name, s.slot_kind, contest))
            else:
                open_slots.append((l.lineup_id, s.slot_name, s.slot_kind, contest))

    # Calculate baseline payout before reallocation
    initial_evals = identify_promising_lineups(plan, live_states, current_time)
    baseline_portfolio_payout = round(sum(e.expected_payout for e in initial_evals if e.is_valid), 2)

    if not open_slots:
        elapsed = round(time.perf_counter() - start_time, 3)
        return CoordinatedReallocationPlan(
            plan_id=plan_id,
            created_at=now_iso,
            swaps=[],
            is_valid=True,
            validation_errors=[],
            portfolio_payout_before=baseline_portfolio_payout,
            portfolio_payout_after=baseline_portfolio_payout,
            portfolio_payout_delta=0.0,
            execution_steps=["All slots are currently locked. No swaps permitted."],
            optimality_status="Proven Optimal",
            is_proven_optimal=True,
            elapsed_seconds=elapsed,
        )

    # Candidate cards: all eligible roster cards whose game has not kicked off and not already locked
    available_candidate_cards: list[PlannerCard] = []
    for c in plan.roster_cards:
        if not c.is_eligible:
            continue
        if c.card.card_id in used_locked_cids:
            continue
        akey = c.card.athlete_key
        p_state = live_states.get(akey)
        if p_state and p_state.is_locked(current_time):
            continue
        available_candidate_cards.append(c)

    card_dict = {c.card.card_id: c for c in available_candidate_cards}
    for (lid, sname), pcard in locked_slot_cards.items():
        card_dict[pcard.card.card_id] = pcard

    # Group available candidate cards into equivalence classes
    # Equivalence key: (athlete_key, position, multiplier, weekly_salary)
    equiv_to_cards: dict[tuple[str, str, float, int], list[PlannerCard]] = defaultdict(list)
    for c in available_candidate_cards:
        eq_key = (c.card.athlete_key, c.card.position, c.card.multiplier, c.weekly_salary)
        equiv_to_cards[eq_key].append(c)

    equiv_reps: dict[tuple[str, str, float, int], PlannerCard] = {
        eq_k: clist[0] for eq_k, clist in equiv_to_cards.items()
    }
    equiv_caps: dict[tuple[str, str, float, int], int] = {
        eq_k: len(clist) for eq_k, clist in equiv_to_cards.items()
    }

    # Pre-calculate each lineup's current state and target thresholds
    lineup_dict = {l.lineup_id: l for l in plan.lineups}

    # Group open slots by lineup
    open_slots_by_lineup: dict[str, list[tuple[str, str, ContestConfig]]] = {}
    for lid, sname, skind, cfg in open_slots:
        open_slots_by_lineup.setdefault(lid, []).append((sname, skind, cfg))

    # Pattern Generation:
    # Precompute legal candidate patterns for each lineup's open slots and evaluate
    # their exact joint expected payout under evaluate_lineup_live_state().
    #
    # Candidate pool filtering:
    # We filter equivalence classes rather than arbitrary individual cards so no
    # distinct athlete/multiplier profile is lost, while physical duplicate copies
    # remain available for assignment across multiple lineups up to their true inventory cap.
    lineup_patterns: dict[str, list[tuple[int, dict[str, PlannerCard], dict[str, tuple[str, str, float, int]], float, float]]] = {}
    pattern_generation_truncated = False

    for l in plan.lineups:
        lid = l.lineup_id
        cfg = plan.contests[l.contest_name]
        l_open_slots = open_slots_by_lineup.get(lid, [])

        if not l_open_slots:
            ev = evaluate_lineup_live_state(l, cfg, live_states, current_time)
            lineup_patterns[lid] = [(0, {}, {}, ev.expected_payout, ev.projected_total)]
            continue

        locked_sal = sum(
            pcard.weekly_salary for (l_id, s_name), pcard in locked_slot_cards.items() if l_id == lid
        )
        locked_athletes = {
            pcard.card.athlete_key for (l_id, s_name), pcard in locked_slot_cards.items() if l_id == lid
        }

        # Determine slot candidate pool limit based on number of open slots
        num_open = len(l_open_slots)
        if num_open <= 2:
            max_cands_per_slot = 40
            max_patterns_for_lineup = 10000
        elif num_open == 3:
            max_cands_per_slot = 18
            max_patterns_for_lineup = 6000
        elif num_open == 4:
            max_cands_per_slot = 10
            max_patterns_for_lineup = 4000
        else:
            max_cands_per_slot = 8
            max_patterns_for_lineup = 3000

        # Current cards in this lineup must always be included in the candidate pool
        cur_card_ids = {s.card.card.card_id for s in l.slots if s.card}

        pt_teams = get_primetime_eligible_teams()
        is_pyro = l.contest_name == "Primetime Pyro"
        slot_candidates: list[list[tuple[str, tuple[str, str, float, int], PlannerCard]]] = []
        for sname, skind, _ in l_open_slots:
            allowed = allowed_positions_for_slot(skind)
            matching_equivs = [
                (eq_k, rep) for eq_k, rep in equiv_reps.items()
                if rep.card.position in allowed
                and rep.card.athlete_key not in locked_athletes
                and (is_pyro or getattr(rep.card, "collection_group", "Core") != "Primetime")
            ]


            # Prioritize: 1) volatile/bimodal players, 2) currently assigned cards, 3) adjusted projection desc
            def candidate_priority_key(item: tuple[tuple[str, str, float, int], PlannerCard]) -> tuple[int, int, float, float]:
                eq_k, c = item
                p_state = live_states.get(c.card.athlete_key)
                is_bimodal = 1 if (p_state and p_state.bimodal_outcomes) else 0
                is_current = 1 if any(card.card.card_id in cur_card_ids for card in equiv_to_cards[eq_k]) else 0
                return (is_bimodal, is_current, c.adjusted_projection, c.card.multiplier)

            matching_equivs.sort(key=candidate_priority_key, reverse=True)

            # Check if truncation occurred for this slot
            if len(matching_equivs) > max_cands_per_slot:
                pattern_generation_truncated = True

            chosen_equivs = matching_equivs[:max_cands_per_slot]
            slot_candidates.append([(sname, eq_k, rep) for eq_k, rep in chosen_equivs])

        patterns: list[tuple[int, dict[str, PlannerCard], dict[str, tuple[str, str, float, int]], float, float]] = []
        p_idx = 0

        # Construct a temporary lineup for evaluating joint distributions across the open slots
        temp_slots = [
            LineupSlotAssignment(
                s.slot_name,
                s.slot_kind,
                locked_slot_cards.get((lid, s.slot_name)),
                is_locked=s.is_locked,
            )
            for s in l.slots
        ]
        temp_lineup = PlannerLineup(l.lineup_id, l.contest_name, temp_slots)

        # Include current assignment as baseline pattern 0 if feasible
        cur_open_cards: dict[str, PlannerCard] = {}
        cur_feasible = True
        for s in l.slots:
            if any(s.slot_name == osname for osname, _, _ in l_open_slots):
                if s.card:
                    cur_open_cards[s.slot_name] = s.card
                else:
                    cur_feasible = False
        if cur_feasible and len(cur_open_cards) == len(l_open_slots):
            for ts in temp_slots:
                if ts.slot_name in cur_open_cards:
                    ts.card = cur_open_cards[ts.slot_name]
            base_ev = evaluate_lineup_live_state(temp_lineup, cfg, live_states, current_time)
            cur_eq_dict = {
                sname: (c.card.athlete_key, c.card.position, c.card.multiplier, c.weekly_salary)
                for sname, c in cur_open_cards.items()
            }
            patterns.append((p_idx, dict(cur_open_cards), cur_eq_dict, base_ev.expected_payout, base_ev.projected_total))
            p_idx += 1

        pattern_eqs_seen: set[frozenset[tuple[str, str, float, int]]] = set()
        if cur_open_cards:
            pattern_eqs_seen.add(frozenset(
                (c.card.athlete_key, c.card.position, c.card.multiplier, c.weekly_salary)
                for c in cur_open_cards.values()
            ))

        # Enforce pattern generation time limit: at most 50% of remaining budget
        pattern_deadline = start_time + (total_time_limit * 0.5)

        for combo in itertools.product(*slot_candidates):
            if time.perf_counter() > pattern_deadline:
                pattern_generation_truncated = True
                break

            eq_keys = [eq_k for sname, eq_k, rep in combo]
            if len(set(eq_keys)) < len(eq_keys):
                continue
            akeys = [rep.card.athlete_key for sname, eq_k, rep in combo]
            if len(set(akeys)) < len(akeys):
                continue

            sal = locked_sal + sum(rep.weekly_salary for sname, eq_k, rep in combo)
            if sal < cfg.minimum_salary or sal > cfg.maximum_salary:
                continue

            combo_eq_key = frozenset(eq_keys)
            if combo_eq_key in pattern_eqs_seen:
                continue
            pattern_eqs_seen.add(combo_eq_key)

            combo_rep_dict = {sname: rep for sname, eq_k, rep in combo}
            combo_eq_dict = {sname: eq_k for sname, eq_k, rep in combo}
            for ts in temp_slots:
                if ts.slot_name in combo_rep_dict:
                    ts.card = combo_rep_dict[ts.slot_name]

            ev = evaluate_lineup_live_state(temp_lineup, cfg, live_states, current_time)
            patterns.append((p_idx, combo_rep_dict, combo_eq_dict, ev.expected_payout, ev.projected_total))
            p_idx += 1

            if len(patterns) >= max_patterns_for_lineup:
                pattern_generation_truncated = True
                break

        lineup_patterns[lid] = patterns

    # Formulate Portfolio MIP over exact lineup patterns
    model = pulp.LpProblem("GameBlazers_Pattern_Reallocation", pulp.LpMaximize)

    # z[lid, pid]: binary activation variable for choosing pattern pid in lineup lid
    z: dict[tuple[str, int], pulp.LpVariable] = {}
    for lid, pats in lineup_patterns.items():
        for pid, cdict, eq_dict, exp_pay, proj in pats:
            z[(lid, pid)] = pulp.LpVariable(f"z_{lid}_{pid}", cat=pulp.LpBinary)

    # Constraint 1: Exactly 1 pattern chosen per active lineup
    for lid, pats in lineup_patterns.items():
        if pats:
            model += pulp.lpSum(z[(lid, pid)] for pid, _, _, _, _ in pats) == 1, f"pattern_one_{lid}"

    # Constraint 2: Global card copy capacity across all chosen lineup patterns
    # Each equivalence group cannot be used in more lineups than the physical count of owned copies
    for eq_key, cap in equiv_caps.items():
        using_vars: list[pulp.LpVariable] = []
        for lid, pats in lineup_patterns.items():
            for pid, cdict, eq_dict, _, _ in pats:
                if any(k == eq_key for k in eq_dict.values()):
                    using_vars.append(z[(lid, pid)])
        if using_vars:
            model += pulp.lpSum(using_vars) <= cap, f"copy_capacity_{eq_key[0]}_{eq_key[2]}_{eq_key[3]}"

    # Objective: Maximize total portfolio expected payout with secondary tiebreaker on projected points
    model += pulp.lpSum(
        (exp_pay + 0.0001 * proj) * z[(lid, pid)]
        for lid, pats in lineup_patterns.items()
        for pid, cdict, eq_dict, exp_pay, proj in pats
    )

    # Bound solver time with remaining budget (at least 1.0s)
    remaining_time = max(1.0, deadline - time.perf_counter())
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=remaining_time)
    model.solve(solver)
    status_str = pulp.LpStatus[model.status]

    # Handle infeasibility or solver timeout: retain valid baseline allocation
    if status_str not in ("Optimal", "Feasible"):
        elapsed = round(time.perf_counter() - start_time, 3)
        return CoordinatedReallocationPlan(
            plan_id=plan_id,
            created_at=now_iso,
            swaps=[],
            is_valid=True,
            validation_errors=[],
            portfolio_payout_before=baseline_portfolio_payout,
            portfolio_payout_after=baseline_portfolio_payout,
            portfolio_payout_delta=0.0,
            execution_steps=["Solver timed out without finding improvements. Retaining baseline lineups."],
            optimality_status="Timed Out (Baseline Preserved)",
            is_proven_optimal=False,
            elapsed_seconds=elapsed,
        )

    # Determine optimality label
    if status_str == "Optimal" and not pattern_generation_truncated:
        optimality_status = "Proven Optimal"
        is_proven_optimal = True
    elif status_str == "Optimal" and pattern_generation_truncated:
        optimality_status = "Best Found within Budget (Candidate Pool Filtered)"
        is_proven_optimal = False
    else:
        optimality_status = f"Feasible Suboptimal ({status_str})"
        is_proven_optimal = False

    # Extract chosen equivalence groups for each lineup slot
    chosen_eq_assignments: dict[tuple[str, str], tuple[str, str, float, int]] = {}
    for lid, pats in lineup_patterns.items():
        for pid, cdict, eq_dict, exp_pay, proj in pats:
            var = z.get((lid, pid))
            if var is not None and var.value() and var.value() > 0.5:
                for sname, eq_k in eq_dict.items():
                    chosen_eq_assignments[(lid, sname)] = eq_k

    # Resolve concrete physical card assignments from available inventory
    # Preserves currently assigned cards whenever they match the chosen equivalence group,
    # and distributes separate physical card copies across lineups without cross-lineup collision.
    proposed_assignments: dict[tuple[str, str], PlannerCard] = {}
    assigned_card_ids: set[str] = set()

    cur_lineup_cards: dict[tuple[str, str], PlannerCard] = {}
    for lid, sname, _, _ in open_slots:
        l = lineup_dict.get(lid)
        if l:
            slot = next((s for s in l.slots if s.slot_name == sname), None)
            if slot and slot.card:
                cur_lineup_cards[(lid, sname)] = slot.card

    slots_by_eq: dict[tuple[str, str, float, int], list[tuple[str, str]]] = defaultdict(list)
    for (lid, sname), eq_k in chosen_eq_assignments.items():
        slots_by_eq[eq_k].append((lid, sname))

    for eq_k, slot_list in slots_by_eq.items():
        pool = [c for c in equiv_to_cards[eq_k] if c.card.card_id not in assigned_card_ids]
        # Pass 1: Keep current card copy if it belongs to this equivalence group
        for (lid, sname) in slot_list:
            cur_c = cur_lineup_cards.get((lid, sname))
            if cur_c and cur_c.card.card_id in [p.card.card_id for p in pool]:
                proposed_assignments[(lid, sname)] = cur_c
                assigned_card_ids.add(cur_c.card.card_id)
                pool = [p for p in pool if p.card.card_id != cur_c.card.card_id]
        # Pass 2: Assign distinct copies from the remaining pool
        for (lid, sname) in slot_list:
            if (lid, sname) not in proposed_assignments:
                if pool:
                    assigned = pool.pop(0)
                    proposed_assignments[(lid, sname)] = assigned
                    assigned_card_ids.add(assigned.card.card_id)

    # Build swap actions and rationales
    swaps: list[CoordinatedSwap] = []
    for lid, sname, skind, cfg in open_slots:
        l = lineup_dict[lid]
        cur_slot = next((s for s in l.slots if s.slot_name == sname), None)
        cur_card = cur_slot.card if cur_slot else None
        new_card = proposed_assignments.get((lid, sname))

        if new_card and (cur_card is None or cur_card.card.card_id != new_card.card.card_id):
            sal_delta = new_card.weekly_salary - (cur_card.weekly_salary if cur_card else 0)
            cur_pts = (
                live_states[cur_card.card.athlete_key].effective_mean(cur_card.card.multiplier)
                if cur_card and cur_card.card.athlete_key in live_states
                else (cur_card.adjusted_projection if cur_card else 0.0)
            )
            new_pts = (
                live_states[new_card.card.athlete_key].effective_mean(new_card.card.multiplier)
                if new_card.card.athlete_key in live_states
                else new_card.adjusted_projection
            )
            score_delta = round(new_pts - cur_pts, 2)

            p_state = live_states.get(new_card.card.athlete_key)
            profile_label = p_state.profile_type if p_state else "standard"

            ev_lineup = evaluate_lineup_live_state(l, cfg, live_states, current_time)
            rationale = _generate_swap_rationale(
                lineup_id=lid,
                contest_name=l.contest_name,
                cur_card=cur_card,
                new_card=new_card,
                score_delta=score_delta,
                profile_type=profile_label,
                leverage_status=ev_lineup.leverage_status,
                target_tier=ev_lineup.target_tier,
            )

            swaps.append(
                CoordinatedSwap(
                    lineup_id=lid,
                    slot_name=sname,
                    slot_kind=skind,
                    current_card_id=cur_card.card.card_id if cur_card else None,
                    current_player_name=cur_card.card.player_name if cur_card else "<Empty>",
                    new_card_id=new_card.card.card_id,
                    new_player_name=new_card.card.player_name,
                    salary_delta=sal_delta,
                    score_delta=score_delta,
                    expected_payout_delta=0.0,
                    rationale=rationale,
                )
            )

    # Validate the full proposed portfolio
    temp_plan = _simulate_plan_with_swaps(plan, proposed_assignments)
    post_evals = identify_promising_lineups(temp_plan, live_states, current_time)
    portfolio_payout_after = round(sum(e.expected_payout for e in post_evals if e.is_valid), 2)
    payout_delta = round(portfolio_payout_after - baseline_portfolio_payout, 2)

    # Assign lineup-level payout delta to swaps
    for s in swaps:
        before_ev = next((e.expected_payout for e in initial_evals if e.lineup_id == s.lineup_id), 0.0)
        after_ev = next((e.expected_payout for e in post_evals if e.lineup_id == s.lineup_id), 0.0)
        s.expected_payout_delta = round(after_ev - before_ev, 2)

    validation_errors: list[str] = []
    is_valid = True
    for l in temp_plan.lineups:
        if not l.is_valid:
            is_valid = False
            validation_errors.extend(l.validation_errors)

    execution_steps = _generate_execution_steps(swaps)
    elapsed = round(time.perf_counter() - start_time, 3)

    return CoordinatedReallocationPlan(
        plan_id=plan_id,
        created_at=now_iso,
        swaps=swaps,
        is_valid=is_valid,
        validation_errors=validation_errors,
        portfolio_payout_before=baseline_portfolio_payout,
        portfolio_payout_after=portfolio_payout_after,
        portfolio_payout_delta=payout_delta,
        execution_steps=execution_steps,
        optimality_status=optimality_status,
        is_proven_optimal=is_proven_optimal,
        elapsed_seconds=elapsed,
    )


def apply_coordinated_reallocation(
    plan: WeeklyPlan,
    realloc_plan: CoordinatedReallocationPlan,
    live_states: Mapping[str, PlayerLiveState],
    current_time: datetime | None = None,
) -> tuple[bool, str]:
    """Atomically commit a validated reallocation plan to the local WeeklyPlan.

    Strict validation:
    1. Rejects if the plan is flagged as invalid or has validation errors.
    2. Validates incoming cards exist in the current roster and are not locked.
    3. Validates outgoing cards match the expected current slot occupant and are not locked.
    4. Simulates and validates the entire proposed portfolio (salary, slot eligibility, athlete uniqueness, single copy use) BEFORE modifying any live state.
    5. Rejects stale or invalid recommendations without modifying lineups.
    """
    if not realloc_plan.is_valid:
        err_msg = "; ".join(realloc_plan.validation_errors) if realloc_plan.validation_errors else "Plan is marked invalid"
        return False, f"Cannot apply invalid plan: {err_msg}"

    if not realloc_plan.swaps:
        return True, "No swaps needed. Current allocation is already optimal."

    card_map = {c.card.card_id: c for c in plan.roster_cards}
    lineup_map = {l.lineup_id: l for l in plan.lineups}

    # Step 1: Pre-validate each swap for staleness, existence, and locks
    assignments: dict[tuple[str, str], PlannerCard] = {}
    for s in realloc_plan.swaps:
        l = lineup_map.get(s.lineup_id)
        if not l:
            return False, f"Validation failed: Lineup '{s.lineup_id}' not found in active plan."

        slot = next((x for x in l.slots if x.slot_name == s.slot_name), None)
        if not slot:
            return False, f"Validation failed: Slot '{s.slot_name}' not found in lineup '{s.lineup_id}'."

        # Verify outgoing card matches current state (guards against stale plans)
        cur_cid = slot.card.card.card_id if slot.card else None
        if cur_cid != s.current_card_id:
            return False, (
                f"Validation failed: Stale recommendation for {s.lineup_id} {s.slot_name}. "
                f"Expected current card '{s.current_card_id}', but found '{cur_cid}'."
            )

        # Verify outgoing slot is not locked
        if slot.is_locked or l.is_locked:
            return False, f"Validation failed: Cannot swap manually locked slot {s.slot_name} in lineup {s.lineup_id}."

        if slot.card:
            p_state = live_states.get(slot.card.card.athlete_key)
            if p_state and p_state.is_locked(current_time):
                return False, (
                    f"Validation failed: Cannot swap outgoing player '{slot.card.card.player_name}' in {s.lineup_id} "
                    f"because the game is already locked ({p_state.game_status})."
                )

        # Verify incoming card exists, is eligible, and not locked
        if s.new_card_id is not None:
            new_card = card_map.get(s.new_card_id)
            if not new_card:
                return False, f"Validation failed: Incoming card '{s.new_card_id}' not found in roster inventory."
            if not new_card.is_eligible:
                return False, f"Validation failed: Incoming card '{new_card.card.player_name}' is ineligible: {new_card.ineligibility_reason}."

            new_p_state = live_states.get(new_card.card.athlete_key)
            if new_p_state and new_p_state.is_locked(current_time):
                return False, (
                    f"Validation failed: Incoming player '{new_card.card.player_name}' is already locked "
                    f"({new_p_state.game_status}) and cannot be assigned."
                )

            allowed = allowed_positions_for_slot(slot.slot_kind)
            if new_card.card.position not in allowed:
                return False, (
                    f"Validation failed: Incoming card '{new_card.card.player_name}' ({new_card.card.position}) "
                    f"is not eligible for slot '{s.slot_name}' ({slot.slot_kind})."
                )
            assignments[(s.lineup_id, s.slot_name)] = new_card

    # Step 2: Validate the full proposed portfolio in simulation BEFORE mutating state
    sim_plan = _simulate_plan_with_swaps(plan, assignments)
    for sim_l in sim_plan.lineups:
        if not sim_l.is_valid:
            return False, f"Validation failed on proposed allocation: {'; '.join(sim_l.validation_errors)}"

    # Step 3: All checks passed; apply all swaps simultaneously
    for s in realloc_plan.swaps:
        l = lineup_map[s.lineup_id]
        slot = next(x for x in l.slots if x.slot_name == s.slot_name)
        slot.card = card_map.get(s.new_card_id) if s.new_card_id else None

    # Step 4: Recalculate and validate every lineup in the active plan
    card_usage: dict[str, list[str]] = {}
    for l in plan.lineups:
        for slot in l.slots:
            if slot.card:
                card_usage.setdefault(slot.card.card.card_id, []).append(l.lineup_id)

    for l in plan.lineups:
        cfg = plan.contests[l.contest_name]
        recalculate_lineup(l, cfg, card_usage)

    plan.total_weekly_projection = round(sum(l.total_projection for l in plan.lineups), 2)
    plan.total_weekly_estimated_payout = round(sum(l.estimated_payout for l in plan.lineups if l.is_valid), 2)
    plan.updated_at = (current_time or datetime.now(timezone.utc)).isoformat()

    return True, f"Successfully applied {len(realloc_plan.swaps)} coordinated swaps across portfolio."


def _simulate_plan_with_swaps(
    original_plan: WeeklyPlan,
    assignments: Mapping[tuple[str, str], PlannerCard],
) -> WeeklyPlan:
    sim_lineups: list[PlannerLineup] = []
    for l in original_plan.lineups:
        sim_slots: list[LineupSlotAssignment] = []
        for s in l.slots:
            assigned = assignments.get((l.lineup_id, s.slot_name), s.card)
            sim_slots.append(
                LineupSlotAssignment(
                    slot_name=s.slot_name,
                    slot_kind=s.slot_kind,
                    card=assigned,
                    is_locked=s.is_locked,
                )
            )
        sim_lineups.append(
            PlannerLineup(
                lineup_id=l.lineup_id,
                contest_name=l.contest_name,
                slots=sim_slots,
                is_locked=l.is_locked,
            )
        )

    card_usage: dict[str, list[str]] = {}
    for l in sim_lineups:
        for s in l.slots:
            if s.card:
                card_usage.setdefault(s.card.card.card_id, []).append(l.lineup_id)

    for l in sim_lineups:
        cfg = original_plan.contests[l.contest_name]
        recalculate_lineup(l, cfg, card_usage)

    return WeeklyPlan(
        plan_id="sim_plan",
        name="Simulated Plan",
        created_at=original_plan.created_at,
        updated_at=datetime.now(timezone.utc).isoformat(),
        slate_id=original_plan.slate_id,
        salary_source=original_plan.salary_source,
        contests=original_plan.contests,
        lineups=sim_lineups,
        roster_cards=original_plan.roster_cards,
        projection_overrides=original_plan.projection_overrides,
        excluded_athlete_keys=original_plan.excluded_athlete_keys,
    )


def _generate_swap_rationale(
    lineup_id: str,
    contest_name: str,
    cur_card: PlannerCard | None,
    new_card: PlannerCard,
    score_delta: float,
    profile_type: str,
    leverage_status: str,
    target_tier: str | None,
) -> str:
    cur_name = cur_card.card.player_name if cur_card else "empty slot"
    new_name = new_card.card.player_name
    diff_sign = "+" if score_delta > 0 else ""

    if leverage_status == "Surging Contender":
        return (
            f"Assigning {new_name} ({diff_sign}{score_delta:.1f} pts, {profile_type}) to surging contender {lineup_id} "
            f"to protect early Thursday banked gains and reach {target_tier or 'top tier'}."
        )
    if leverage_status == "Top Contender (Chasing 1st)":
        return (
            f"Assigning {new_name} ({profile_type} upside, {diff_sign}{score_delta:.1f} pts) to {lineup_id} "
            f"because climbing to 1st place requires high ceiling outcomes over a safe floor."
        )
    if leverage_status == "Needs Boom":
        return (
            f"Assigning {new_name} ({profile_type} ceiling) to trailing {lineup_id} "
            f"because standard baseline falls short of cash line and only ceiling variance reaches payout."
        )
    return (
        f"Upgrades {lineup_id} from {cur_name} to {new_name} ({diff_sign}{score_delta:.1f} pts) "
        f"while maintaining salary feasibility across the portfolio."
    )


def _generate_execution_steps(swaps: list[CoordinatedSwap]) -> list[str]:
    """Generate a legally executable sequence for dependent swaps, or explicitly flag exchanges that cannot be sequenced directly."""
    if not swaps:
        return []

    # Map who currently holds which card
    holders: dict[str, CoordinatedSwap] = {}
    for s in swaps:
        if s.current_card_id:
            holders[s.current_card_id] = s

    # Build dependency graph between swaps:
    # Swap s depends on Swap h if s.new_card_id == h.current_card_id
    deps: dict[int, set[int]] = {i: set() for i in range(len(swaps))}
    rev_deps: dict[int, set[int]] = {i: set() for i in range(len(swaps))}

    for i, s in enumerate(swaps):
        if s.new_card_id in holders:
            h = holders[s.new_card_id]
            h_idx = swaps.index(h)
            if h_idx != i:
                # s needs card released by h, so h must execute BEFORE s
                deps[i].add(h_idx)
                rev_deps[h_idx].add(i)

    # Topological sort (Kahn's algorithm)
    ready = [i for i in range(len(swaps)) if len(deps[i]) == 0]
    ordered_indices: list[int] = []

    deps_copy = {i: set(deps[i]) for i in deps}
    while ready:
        curr = ready.pop(0)
        ordered_indices.append(curr)
        for nxt in sorted(rev_deps[curr]):
            deps_copy[nxt].discard(curr)
            if len(deps_copy[nxt]) == 0 and nxt not in ordered_indices and nxt not in ready:
                ready.append(nxt)

    steps: list[str] = []
    step_num = 1

    if len(ordered_indices) == len(swaps):
        # All swaps can be sequenced legally without circular deadlock
        for idx in ordered_indices:
            s = swaps[idx]
            from_desc = f"from bench/pool" if s.new_card_id not in holders else f"released from {holders[s.new_card_id].lineup_id}"
            steps.append(
                f"Step {step_num}: In {s.lineup_id}, open slot {s.slot_name} ({s.slot_kind}) and replace "
                f"{s.current_player_name} with {s.new_player_name} ({from_desc}, salary delta: ${s.salary_delta:+d})."
            )
            step_num += 1
    else:
        # Some swaps are part of a circular exchange cycle
        sequenced_set = set(ordered_indices)
        for idx in ordered_indices:
            s = swaps[idx]
            from_desc = f"from bench/pool" if s.new_card_id not in holders else f"released from {holders[s.new_card_id].lineup_id}"
            steps.append(
                f"Step {step_num}: In {s.lineup_id}, open slot {s.slot_name} ({s.slot_kind}) and replace "
                f"{s.current_player_name} with {s.new_player_name} ({from_desc}, salary delta: ${s.salary_delta:+d})."
            )
            step_num += 1

        cycle_indices = [i for i in range(len(swaps)) if i not in sequenced_set]
        cycle_lineups = [swaps[i].lineup_id for i in cycle_indices]
        steps.append(
            f"Notice: Direct sequential execution blocked by mutual card exchange cycle between {', '.join(cycle_lineups)}. "
            f"GameBlazers does not support simultaneous multi-lineup commits. "
            f"To execute in-app without copy violations, temporarily swap an open bench card into one lineup first to break the cycle, "
            f"or apply the swap when replacing lineups."
        )
        for idx in cycle_indices:
            s = swaps[idx]
            steps.append(
                f"Cycle Move: In {s.lineup_id}, slot {s.slot_name} ({s.slot_kind}): "
                f"{s.current_player_name} -> {s.new_player_name} (needs card currently held in {holders[s.new_card_id].lineup_id})."
            )

    return steps


# ---------------------------------------------------------------------------
# Pre-Configured Working Examples & Demonstration Scenarios
# ---------------------------------------------------------------------------


def load_thursday_breakout_scenario() -> tuple[WeeklyPlan, dict[str, PlayerLiveState]]:
    """Scenario 1: Thursday Breakout.

    Lineup 1 started cheap TE Colby Parkinson (projected 4.0, 1.5x) who explodes for 21.0 actual pts.
    Lineup 1 banks 31.5 points on Thursday. Thursday game is FINAL.
    Reallocation engine detects Lineup 1 as a Surging Contender and reallocates elite Sunday/Monday assets into it.
    """
    contests = get_default_contests()
    scorcher = contests["Scorcher"]

    c_parkinson = OwnedCard("c_parkinson", "Colby Parkinson", "colbyparkinson", "LAR", "TE", 1.5, 3400, "Active", 2)
    c_allen = OwnedCard("c_allen", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 7800, "Active", 3)
    c_cook = OwnedCard("c_cook", "James Cook", "jamescook", "BUF", "RB", 1.2, 6900, "Active", 4)
    c_diggs = OwnedCard("c_diggs", "Stefon Diggs", "stefondiggs", "HOU", "WR", 1.1, 6500, "Active", 5)
    c_flex_low = OwnedCard("c_flex_low", "Gabe Davis", "gabedavis", "JAX", "WR", 1.0, 4800, "Active", 6)

    c_mahomes = OwnedCard("c_mahomes", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.0, 7200, "Active", 7)
    c_pacheco = OwnedCard("c_pacheco", "Isiah Pacheco", "isiahpacheco", "KC", "RB", 1.0, 6400, "Active", 8)
    c_rice = OwnedCard("c_rice", "Rashee Rice", "rasheerice", "KC", "WR", 1.2, 6300, "Active", 9)
    c_kelce = OwnedCard("c_kelce", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 6100, "Active", 10)
    c_flex_mid = OwnedCard("c_flex_mid", "Jayden Reed", "jaydenreed", "GB", "WR", 1.0, 5600, "Active", 11)

    # Sunday bench upgrade cards
    c_kyren = OwnedCard("c_kyren", "Kyren Williams", "kyrenwilliams", "LAR", "RB", 1.3, 7600, "Active", 12)
    c_stbrown = OwnedCard("c_stbrown", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.2, 8200, "Active", 13)

    cards = [c_parkinson, c_allen, c_cook, c_diggs, c_flex_low, c_mahomes, c_pacheco, c_rice, c_kelce, c_flex_mid, c_kyren, c_stbrown]
    pc_dict = {
        c.card_id: PlannerCard(
            card=c,
            raw_projection=15.0,
            adjusted_projection=round(15.0 * c.multiplier, 2),
            weekly_salary=c.roster_salary,
            salary_source="dff",
            is_matched=True,
        )
        for c in cards
    }
    pc_dict["c_parkinson"].raw_projection = 4.0
    pc_dict["c_parkinson"].adjusted_projection = 6.0
    pc_dict["c_kyren"].raw_projection = 18.0
    pc_dict["c_kyren"].adjusted_projection = 23.4
    pc_dict["c_stbrown"].raw_projection = 19.0
    pc_dict["c_stbrown"].adjusted_projection = 22.8

    l1 = PlannerLineup(
        lineup_id="scorcher_1",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_allen"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_cook"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_diggs"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_parkinson"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_flex_low"]),
        ],
    )
    l2 = PlannerLineup(
        lineup_id="scorcher_2",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_mahomes"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_pacheco"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_rice"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_kelce"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_flex_mid"]),
        ],
    )

    recalculate_lineup(l1, scorcher)
    recalculate_lineup(l2, scorcher)

    plan = WeeklyPlan(
        plan_id="thursday_breakout_demo",
        name="Thursday Breakout Demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        slate_id="thursday_slate",
        salary_source="dff",
        contests=contests,
        lineups=[l1, l2],
        roster_cards=list(pc_dict.values()),
        projection_overrides={},
        excluded_athlete_keys=[],
    )

    live_states = {
        "colbyparkinson": PlayerLiveState(
            athlete_key="colbyparkinson",
            player_name="Colby Parkinson",
            team="LAR",
            position="TE",
            game_status=GAME_STATUS_FINAL,
            kickoff_time="2026-09-10T20:15:00-04:00",
            final_points=21.0,
            remaining_projection=0.0,
            remaining_stdev=0.0,
            profile_type="standard",
            game_clock="Final",
        ),
        "kyrenwilliams": PlayerLiveState(
            athlete_key="kyrenwilliams",
            player_name="Kyren Williams",
            team="LAR",
            position="RB",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-13T16:25:00-04:00",
            remaining_projection=18.0,
            remaining_stdev=4.0,
            profile_type="reliable",
        ),
        "amonrastbrown": PlayerLiveState(
            athlete_key="amonrastbrown",
            player_name="Amon-Ra St. Brown",
            team="DET",
            position="WR",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-13T20:20:00-04:00",
            remaining_projection=19.0,
            remaining_stdev=4.5,
            profile_type="reliable",
        ),
    }

    return plan, live_states


def load_monday_player_choice_scenario() -> tuple[WeeklyPlan, dict[str, PlayerLiveState]]:
    """Scenario 2: Monday Player-Choice (Floor Defense vs Cash Line).

    Sunday games completed. Two lineups enter Monday Night Football:
    - Lineup 1 is sitting at 142.0 banked pts, needing 14.0 pts to clinch Top 10 ($40 payout).
      A score of 4 points drops it below cutoff ($0).
    - Lineup 2 is at 118.0 banked pts, needing 27.0 pts to reach cash cutoff ($15 payout).
      A score of 15 points yields $0.
    Monday candidates:
    - Player A (Consistent): exactly 15.0 pts (stdev 1.5).
    - Player B (Volatile): bimodal (50% chance of 4.0 pts, 50% chance of 32.0 pts).
    """
    contests = get_default_contests()
    scorcher = contests["Scorcher"]

    c_sunday_1 = OwnedCard("c_sun1", "Sunday Player 1", "sundayplayer1", "BUF", "QB", 1.0, 7000, "Active", 2)
    c_sunday_2 = OwnedCard("c_sun2", "Sunday Player 2", "sundayplayer2", "KC", "RB", 1.0, 7000, "Active", 3)
    c_sunday_3 = OwnedCard("c_sun3", "Sunday Player 3", "sundayplayer3", "HOU", "WR", 1.0, 7000, "Active", 4)
    c_sunday_4 = OwnedCard("c_sun4", "Sunday Player 4", "sundayplayer4", "BAL", "TE", 1.0, 7000, "Active", 5)

    c_sunday_5 = OwnedCard("c_sun5", "Sunday Player 5", "sundayplayer5", "DET", "QB", 1.0, 7000, "Active", 6)
    c_sunday_6 = OwnedCard("c_sun6", "Sunday Player 6", "sundayplayer6", "PHI", "RB", 1.0, 7000, "Active", 7)
    c_sunday_7 = OwnedCard("c_sun7", "Sunday Player 7", "sundayplayer7", "DAL", "WR", 1.0, 7000, "Active", 8)
    c_sunday_8 = OwnedCard("c_sun8", "Sunday Player 8", "sundayplayer8", "MIA", "TE", 1.0, 7000, "Active", 9)

    c_consistent = OwnedCard("c_consistent", "Tyler Lockett", "tylerlockett", "SEA", "WR", 1.0, 5800, "Active", 10)
    c_volatile = OwnedCard("c_volatile", "Christian Watson", "christianwatson", "GB", "WR", 1.0, 6000, "Active", 11)

    cards = [c_sunday_1, c_sunday_2, c_sunday_3, c_sunday_4, c_sunday_5, c_sunday_6, c_sunday_7, c_sunday_8, c_consistent, c_volatile]
    pc_dict = {
        c.card_id: PlannerCard(
            card=c,
            raw_projection=15.0,
            adjusted_projection=15.0,
            weekly_salary=c.roster_salary,
            salary_source="dff",
            is_matched=True,
        )
        for c in cards
    }

    # Lineup 1 initially has volatile card, Lineup 2 has consistent card (suboptimal)
    l1 = PlannerLineup(
        lineup_id="scorcher_1",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_sun1"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_sun2"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_sun3"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_sun4"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_volatile"]),
        ],
    )
    l2 = PlannerLineup(
        lineup_id="scorcher_2",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_sun5"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_sun6"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_sun7"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_sun8"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_consistent"]),
        ],
    )

    recalculate_lineup(l1, scorcher)
    recalculate_lineup(l2, scorcher)

    plan = WeeklyPlan(
        plan_id="monday_choice_demo",
        name="Monday Player-Choice Demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        slate_id="monday_slate",
        salary_source="dff",
        contests=contests,
        lineups=[l1, l2],
        roster_cards=list(pc_dict.values()),
        projection_overrides={},
        excluded_athlete_keys=[],
    )

    live_states = {
        # Sunday players: FINAL
        "sundayplayer1": PlayerLiveState("sundayplayer1", "Sunday Player 1", "BUF", "QB", GAME_STATUS_FINAL, final_points=31.25),
        "sundayplayer2": PlayerLiveState("sundayplayer2", "Sunday Player 2", "KC", "RB", GAME_STATUS_FINAL, final_points=31.25),
        "sundayplayer3": PlayerLiveState("sundayplayer3", "Sunday Player 3", "HOU", "WR", GAME_STATUS_FINAL, final_points=31.25),
        "sundayplayer4": PlayerLiveState("sundayplayer4", "Sunday Player 4", "BAL", "TE", GAME_STATUS_FINAL, final_points=31.25),
        # Total banked L1 = 125.0

        "sundayplayer5": PlayerLiveState("sundayplayer5", "Sunday Player 5", "DET", "QB", GAME_STATUS_FINAL, final_points=23.0),
        "sundayplayer6": PlayerLiveState("sundayplayer6", "Sunday Player 6", "PHI", "RB", GAME_STATUS_FINAL, final_points=23.0),
        "sundayplayer7": PlayerLiveState("sundayplayer7", "Sunday Player 7", "DAL", "WR", GAME_STATUS_FINAL, final_points=23.0),
        "sundayplayer8": PlayerLiveState("sundayplayer8", "Sunday Player 8", "MIA", "TE", GAME_STATUS_FINAL, final_points=23.0),
        # Total banked L2 = 92.0

        # Monday options: UPCOMING
        "tylerlockett": PlayerLiveState(
            athlete_key="tylerlockett",
            player_name="Tyler Lockett",
            team="SEA",
            position="WR",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-14T20:15:00-04:00",
            remaining_projection=15.0,
            remaining_stdev=1.5,
            profile_type="reliable",
        ),
        "christianwatson": PlayerLiveState(
            athlete_key="christianwatson",
            player_name="Christian Watson",
            team="GB",
            position="WR",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-14T20:15:00-04:00",
            remaining_projection=18.0,
            remaining_stdev=14.0,
            profile_type="volatile",
            bimodal_outcomes=[(4.0, 0.5), (32.0, 0.5)],
        ),
    }

    return plan, live_states


def load_monday_first_place_chase_scenario() -> tuple[WeeklyPlan, dict[str, PlayerLiveState]]:
    """Scenario 3: Monday 1st-Place Chase (Leading Lineup Benefits from Boom-or-Bust).

    Lineup 1 is currently in 3rd place in Wildfire with 155.0 banked points.
    1st place cutoff: 187.0 pts ($1,200 payout).
    3rd place cutoff: 170.0 pts ($300 payout).
    Remaining Monday candidates:
    - Reliable Player: 15.0 pts (yields 170.0 pts = 3rd place = $300 payout).
    - Boom-or-Bust Player: 40% chance of 33.0 pts (188.0 pts = 1st place = $1,200), 60% chance of 5.0 pts ($0).
    Expected payout with Volatile: 0.40 * 1200 = $480.
    Expected payout with Reliable: 1.00 * 300 = $300.
    The leading lineup properly chooses the boom-or-bust player.
    """
    contests = get_default_contests()
    wildfire = contests["Wildfire"]

    c_sun1 = OwnedCard("c_wf_sun1", "Sunday Star 1", "sundaystar1", "BUF", "QB", 1.0, 8000, "Active", 2)
    c_sun2 = OwnedCard("c_wf_sun2", "Sunday Star 2", "sundaystar2", "KC", "RB", 1.0, 8000, "Active", 3)
    c_sun3 = OwnedCard("c_wf_sun3", "Sunday Star 3", "sundaystar3", "DET", "WR", 1.0, 8000, "Active", 4)
    c_sun4 = OwnedCard("c_wf_sun4", "Sunday Star 4", "sundaystar4", "BAL", "TE", 1.0, 8000, "Active", 5)
    c_sun5 = OwnedCard("c_wf_sun5", "Sunday Star 5", "sundaystar5", "PHI", "WR", 1.0, 8000, "Active", 6)

    c_reliable = OwnedCard("c_safe_vet", "Safe Veteran", "safeveteran", "LAR", "WR", 1.0, 5000, "Active", 7)
    c_volatile = OwnedCard("c_boom_rookie", "Boom Rookie", "boomrookie", "SF", "WR", 1.0, 5000, "Active", 8)

    cards = [c_sun1, c_sun2, c_sun3, c_sun4, c_sun5, c_reliable, c_volatile]
    pc_dict = {
        c.card_id: PlannerCard(
            card=c,
            raw_projection=15.0,
            adjusted_projection=15.0,
            weekly_salary=c.roster_salary,
            salary_source="dff",
            is_matched=True,
        )
        for c in cards
    }

    l1 = PlannerLineup(
        lineup_id="wildfire_1",
        contest_name="Wildfire",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_wf_sun1"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_wf_sun2"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_wf_sun3"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_wf_sun4"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_wf_sun5"]),
            LineupSlotAssignment("Flex2", "Flex", pc_dict["c_safe_vet"]),  # currently has reliable
        ],
    )
    recalculate_lineup(l1, wildfire)

    plan = WeeklyPlan(
        plan_id="monday_first_place_demo",
        name="Monday 1st-Place Chase Demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        slate_id="monday_wf_slate",
        salary_source="dff",
        contests=contests,
        lineups=[l1],
        roster_cards=list(pc_dict.values()),
        projection_overrides={},
        excluded_athlete_keys=[],
    )

    live_states = {
        "sundaystar1": PlayerLiveState("sundaystar1", "Sunday Star 1", "BUF", "QB", GAME_STATUS_FINAL, final_points=31.0),
        "sundaystar2": PlayerLiveState("sundaystar2", "Sunday Star 2", "KC", "RB", GAME_STATUS_FINAL, final_points=31.0),
        "sundaystar3": PlayerLiveState("sundaystar3", "Sunday Star 3", "DET", "WR", GAME_STATUS_FINAL, final_points=31.0),
        "sundaystar4": PlayerLiveState("sundaystar4", "Sunday Star 4", "BAL", "TE", GAME_STATUS_FINAL, final_points=31.0),
        "sundaystar5": PlayerLiveState("sundaystar5", "Sunday Star 5", "PHI", "WR", GAME_STATUS_FINAL, final_points=31.0),
        # Total banked L1 = 155.0

        "safeveteran": PlayerLiveState(
            athlete_key="safeveteran",
            player_name="Safe Veteran",
            team="LAR",
            position="WR",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-14T20:15:00-04:00",
            remaining_projection=15.0,
            remaining_stdev=1.0,
            profile_type="reliable",
        ),
        "boomrookie": PlayerLiveState(
            athlete_key="boomrookie",
            player_name="Boom Rookie",
            team="SF",
            position="WR",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-14T20:15:00-04:00",
            remaining_projection=16.2,
            remaining_stdev=13.5,
            profile_type="volatile",
            bimodal_outcomes=[(5.0, 0.6), (33.0, 0.4)],
        ),
    }

    return plan, live_states


def load_inprogress_dependent_chain_scenario() -> tuple[WeeklyPlan, dict[str, PlayerLiveState]]:
    """Scenario 4: In-Progress Game & Multi-Lineup Dependent Chain.

    Sunday 1 PM games are in progress:
    - Josh Allen is IN_PROGRESS (Q3 08:45, 18.5 live points, 6.0 remaining projection).
    - Started slots are locked.
    - An unlocked late-afternoon RB can move to Lineup 1, shifting a WR to Lineup 2, shifting a flex to bench.
    """
    contests = get_default_contests()
    scorcher = contests["Scorcher"]

    c_allen = OwnedCard("c_inprog_allen", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 7800, "Active", 2)
    c_cook = OwnedCard("c_inprog_cook", "James Cook", "jamescook", "BUF", "RB", 1.0, 6800, "Active", 3)
    c_diggs = OwnedCard("c_inprog_diggs", "Stefon Diggs", "stefondiggs", "HOU", "WR", 1.0, 6500, "Active", 4)
    c_kincaid = OwnedCard("c_inprog_kincaid", "Dalton Kincaid", "daltonkincaid", "BUF", "TE", 1.0, 5200, "Active", 5)
    c_shakir = OwnedCard("c_inprog_shakir", "Khalil Shakir", "khalilshakir", "BUF", "WR", 1.0, 5400, "Active", 6)

    c_stafford = OwnedCard("c_late_stafford", "Matthew Stafford", "matthewstafford", "LAR", "QB", 1.0, 6400, "Active", 7)
    c_kyren = OwnedCard("c_late_kyren", "Kyren Williams", "kyrenwilliams", "LAR", "RB", 1.0, 7600, "Active", 8)
    c_kupp = OwnedCard("c_late_kupp", "Cooper Kupp", "cooperkupp", "LAR", "WR", 1.0, 7500, "Active", 9)
    c_laporta = OwnedCard("c_late_laporta", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 5600, "Active", 10)
    c_davis = OwnedCard("c_late_davis", "Gabe Davis", "gabedavis", "JAX", "WR", 1.0, 4600, "Active", 11)

    # Bench premium RB
    c_gibbs = OwnedCard("c_bench_gibbs", "Jahmyr Gibbs", "jahmyrgibbs", "DET", "RB", 1.2, 7400, "Active", 12)

    cards = [c_allen, c_cook, c_diggs, c_kincaid, c_shakir, c_stafford, c_kyren, c_kupp, c_laporta, c_davis, c_gibbs]
    pc_dict = {
        c.card_id: PlannerCard(
            card=c,
            raw_projection=14.0,
            adjusted_projection=round(14.0 * c.multiplier, 2),
            weekly_salary=c.roster_salary,
            salary_source="dff",
            is_matched=True,
        )
        for c in cards
    }
    pc_dict["c_bench_gibbs"].raw_projection = 18.0
    pc_dict["c_bench_gibbs"].adjusted_projection = 21.6

    l1 = PlannerLineup(
        lineup_id="scorcher_1",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_inprog_allen"]),  # IN PROGRESS
            LineupSlotAssignment("RB", "RB", pc_dict["c_inprog_cook"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_inprog_diggs"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_inprog_kincaid"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_inprog_shakir"]),
        ],
    )
    l2 = PlannerLineup(
        lineup_id="scorcher_2",
        contest_name="Scorcher",
        slots=[
            LineupSlotAssignment("QB", "QB", pc_dict["c_late_stafford"]),
            LineupSlotAssignment("RB", "RB", pc_dict["c_late_kyren"]),
            LineupSlotAssignment("WR", "WR", pc_dict["c_late_kupp"]),
            LineupSlotAssignment("TE", "TE", pc_dict["c_late_laporta"]),
            LineupSlotAssignment("Flex", "Flex", pc_dict["c_late_davis"]),
        ],
    )

    recalculate_lineup(l1, scorcher)
    recalculate_lineup(l2, scorcher)

    plan = WeeklyPlan(
        plan_id="inprogress_chain_demo",
        name="In-Progress Dependent Chain Demo",
        created_at=datetime.now(timezone.utc).isoformat(),
        updated_at=datetime.now(timezone.utc).isoformat(),
        slate_id="sunday_slate",
        salary_source="dff",
        contests=contests,
        lineups=[l1, l2],
        roster_cards=list(pc_dict.values()),
        projection_overrides={},
        excluded_athlete_keys=[],
    )

    live_states = {
        "joshallen": PlayerLiveState(
            athlete_key="joshallen",
            player_name="Josh Allen",
            team="BUF",
            position="QB",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00-04:00",
            live_points=18.5,
            remaining_projection=6.0,
            remaining_stdev=3.0,
            game_clock="Q3 08:45",
        ),
        "jamescook": PlayerLiveState(
            athlete_key="jamescook",
            player_name="James Cook",
            team="BUF",
            position="RB",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00-04:00",
            live_points=14.0,
            remaining_projection=4.0,
            remaining_stdev=2.5,
            game_clock="Q3 08:45",
        ),
        "stefondiggs": PlayerLiveState(
            athlete_key="stefondiggs",
            player_name="Stefon Diggs",
            team="HOU",
            position="WR",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00-04:00",
            live_points=12.2,
            remaining_projection=5.0,
            remaining_stdev=2.8,
            game_clock="Q3 08:45",
        ),
        "daltonkincaid": PlayerLiveState(
            athlete_key="daltonkincaid",
            player_name="Dalton Kincaid",
            team="BUF",
            position="TE",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00-04:00",
            live_points=8.0,
            remaining_projection=3.0,
            remaining_stdev=2.0,
            game_clock="Q3 08:45",
        ),
        "khalilshakir": PlayerLiveState(
            athlete_key="khalilshakir",
            player_name="Khalil Shakir",
            team="BUF",
            position="WR",
            game_status=GAME_STATUS_IN_PROGRESS,
            kickoff_time="2026-09-13T13:00:00-04:00",
            live_points=16.4,
            remaining_projection=3.5,
            remaining_stdev=2.0,
            game_clock="Q3 08:45",
        ),
        # Late games: UPCOMING
        "jahmyrgibbs": PlayerLiveState(
            athlete_key="jahmyrgibbs",
            player_name="Jahmyr Gibbs",
            team="DET",
            position="RB",
            game_status=GAME_STATUS_UPCOMING,
            kickoff_time="2026-09-13T20:20:00-04:00",
            remaining_projection=18.0,
            remaining_stdev=4.5,
            profile_type="reliable",
        ),
    }

    return plan, live_states


def parse_live_scores_csv(stream_or_str: Any) -> list[PlayerLiveState]:
    """Parse CSV containing live player updates, scores, and kickoff timestamps."""
    if isinstance(stream_or_str, str):
        stream = io.StringIO(stream_or_str)
    elif hasattr(stream_or_str, "read"):
        content = stream_or_str.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8-sig")
        stream = io.StringIO(content)
    else:
        raise ValueError("Invalid live scores input stream")

    reader = csv.DictReader(stream)
    headers = list(reader.fieldnames or [])
    h_map = {h.casefold().replace(" ", "").replace("_", ""): h for h in headers}

    name_col = next((h_map[k] for k in ("playername", "player", "name") if k in h_map), None)
    team_col = next((h_map[k] for k in ("team", "teamcode") if k in h_map), None)
    pos_col = next((h_map[k] for k in ("position", "pos") if k in h_map), None)
    status_col = next((h_map[k] for k in ("status", "gamestatus") if k in h_map), None)
    ko_col = next((h_map[k] for k in ("kickoff", "kickofftime", "time") if k in h_map), None)
    live_col = next((h_map[k] for k in ("livepoints", "livepts", "score") if k in h_map), None)
    final_col = next((h_map[k] for k in ("finalpoints", "finalpts") if k in h_map), None)
    rem_col = next((h_map[k] for k in ("remainingproj", "remproj", "proj") if k in h_map), None)
    profile_col = next((h_map[k] for k in ("profile", "profiletype", "risk") if k in h_map), None)

    if not name_col:
        raise ValueError("Live scores CSV must contain a Player Name column.")

    states: list[PlayerLiveState] = []
    for row in reader:
        pname = str(row.get(name_col, "") or "").strip()
        if not pname:
            continue
        akey = normalize_name(pname)
        team = normalize_team(row.get(team_col)) if team_col else ""
        pos = normalize_position(row.get(pos_col)) if pos_col else ""
        g_status = str(row.get(status_col, GAME_STATUS_UPCOMING)).strip().upper()
        if g_status not in (GAME_STATUS_UPCOMING, GAME_STATUS_IN_PROGRESS, GAME_STATUS_FINAL):
            g_status = GAME_STATUS_UPCOMING

        ko_time = str(row.get(ko_col, "")).strip() if ko_col else ""
        try:
            live_pts = float(str(row.get(live_col, 0.0)).replace(",", "")) if live_col else 0.0
        except (ValueError, TypeError):
            live_pts = 0.0
        try:
            final_pts = float(str(row.get(final_col, 0.0)).replace(",", "")) if final_col else 0.0
        except (ValueError, TypeError):
            final_pts = 0.0
        try:
            rem_proj = float(str(row.get(rem_col, 0.0)).replace(",", "")) if rem_col else 0.0
        except (ValueError, TypeError):
            rem_proj = 0.0

        profile = str(row.get(profile_col, "standard")).strip().lower() if profile_col else "standard"
        if profile not in ("reliable", "volatile", "standard"):
            profile = "standard"

        states.append(
            PlayerLiveState(
                athlete_key=akey,
                player_name=pname,
                team=team,
                position=pos,
                game_status=g_status,
                kickoff_time=ko_time,
                live_points=live_pts,
                final_points=final_pts,
                remaining_projection=rem_proj,
                remaining_stdev=round(0.35 * rem_proj, 2),
                remaining_is_estimate=bool(rem_col),
                profile_type=profile,
            )
        )
    return states
