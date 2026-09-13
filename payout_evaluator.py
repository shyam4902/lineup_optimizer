"""GameBlazers payout evaluator and threshold scoring.

Strictly separates:
1. Projected lineup score.
2. Score distributions and contest cutoffs.
3. Payout associated with each finishing band.

Distinguishes deterministic payout-at-projected-score estimates from probabilistic expected earnings,
and guarantees mutually exclusive payout bands without double-counting.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from contest_config import ContestConfig, HistoricalThreshold, PayoutBand


@dataclass
class DeterministicTierEvaluation:
    contest_name: str
    projected_score: float
    achieved_tier: str | None
    cutoff_score: float | None
    estimated_payout: float
    estimate_type: str = "payout_at_projected_score"
    uncertainty_note: str = ""
    bands_evaluated: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MutuallyExclusiveBandOutcome:
    band_name: str
    start_rank: int
    end_rank: int
    prize_per_entry: float
    probability: float
    expected_value: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProbabilisticPayoutEvaluation:
    contest_name: str
    projected_score: float
    lineup_stdev: float
    entry_fee: float
    band_outcomes: list[MutuallyExclusiveBandOutcome]
    gross_expected_payout: float
    net_expected_payout: float
    out_of_the_money_probability: float
    estimate_type: str = "scenario_probabilistic_expected_payout"
    data_limitation_notice: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_deterministic_tier(
    projected_score: float,
    contest: ContestConfig,
) -> DeterministicTierEvaluation:
    """Evaluate projected score against historical threshold tiers deterministically.

    This produces a 'payout_at_projected_score' estimate based on historical fitted cutoffs.
    It is NOT an expected earnings figure.
    """
    thresholds = sorted(contest.historical_thresholds, key=lambda th: th.estimate, reverse=True)
    if not thresholds:
        return DeterministicTierEvaluation(
            contest_name=contest.name,
            projected_score=projected_score,
            achieved_tier=None,
            cutoff_score=None,
            estimated_payout=0.0,
            uncertainty_note="No historical threshold data available for this contest.",
        )

    matched_threshold: HistoricalThreshold | None = None
    for th in thresholds:
        if projected_score >= th.estimate:
            matched_threshold = th
            break

    estimated_payout = 0.0
    achieved_tier_name = None
    cutoff = None

    if matched_threshold is not None:
        achieved_tier_name = matched_threshold.tier
        cutoff = matched_threshold.estimate
        rank = matched_threshold.rank_cutoff
        # Find corresponding payout band for this rank
        for band in contest.payout_bands:
            if band.start_rank <= rank <= band.end_rank:
                estimated_payout = band.payout_per_entry
                break
        if estimated_payout == 0.0 and contest.payout_bands:
            # If rank is within top range
            eligible_bands = [b for b in contest.payout_bands if b.end_rank >= rank]
            if eligible_bands:
                estimated_payout = min(eligible_bands, key=lambda b: b.end_rank).payout_per_entry

    uncertainty = (
        f"Based on historical Bayesian models ({matched_threshold.source_date if matched_threshold else 'historical'}). "
        f"Deterministic tier estimate, not calibrated live probability."
    )

    return DeterministicTierEvaluation(
        contest_name=contest.name,
        projected_score=round(projected_score, 2),
        achieved_tier=achieved_tier_name,
        cutoff_score=cutoff,
        estimated_payout=estimated_payout,
        estimate_type="payout_at_projected_score",
        uncertainty_note=uncertainty,
    )


def normal_cdf(x: float, mean: float, std: float) -> float:
    """Standard normal cumulative distribution function."""
    if std <= 0.0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2.0))))


def evaluate_probabilistic_payout(
    projected_score: float,
    contest: ContestConfig,
    lineup_stdev: float = 16.0,
    threshold_adjustments: Mapping[str, float] | None = None,
) -> ProbabilisticPayoutEvaluation:
    """Evaluate lineup score distribution across mutually exclusive finishing bands.

    Guarantees:
    1. Overlapping tiers are decomposed into disjoint mutually exclusive bands.
    2. Sum of all band probabilities + out-of-money probability equals 1.0.
    3. Net expected payout subtracts applicable entry fee.
    """
    thresholds = sorted(contest.historical_thresholds, key=lambda th: th.estimate, reverse=True)
    if not thresholds:
        return ProbabilisticPayoutEvaluation(
            contest_name=contest.name,
            projected_score=projected_score,
            lineup_stdev=lineup_stdev,
            entry_fee=contest.entry_fee,
            band_outcomes=[],
            gross_expected_payout=0.0,
            net_expected_payout=0.0,
            out_of_the_money_probability=1.0,
            data_limitation_notice="No threshold model available to construct finishing distribution.",
        )

    adjustments = threshold_adjustments or {}

    # Calculate cumulative probability of exceeding each threshold
    # P(Score >= Cutoff) = 1 - CDF(Cutoff; mu=projected_score, sigma=lineup_stdev)
    cum_probs: list[tuple[HistoricalThreshold, float]] = []
    for th in thresholds:
        cutoff = adjustments.get(th.tier, th.estimate)
        prob_exceed = 1.0 - normal_cdf(cutoff, projected_score, lineup_stdev)
        cum_probs.append((th, max(0.0, min(1.0, prob_exceed))))

    # Decompose into mutually exclusive bands:
    # Band 0 (Highest tier): P(exceeding highest cutoff)
    # Band k: P(exceeding cutoff k) - P(exceeding cutoff k-1)
    band_outcomes: list[MutuallyExclusiveBandOutcome] = []
    cum_allocated_prob = 0.0

    for idx, (th, prob_ge) in enumerate(cum_probs):
        if idx == 0:
            band_prob = prob_ge
        else:
            prev_prob_ge = cum_probs[idx - 1][1]
            band_prob = max(0.0, prob_ge - prev_prob_ge)

        cum_allocated_prob += band_prob

        # Find payout for this tier's rank cutoff
        payout = 0.0
        for b in contest.payout_bands:
            if b.start_rank <= th.rank_cutoff <= b.end_rank:
                payout = b.payout_per_entry
                break
        if payout == 0.0 and contest.payout_bands:
            eligible = [b for b in contest.payout_bands if b.end_rank >= th.rank_cutoff]
            if eligible:
                payout = min(eligible, key=lambda b: b.end_rank).payout_per_entry

        prob_rounded = round(band_prob, 5)
        band_outcomes.append(
            MutuallyExclusiveBandOutcome(
                band_name=th.tier,
                start_rank=1 if idx == 0 else cum_probs[idx - 1][0].rank_cutoff + 1,
                end_rank=th.rank_cutoff,
                prize_per_entry=payout,
                probability=prob_rounded,
                expected_value=round(prob_rounded * payout, 4),
            )
        )

    out_of_money_prob = max(0.0, 1.0 - cum_allocated_prob)
    gross_ev = sum(b.expected_value for b in band_outcomes)
    net_ev = gross_ev - contest.entry_fee

    notice = (
        "Scenario-based probabilistic payout. Note: historical thresholds are fitted model means, "
        "not live dynamic cutoffs. Live contest field distributions are required for true calibrated EV."
    )

    return ProbabilisticPayoutEvaluation(
        contest_name=contest.name,
        projected_score=round(projected_score, 2),
        lineup_stdev=round(lineup_stdev, 2),
        entry_fee=contest.entry_fee,
        band_outcomes=band_outcomes,
        gross_expected_payout=round(gross_ev, 4),
        net_expected_payout=round(net_ev, 4),
        out_of_the_money_probability=round(out_of_money_prob, 5),
        data_limitation_notice=notice,
    )


def get_probabilistic_ranking_requirements() -> list[dict[str, str]]:
    """Return explicit list of data assets required to transition from provisional threshold scoring to live calibrated EV."""
    return [
        {
            "requirement": "Live Field Contest Score Distribution",
            "current_status": "Missing / Historical Only (2024 weeks 8-13 sample)",
            "impact": "Current model uses static point-estimates for cutoffs. Live contest cutoffs fluctuate with slate scoring.",
            "what_is_needed": "Weekly contest leaderboard export or API feed showing actual 1st, 10th, 50th, 100th place scores.",
        },
        {
            "requirement": "Player Projection Error Covariance",
            "current_status": "Independent Point Estimates (DFF PPG)",
            "impact": "Within-lineup correlation (e.g. QB-WR stack) and opponent game scripts are not captured in simple sum-of-points.",
            "what_is_needed": "Historical week-by-week projection vs actual fantasy point tables to fit empirical covariance matrix.",
        },
        {
            "requirement": "Field Ownership & Duplication Model",
            "current_status": "Unavailable",
            "impact": "In large-field contests, identical or similar lineups split prizes, reducing effective payout at peak ranks.",
            "what_is_needed": "Contest ownership percentages to penalize chalk overlap at top prize tiers.",
        },
        {
            "requirement": "Current Season Predictive Calibration",
            "current_status": "Provisional (In-sample fitted values from 12/7/2024)",
            "impact": "Historical Bayesian estimates are fitted values from weeks 8, 10, 11, 13 of 2024, not future prediction intervals.",
            "what_is_needed": "Weekly model validation against subsequent 2024-2026 contest outcomes.",
        },
    ]
