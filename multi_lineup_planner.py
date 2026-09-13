"""GameBlazers weekly multi-lineup allocation planner and editable workspace.

Supports:
- Portfolio-level MIP allocation across multiple weekly lineups without card copy reuse.
- Preservation of owned card copies (multiple copies of the same athlete handled as distinct assets).
- Exported roster salaries used as-is, with multiplier applied once to raw projection.
- Manual card replacement, locks, exclusions, validation, and recalculation.
- Contest entry limits and inventory feasibility reporting.
"""

from __future__ import annotations

import csv
import io
import itertools
import json
import math
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pulp

from contest_config import ContestConfig, get_default_contests
from dff_client import DFFPlayerProjection, ProjectionsSnapshot
from optimizer_core import (
    FLEX_POSITIONS,
    POSITIONS,
    SUPERFLEX_POSITIONS,
    Diagnostic,
    normalize_name,
    normalize_position,
    normalize_team,
)
from payout_evaluator import evaluate_deterministic_tier


@dataclass(frozen=True)
class OwnedCard:
    card_id: str
    player_name: str
    athlete_key: str
    team: str
    position: str
    multiplier: float
    roster_salary: int
    status: str
    source_row: int
    rarity: str = ""
    franchise: str = ""
    tradeable: bool = True
    expires: str = ""
    collection: str = ""
    collection_group: str = "Core"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PlannerCard:
    card: OwnedCard
    raw_projection: float
    adjusted_projection: float
    weekly_salary: int
    salary_source: str  # 'roster'; the export already includes the multiplier
    is_matched: bool
    is_overridden: bool = False
    is_eligible: bool = True
    ineligibility_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "card_id": self.card.card_id,
            "player_name": self.card.player_name,
            "athlete_key": self.card.athlete_key,
            "team": self.card.team,
            "position": self.card.position,
            "multiplier": self.card.multiplier,
            "roster_salary": self.card.roster_salary,
            "weekly_salary": self.weekly_salary,
            "salary_source": self.salary_source,
            "raw_projection": self.raw_projection,
            "adjusted_projection": self.adjusted_projection,
            "is_matched": self.is_matched,
            "is_overridden": self.is_overridden,
            "is_eligible": self.is_eligible,
            "ineligibility_reason": self.ineligibility_reason,
            "rarity": self.card.rarity,
            "collection": self.card.collection,
            "collection_group": self.card.collection_group,
            "source_row": self.card.source_row,
        }


@dataclass
class LineupSlotAssignment:
    slot_name: str
    slot_kind: str
    card: PlannerCard | None = None
    is_locked: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot_name": self.slot_name,
            "slot_kind": self.slot_kind,
            "card": self.card.to_dict() if self.card else None,
            "is_locked": self.is_locked,
        }


@dataclass
class PlannerLineup:
    lineup_id: str
    contest_name: str
    slots: list[LineupSlotAssignment]
    is_locked: bool = False
    is_valid: bool = False
    validation_errors: list[str] = field(default_factory=list)
    total_salary: int = 0
    remaining_salary: int = 0
    total_projection: float = 0.0
    estimated_tier: str | None = None
    estimated_payout: float = 0.0
    estimate_type: str = "payout_at_projected_score"
    contest_benchmark: float = 0.0
    benchmark_percentage: float = 0.0
    unused_salary_flag: bool = False
    unused_salary_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "lineup_id": self.lineup_id,
            "contest_name": self.contest_name,
            "slots": [s.to_dict() for s in self.slots],
            "is_locked": self.is_locked,
            "is_valid": self.is_valid,
            "validation_errors": self.validation_errors,
            "total_salary": self.total_salary,
            "remaining_salary": self.remaining_salary,
            "total_projection": self.total_projection,
            "estimated_tier": self.estimated_tier,
            "estimated_payout": self.estimated_payout,
            "estimate_type": self.estimate_type,
            "contest_benchmark": self.contest_benchmark,
            "benchmark_percentage": self.benchmark_percentage,
            "unused_salary_flag": self.unused_salary_flag,
            "unused_salary_note": self.unused_salary_note,
        }


@dataclass
class WeeklyPlan:
    plan_id: str
    name: str
    created_at: str
    updated_at: str
    slate_id: str
    salary_source: str  # 'roster'
    contests: dict[str, ContestConfig]
    lineups: list[PlannerLineup]
    roster_cards: list[PlannerCard]
    projection_overrides: dict[str, float]
    excluded_athlete_keys: list[str]
    total_weekly_projection: float = 0.0
    total_weekly_estimated_payout: float = 0.0
    inventory_summary: dict[str, Any] = field(default_factory=dict)
    contest_benchmarks: dict[str, dict[str, Any]] = field(default_factory=dict)
    quality_threshold: float = 0.0
    preserved_existing_lineups: list[PlannerLineup] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "slate_id": self.slate_id,
            "salary_source": self.salary_source,
            "contests": {k: v.to_dict() for k, v in self.contests.items()},
            "lineups": [lineup.to_dict() for lineup in self.lineups],
            "preserved_existing_lineups": [lineup.to_dict() for lineup in self.preserved_existing_lineups],
            "roster_cards": [c.to_dict() for c in self.roster_cards],
            "projection_overrides": self.projection_overrides,
            "excluded_athlete_keys": self.excluded_athlete_keys,
            "total_weekly_projection": self.total_weekly_projection,
            "total_weekly_estimated_payout": self.total_weekly_estimated_payout,
            "inventory_summary": self.inventory_summary,
            "contest_benchmarks": self.contest_benchmarks,
            "quality_threshold": self.quality_threshold,
        }


def parse_roster_cards(source: Any) -> list[OwnedCard]:
    """Parse a roster source into a list of OwnedCards, preserving individual copies."""
    if hasattr(source, "read"):
        content = source.read()
        if isinstance(content, bytes):
            content = content.decode("utf-8-sig")
        stream = io.StringIO(content)
    elif isinstance(source, (str, Path)):
        p = Path(source)
        if p.exists():
            with p.open("r", encoding="utf-8-sig") as f:
                stream = io.StringIO(f.read())
        else:
            stream = io.StringIO(str(source))
    else:
        raise ValueError("Invalid roster source type")

    reader = csv.DictReader(stream)
    headers = list(reader.fieldnames or [])
    if not headers:
        raise ValueError("Roster CSV has no header row")

    header_map = {h.lstrip("\ufeff").casefold().replace(" ", "").replace("_", ""): h for h in headers}
    name_col = next((header_map[k] for k in ("playername", "player") if k in header_map), None)
    team_col = next((header_map[k] for k in ("team", "teamcode") if k in header_map), None)
    pos_col = next((header_map[k] for k in ("position", "positions", "pos") if k in header_map), None)
    mult_col = next((header_map[k] for k in ("multiplier", "mult") if k in header_map), None)
    sal_col = next((header_map[k] for k in ("salary", "sal") if k in header_map), None)
    status_col = next((header_map[k] for k in ("cardstatus", "status") if k in header_map), None)
    rarity_col = next((header_map[k] for k in ("rarity",) if k in header_map), None)
    collection_col = next((header_map[k] for k in ("collection", "cardcollection") if k in header_map), None)

    if not name_col or not pos_col or not mult_col or not sal_col:
        raise ValueError(f"Roster missing required columns. Found headers: {headers}")

    cards: list[OwnedCard] = []
    for idx, row in enumerate(reader):
        source_row = idx + 2
        raw_name = str(row.get(name_col, "") or "").strip()
        raw_team = row.get(team_col) if team_col else ""
        raw_pos = row.get(pos_col) if pos_col else ""

        pos_norm = normalize_position(raw_pos)
        team_norm = normalize_team(raw_team)

        try:
            mult = float(str(row.get(mult_col, 1.0)).replace(",", "").replace("x", ""))
        except (ValueError, TypeError):
            mult = 1.0

        try:
            sal = int(float(str(row.get(sal_col, 0)).replace(",", "").replace("$", ""))) if sal_col else 0
        except (ValueError, TypeError, OverflowError):
            sal = 0

        status = str(row.get(status_col, "Active") if status_col else "Active").strip()
        rarity = str(row.get(rarity_col, "") if rarity_col else "").strip()
        raw_collection = str(row.get(collection_col, "") if collection_col else "").strip()
        col_lower = raw_collection.lower()
        if "primetime" in col_lower:
            col_group = "Primetime"
        elif "core" in col_lower:
            col_group = "Core"
        elif raw_collection:
            col_group = raw_collection
        else:
            col_group = "Core"

        card_id = f"card_{source_row}_{normalize_name(raw_name)}"

        cards.append(
            OwnedCard(
                card_id=card_id,
                player_name=raw_name,
                athlete_key=normalize_name(raw_name),
                team=team_norm,
                position=pos_norm,
                multiplier=mult,
                roster_salary=sal,
                status=status,
                source_row=source_row,
                rarity=rarity,
                collection=raw_collection,
                collection_group=col_group,
            )
        )
    return cards


def join_cards_with_projections(
    cards: Sequence[OwnedCard],
    projections: Sequence[DFFPlayerProjection],
    salary_source: str = "roster",
    overrides: Mapping[str, float] | None = None,
) -> list[PlannerCard]:
    """Join owned cards with weekly projections.

    Guarantees:
    - Multiplier is applied to raw_projection exactly once.
    - Roster Salary already includes the multiplier and is used unchanged.
    - Projection-feed salaries never price owned cards. The salary_source argument
      remains accepted for older callers, but always resolves to roster pricing.
    """
    user_overrides = overrides or {}
    proj_map: dict[str, list[DFFPlayerProjection]] = {}
    for p in projections:
        proj_map.setdefault(p.athlete_key, []).append(p)

    planner_cards: list[PlannerCard] = []

    for card in cards:
        if card.status.casefold() not in ("active", "listed"):
            planner_cards.append(
                PlannerCard(
                    card=card,
                    raw_projection=0.0,
                    adjusted_projection=0.0,
                    weekly_salary=card.roster_salary,
                    salary_source="roster",
                    is_matched=False,
                    is_eligible=False,
                    ineligibility_reason=f"Card status is '{card.status}' (Active or Listed required)",
                )
            )
            continue

        matches = proj_map.get(card.athlete_key, [])
        if not matches:
            planner_cards.append(
                PlannerCard(
                    card=card,
                    raw_projection=0.0,
                    adjusted_projection=0.0,
                    weekly_salary=card.roster_salary,
                    salary_source="roster",
                    is_matched=False,
                    is_eligible=False,
                    ineligibility_reason="No weekly projection found for athlete",
                )
            )
            continue

        # Position filter
        same_pos = [p for p in matches if p.position == card.position]
        if not same_pos:
            planner_cards.append(
                PlannerCard(
                    card=card,
                    raw_projection=0.0,
                    adjusted_projection=0.0,
                    weekly_salary=card.roster_salary,
                    salary_source="roster",
                    is_matched=False,
                    is_eligible=False,
                    ineligibility_reason=f"Position mismatch: roster={card.position}, projection={matches[0].position}",
                )
            )
            continue

        # Team match (strictly require matching team; do not fall back to another team)
        same_team = [p for p in same_pos if p.team == card.team]
        if not same_team:
            planner_cards.append(
                PlannerCard(
                    card=card,
                    raw_projection=0.0,
                    adjusted_projection=0.0,
                    weekly_salary=card.roster_salary,
                    salary_source="roster",
                    is_matched=False,
                    is_eligible=False,
                    ineligibility_reason=f"Team mismatch: roster={card.team}, projection={same_pos[0].team}",
                )
            )
            continue

        if len(same_team) > 1:
            planner_cards.append(
                PlannerCard(
                    card=card,
                    raw_projection=0.0,
                    adjusted_projection=0.0,
                    weekly_salary=card.roster_salary,
                    salary_source="roster",
                    is_matched=False,
                    is_eligible=False,
                    ineligibility_reason="Ambiguous match: multiple projections with identical name, team, and position",
                )
            )
            continue

        matched_proj = same_team[0]

        is_overridden = card.athlete_key in user_overrides
        raw_proj = float(user_overrides[card.athlete_key]) if is_overridden else matched_proj.source_projection

        # Multiplier applied exactly once
        adjusted_proj = round(raw_proj * card.multiplier, 3)

        # Validate eligibility rules:
        # 1. raw projection > 0
        # 2. position in POSITIONS
        # 3. multiplier > 0
        # 4. exported roster salary must be positive
        is_eligible = True
        reason = None

        if card.multiplier <= 0:
            is_eligible = False
            reason = f"Non-positive multiplier: {card.multiplier}"
        elif raw_proj <= 0:
            is_eligible = False
            reason = "Projection is zero or negative"
        elif card.position not in POSITIONS:
            is_eligible = False
            reason = f"Unsupported position: {card.position}"
        elif card.roster_salary <= 0:
            is_eligible = False
            reason = "Roster salary is missing, zero or negative"

        planner_cards.append(
            PlannerCard(
                card=card,
                raw_projection=raw_proj,
                adjusted_projection=adjusted_proj,
                weekly_salary=card.roster_salary,
                salary_source="roster",
                is_matched=True,
                is_overridden=is_overridden,
                is_eligible=is_eligible,
                ineligibility_reason=reason,
            )
        )
    return planner_cards


# Explicit designated Primetime game IDs for Week 1 (Wednesday, Thursday, Sunday night, and Monday night)
WEEK_1_PRIMETIME_GAME_IDS: frozenset[str] = frozenset({
    "2026_01_NE_SEA",  # Wednesday kickoff: NE @ SEA
    "2026_01_SF_LA",   # Thursday kickoff: SF @ LAR
    "2026_01_DAL_NYG", # Sunday night kickoff: DAL @ NYG
    "2026_01_DEN_KC",  # Monday night kickoff: DEN @ KC
})

DEFAULT_PRIMETIME_TEAMS: frozenset[str] = frozenset({
    "NE", "SEA", "SF", "LAR", "DAL", "NYG", "DEN", "KC",
})


def get_primetime_eligible_teams(
    schedule_data: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
    designated_game_ids: frozenset[str] | Sequence[str] | None = None,
    week: int = 1,
) -> frozenset[str]:
    """Derive eligible NFL teams from designated weekly primetime game IDs.

    Explicitly restricted to Week 1 in this preview until the selected week's
    designated games are wired through every caller.
    """
    if week != 1:
        raise ValueError(
            f"Primetime Pyro eligibility is restricted to Week 1 in this preview until the selected week's designated games are wired through every caller (requested week {week})."
        )
    if isinstance(schedule_data, Mapping) and schedule_data.get("week") is not None and schedule_data.get("week") != 1:
        raise ValueError(
            f"Primetime Pyro eligibility is restricted to Week 1 in this preview until the selected week's designated games are wired through every caller (received schedule for week {schedule_data.get('week')})."
        )
    if designated_game_ids is not None:
        for gid in designated_game_ids:
            if not gid.startswith("2026_01_"):
                raise ValueError(
                    f"Primetime Pyro eligibility is restricted to Week 1 in this preview until the selected week's designated games are wired through every caller (received game '{gid}')."
                )

    target_ids = set(designated_game_ids or WEEK_1_PRIMETIME_GAME_IDS)
    if not schedule_data:
        return DEFAULT_PRIMETIME_TEAMS

    games: Sequence[Mapping[str, Any]] = []
    if isinstance(schedule_data, Mapping):
        raw_games = schedule_data.get("games")
        if isinstance(raw_games, Sequence):
            games = raw_games
    elif isinstance(schedule_data, Sequence):
        games = schedule_data

    teams: set[str] = set()
    for g in games:
        gid = str(g.get("game_id", ""))
        if gid in target_ids:
            away = normalize_team(str(g.get("away_team", "")))
            home = normalize_team(str(g.get("home_team", "")))
            if away:
                teams.add(away)
            if home:
                teams.add(home)

    return frozenset(teams) if teams else DEFAULT_PRIMETIME_TEAMS


def allowed_positions_for_slot(slot_kind: str) -> frozenset[str]:
    if slot_kind == "Flex":
        return FLEX_POSITIONS
    if slot_kind == "Superflex":
        return SUPERFLEX_POSITIONS
    return frozenset({slot_kind})


def validate_lineup(
    lineup: PlannerLineup,
    contest: ContestConfig,
    card_usage_across_plan: Mapping[str, Sequence[str]] | None = None,
    primetime_teams: frozenset[str] | Sequence[str] | None = None,
) -> tuple[bool, list[str]]:
    """Validate a single lineup for slot requirements, eligibility, salary bounds, duplicate athlete, cross-lineup copy reuse, collection rules, and weekly primetime game eligibility."""
    errors: list[str] = []
    card_usage = card_usage_across_plan or {}

    # Check required slot kinds against contest specification
    required_kinds = list(contest.slots)
    actual_kinds = [s.slot_kind for s in lineup.slots]
    if sorted(actual_kinds) != sorted(required_kinds):
        errors.append(
            f"Lineup slots {actual_kinds} do not match contest '{contest.name}' required slots {required_kinds}."
        )

    # Check complete slots and card eligibility
    assigned_cards: list[PlannerCard] = []
    for slot in lineup.slots:
        if slot.card is None:
            errors.append(f"Slot '{slot.slot_name}' is empty.")
        else:
            assigned_cards.append(slot.card)
            if not slot.card.is_eligible:
                reason = slot.card.ineligibility_reason or "card is ineligible"
                errors.append(f"Slot '{slot.slot_name}' has ineligible card '{slot.card.card.player_name}': {reason}.")
            allowed = allowed_positions_for_slot(slot.slot_kind)
            if slot.card.card.position not in allowed:
                errors.append(
                    f"Slot '{slot.slot_name}' requires {slot.slot_kind}, but has {slot.card.card.player_name} ({slot.card.card.position})."
                )

    # Check duplicate athletes within lineup
    seen_athletes: set[str] = set()
    for card in assigned_cards:
        if card.card.athlete_key in seen_athletes:
            errors.append(f"Duplicate athlete in lineup: '{card.card.player_name}' is assigned more than once.")
        seen_athletes.add(card.card.athlete_key)

    # Check cross-lineup card reuse across plan
    for card in assigned_cards:
        raw_usage = card_usage.get(card.card.card_id)
        if raw_usage is not None:
            if isinstance(raw_usage, (str, bytes)):
                if raw_usage != lineup.lineup_id:
                    errors.append(
                        f"Card copy '{card.card.player_name}' (ID: {card.card.card_id}) is already used in {raw_usage}."
                    )
            else:
                other_lineups = [l_id for l_id in raw_usage if l_id != lineup.lineup_id]
                if other_lineups:
                    errors.append(
                        f"Card copy '{card.card.player_name}' (ID: {card.card.card_id}) is already used in: {', '.join(other_lineups)}."
                    )

    # Salary constraints
    total_salary = sum(c.weekly_salary for c in assigned_cards)
    if total_salary > contest.maximum_salary:
        errors.append(f"Salary ${total_salary:,} exceeds maximum cap ${contest.maximum_salary:,}.")
    if total_salary < contest.minimum_salary:
        errors.append(f"Salary ${total_salary:,} is below minimum requirement ${contest.minimum_salary:,}.")

    # Primetime collection rule: Primetime collection cards can only enter Primetime Pyro
    if contest.name != "Primetime Pyro":
        for card in assigned_cards:
            grp = getattr(card.card, "collection_group", "Core")
            if grp == "Primetime":
                errors.append(
                    f"Primetime collection card '{card.card.player_name}' can only enter Primetime Pyro."
                )

    # Note: Weekly game eligibility restrictions for Primetime Pyro have been relaxed.


    # Collection constraints (enforced if contest defines requirements and roster cards have collection metadata)
    if contest.collection_requirements:
        has_collection_meta = any(bool(getattr(c.card, "collection", "")) for c in assigned_cards)
        if has_collection_meta:
            counts: dict[str, int] = {}
            for card in assigned_cards:
                grp = getattr(card.card, "collection_group", "Core")
                counts[grp] = counts.get(grp, 0) + 1
            for req_group, (min_c, max_c) in contest.collection_requirements.items():
                actual_cnt = counts.get(req_group, 0)
                if min_c is not None and actual_cnt < min_c:
                    errors.append(
                        f"Contest '{contest.name}' requires at least {min_c} {req_group} cards, but lineup has {actual_cnt}."
                    )
                if max_c is not None and actual_cnt > max_c:
                    errors.append(
                        f"Contest '{contest.name}' allows at most {max_c} {req_group} cards, but lineup has {actual_cnt}."
                    )

    is_valid = len(errors) == 0
    return is_valid, errors


def recalculate_lineup(
    lineup: PlannerLineup,
    contest: ContestConfig,
    card_usage_across_plan: Mapping[str, Sequence[str]] | None = None,
    primetime_teams: frozenset[str] | Sequence[str] | None = None,
) -> None:
    """Recalculate salary, projection, validity, and tier payout for a lineup."""
    assigned = [s.card for s in lineup.slots if s.card is not None]
    lineup.total_salary = sum(c.weekly_salary for c in assigned)
    lineup.remaining_salary = contest.maximum_salary - lineup.total_salary
    lineup.total_projection = round(sum(c.adjusted_projection for c in assigned), 2)

    is_valid, errors = validate_lineup(
        lineup,
        contest,
        card_usage_across_plan=card_usage_across_plan,
        primetime_teams=primetime_teams,
    )
    lineup.is_valid = is_valid
    lineup.validation_errors = errors

    # Deterministic tier evaluation
    eval_result = evaluate_deterministic_tier(lineup.total_projection, contest)
    lineup.estimated_tier = eval_result.achieved_tier
    lineup.estimated_payout = eval_result.estimated_payout
    lineup.estimate_type = eval_result.estimate_type


def check_inventory_feasibility(
    eligible_cards: Sequence[PlannerCard],
    contests: Mapping[str, ContestConfig],
    target_lineups: int,
) -> dict[str, Any]:
    """Check how many lineups the card inventory can physically support."""
    pos_counts: dict[str, int] = {}
    for c in eligible_cards:
        if c.is_eligible:
            pos_counts[c.card.position] = pos_counts.get(c.card.position, 0) + 1

    qb_count = pos_counts.get("QB", 0)
    rb_count = pos_counts.get("RB", 0)
    wr_count = pos_counts.get("WR", 0)
    te_count = pos_counts.get("TE", 0)

    # Flex-only contests do not require a QB. This is an upper bound, not a feasibility proof.
    min_slots = min((len(c.slots) for c in contests.values()), default=1)
    max_lineups = min(target_lineups, len(eligible_cards) // min_slots)
    bottlenecks: list[str] = []
    if contests and all("QB" in c.slots for c in contests.values()):
        max_lineups = min(max_lineups, qb_count)
    if contests and all("QB" in c.slots for c in contests.values()) and qb_count < target_lineups:
        bottlenecks.append(f"QB inventory ({qb_count} cards) limits max lineups to {qb_count}.")

    return {
        "qb_count": qb_count,
        "rb_count": rb_count,
        "wr_count": wr_count,
        "te_count": te_count,
        "requested_target": target_lineups,
        "max_supportable_lineups": max_lineups,
        "bottlenecks": bottlenecks,
    }


def compute_contest_benchmark(
    eligible_cards: Sequence[PlannerCard],
    contest: ContestConfig,
    time_limit: float = 5.0,
    locked_cards: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Find the strongest projected legal lineup for a contest using the full eligible owned pool.

    Objective: Multiplier-adjusted points, with salary only breaking equal-score ties.
    Returns:
        dict containing 'benchmark_score', 'salary', 'cards', and 'status' ('proven_optimal' or 'best_found').
    """
    pool = [c for c in eligible_cards if c.is_eligible]
    # Filter pool based on Primetime rules:
    # 1. Primetime collection cards can only enter Primetime Pyro
    # 2. For Primetime Pyro, all cards must play in designated primetime games
    if contest.name != "Primetime Pyro":
        pool = [c for c in pool if getattr(c.card, "collection_group", "Core") != "Primetime"]


    if not pool:
        return {
            "contest_name": contest.name,
            "benchmark_score": 0.0,
            "salary": 0,
            "status": "infeasible",
            "cards": [],
        }

    seen_kinds: dict[str, int] = {}
    slots: list[tuple[str, str]] = []
    for kind in contest.slots:
        seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
        label = kind if seen_kinds[kind] == 1 else f"{kind}{seen_kinds[kind]}"
        slots.append((label, kind))

    clean_name = re.sub(r"[^a-zA-Z0-9_]", "_", f"Bench_{contest.name}")
    model = pulp.LpProblem(clean_name, pulp.LpMaximize)

    card_dict = {c.card.card_id: c for c in pool}
    x: dict[tuple[str, str], pulp.LpVariable] = {}
    for sname, skind in slots:
        allowed = allowed_positions_for_slot(skind)
        for cid, pcard in card_dict.items():
            if pcard.card.position in allowed:
                x[(cid, sname)] = pulp.LpVariable(f"bx_{cid}_{sname}", cat=pulp.LpBinary)

    for sname, _ in slots:
        slot_vars = [x[(cid, sname)] for cid in card_dict if (cid, sname) in x]
        if not slot_vars:
            return {
                "contest_name": contest.name,
                "benchmark_score": 0.0,
                "salary": 0,
                "status": "infeasible",
                "cards": [],
            }
        model += pulp.lpSum(slot_vars) == 1, f"bslot_{sname}"

    for sname, cid in (locked_cards or {}).items():
        if (cid, sname) not in x:
            raise ValueError(f"Locked card {cid} is unavailable for {sname}.")
        model += x[(cid, sname)] == 1

    for cid in card_dict:
        copy_vars = [x[(cid, sname)] for sname, _ in slots if (cid, sname) in x]
        if copy_vars:
            model += pulp.lpSum(copy_vars) <= 1, f"bcopy_{cid}"

    cards_by_athlete: dict[str, list[str]] = {}
    for cid, pcard in card_dict.items():
        cards_by_athlete.setdefault(pcard.card.athlete_key, []).append(cid)
    for akey, cids in cards_by_athlete.items():
        ath_vars = [x[(cid, sname)] for cid in cids for sname, _ in slots if (cid, sname) in x]
        if ath_vars:
            model += pulp.lpSum(ath_vars) <= 1, f"bath_{akey}"

    sal_expr = pulp.lpSum(
        card_dict[cid].weekly_salary * x[(cid, sname)]
        for sname, _ in slots
        for cid in card_dict
        if (cid, sname) in x
    )
    model += sal_expr <= contest.maximum_salary, "bsal_max"
    model += sal_expr >= contest.minimum_salary, "bsal_min"

    if contest.collection_requirements:
        has_col = any(bool(c.card.collection) for c in card_dict.values())
        if has_col:
            for cgroup, (min_c, max_c) in contest.collection_requirements.items():
                gvars = [
                    x[(cid, sname)]
                    for sname, _ in slots
                    for cid, c in card_dict.items()
                    if (cid, sname) in x and c.card.collection_group == cgroup
                ]
                if gvars:
                    if min_c is not None and min_c > 0:
                        model += pulp.lpSum(gvars) >= min_c, f"bcol_min_{cgroup}"
                    if max_c is not None:
                        model += pulp.lpSum(gvars) <= max_c, f"bcol_max_{cgroup}"
                elif min_c is not None and min_c > 0:
                    return {
                        "contest_name": contest.name,
                        "benchmark_score": 0.0,
                        "salary": 0,
                        "status": "infeasible",
                        "cards": [],
                    }

    score_expr = pulp.lpSum(
        card_dict[cid].adjusted_projection * x[(cid, sname)]
        for sname, _ in slots
        for cid in card_dict
        if (cid, sname) in x
    )

    # Multiplier-adjusted points determine strength; salary only breaks score ties.
    # Stage 1: Strictly maximize points (no salary in objective).
    model += score_expr

    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=time_limit)
    model.solve(solver)

    if model.sol_status not in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
        return {
            "contest_name": contest.name,
            "benchmark_score": 0.0,
            "salary": 0,
            "status": "infeasible" if model.status == pulp.LpStatusInfeasible else "time_limit",
            "cards": [],
        }

    status = "proven_optimal" if model.sol_status == pulp.LpSolutionOptimal else "best_found"
    opt_score_val = float(pulp.value(score_expr) or 0.0)

    # Snapshot Stage 1 integer solution
    stage1_x = {k: float(v.varValue or 0.0) for k, v in x.items() if v.varValue is not None}
    stage1_score = opt_score_val
    stage1_sal = int(round(float(pulp.value(sal_expr) or 0)))

    # Stage 2: Minimize salary subject to achieving the exact optimal score found in Stage 1.
    # This guarantees points strictly outrank salary without numerical tradeoffs.
    model += score_expr >= opt_score_val - 1e-5, "b_opt_score_floor"
    model.setObjective(sal_expr)
    model.sense = pulp.LpMinimize
    # Stage 2 solve with tight time limit
    stage2_solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=min(2.0, time_limit))
    model.solve(stage2_solver)

    stage2_success = model.sol_status in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible)

    def get_bench_x(cid: str, sname: str) -> float:
        if stage2_success:
            var = x.get((cid, sname))
            if var is not None and pulp.value(var) is not None:
                return float(pulp.value(var))
        return stage1_x.get((cid, sname), 0.0)

    chosen_cids: list[str] = []
    for sname, _ in slots:
        for cid in card_dict:
            if get_bench_x(cid, sname) > 0.5:
                chosen_cids.append(cid)
                break

    if stage2_success:
        bench_score = round(float(pulp.value(score_expr) or opt_score_val), 2)
        bench_sal = int(round(float(pulp.value(sal_expr) or stage1_sal)))
    else:
        bench_score = round(stage1_score, 2)
        bench_sal = stage1_sal

    return {
        "contest_name": contest.name,
        "benchmark_score": bench_score,
        "salary": bench_sal,
        "status": status,
        "cards": chosen_cids,
    }


def compute_all_contest_benchmarks(
    eligible_cards: Sequence[PlannerCard],
    contests: Mapping[str, ContestConfig],
    time_limit_per_contest: float = 5.0,
) -> dict[str, dict[str, Any]]:
    """Compute single-lineup benchmarks across all active contests using full inventory."""
    results: dict[str, dict[str, Any]] = {}
    for cname, cfg in contests.items():
        if cfg.enabled and cfg.eligible:
            results[cname] = compute_contest_benchmark(eligible_cards, cfg, time_limit=time_limit_per_contest)
    return results


def generate_candidate_portfolio_distributions(
    contests: Mapping[str, ContestConfig],
    eligible_cards: Sequence[PlannerCard],
    max_target: int,
    benchmarks: Mapping[str, Mapping[str, Any]] | None = None,
    max_candidates: int = 40,
) -> list[dict[str, int]]:
    """Dynamically derive feasible candidate portfolio mixes from enabled contests, limits, and inventory.

    Replaces static hardcoded contest mix lists. Combinations are filtered by inventory feasibility
    (QBs and slot capacity) and ranked by benchmark score potential.
    """
    valid_contests = [
        cname for cname, cfg in contests.items()
        if cfg.enabled and cfg.eligible and (benchmarks is None or benchmarks.get(cname, {}).get("benchmark_score", 0) > 0)
    ]
    if not valid_contests or max_target <= 0:
        return []

    pos_counts: dict[str, int] = {}
    primetime_card_count = 0
    primetime_game_card_count = 0
    pt_teams = get_primetime_eligible_teams()

    for c in eligible_cards:
        if c.is_eligible:
            pos_counts[c.card.position] = pos_counts.get(c.card.position, 0) + 1
            if getattr(c.card, "collection_group", "Core") == "Primetime":
                primetime_card_count += 1
            if normalize_team(c.card.team) in pt_teams:
                primetime_game_card_count += 1

    qb_count = pos_counts.get("QB", 0)

    contest_limits = {
        cname: min(contests[cname].default_entry_limit or 5, max_target)
        for cname in valid_contests
    }

    scored_contests = sorted(
        valid_contests,
        key=lambda c: benchmarks.get(c, {}).get("benchmark_score", 0) if benchmarks else 0,
        reverse=True,
    )

    candidates: list[dict[str, int]] = []
    seen: set[tuple[tuple[str, int], ...]] = set()

    for count in range(max_target, 0, -1):
        count_candidates: list[tuple[dict[str, int], float]] = []
        ranges = [range(min(count, contest_limits[c]) + 1) for c in scored_contests]
        for combo in itertools.product(*ranges):
            if sum(combo) != count:
                continue
            dist = {c: n for c, n in zip(scored_contests, combo) if n > 0}
            needed_qbs = sum(cfg.slots.count("QB") * dist.get(cname, 0) for cname, cfg in contests.items())
            if needed_qbs > qb_count:
                continue
            needed_slots = sum(len(cfg.slots) * dist.get(cname, 0) for cname, cfg in contests.items())
            if needed_slots > len(eligible_cards):
                continue


            pot_score = (
                sum(benchmarks.get(c, {}).get("benchmark_score", 100.0) * n for c, n in dist.items())
                if benchmarks
                else 0.0
            )
            frozen = tuple(sorted(dist.items()))
            if frozen not in seen:
                seen.add(frozen)
                count_candidates.append((dist, pot_score))

        count_candidates.sort(key=lambda item: item[1], reverse=True)
        candidates.extend([d for d, _ in count_candidates[:10]])
        if len(candidates) >= max_candidates:
            break

    return candidates[:max_candidates]


def assess_unused_salary(
    lineup: PlannerLineup,
    contest: ContestConfig,
    available_cards: Sequence[PlannerCard],
    card_usage_across_plan: Mapping[str, Sequence[str]] | None = None,
) -> tuple[bool, str]:
    """Flag substantial unused salary (>10% of cap) and check if a legal replacement would improve points.

    Uses full validate_lineup to enforce athlete uniqueness, card copy reuse, collection constraints,
    position eligibility, slot locks, and salary bounds.
    Does not force a higher-priced, lower-scoring player into a lineup.
    """
    threshold = 0.10 * contest.maximum_salary
    if lineup.remaining_salary <= threshold:
        return False, ""

    best_improvement = 0.0
    improving_swap_note = ""

    for slot_idx, slot in enumerate(lineup.slots):
        if slot.is_locked or not slot.card:
            continue
        cur_card = slot.card
        for cand in available_cards:
            if not cand.is_eligible or cand.card.card_id == cur_card.card.card_id:
                continue

            pt_diff = cand.adjusted_projection - cur_card.adjusted_projection
            if pt_diff <= best_improvement or pt_diff <= 0.1:
                continue

            # Simulate replacing slot.card with cand in a temporary lineup
            sim_slots: list[LineupSlotAssignment] = []
            for s in lineup.slots:
                if s.slot_name == slot.slot_name:
                    sim_slots.append(
                        LineupSlotAssignment(
                            slot_name=s.slot_name,
                            slot_kind=s.slot_kind,
                            card=cand,
                            is_locked=s.is_locked,
                        )
                    )
                else:
                    sim_slots.append(
                        LineupSlotAssignment(
                            slot_name=s.slot_name,
                            slot_kind=s.slot_kind,
                            card=s.card,
                            is_locked=s.is_locked,
                        )
                    )

            sim_lineup = PlannerLineup(
                lineup_id=lineup.lineup_id,
                contest_name=lineup.contest_name,
                slots=sim_slots,
                is_locked=lineup.is_locked,
                contest_benchmark=lineup.contest_benchmark,
            )

            # Build simulated card usage across plan for the candidate swap
            sim_usage: dict[str, list[str]] = {}
            if card_usage_across_plan:
                for cid, lids in card_usage_across_plan.items():
                    sim_usage[cid] = [lid for lid in lids if lid != lineup.lineup_id]
            for s in sim_slots:
                if s.card:
                    sim_usage.setdefault(s.card.card.card_id, []).append(lineup.lineup_id)

            # Validate using full lineup validator
            is_valid, _ = validate_lineup(sim_lineup, contest, sim_usage)
            if not is_valid:
                continue

            sal_diff = cand.weekly_salary - cur_card.weekly_salary
            best_improvement = pt_diff
            improving_swap_note = (
                f"Legal upgrade available: {cand.card.player_name} (+{pt_diff:.1f} pts, +${sal_diff:,})"
            )

    pct_unused = (lineup.remaining_salary / contest.maximum_salary) * 100
    if best_improvement > 0.1:
        msg = f"Substantial unused salary: ${lineup.remaining_salary:,} ({pct_unused:.1f}% of cap). {improving_swap_note}."
    else:
        msg = (
            f"Substantial unused salary: ${lineup.remaining_salary:,} ({pct_unused:.1f}% of cap). "
            f"No higher-scoring replacement fits; lineup retains strong value."
        )
    return True, msg


def is_plan_fully_qualifying(
    plan: WeeklyPlan | None,
    cards: Sequence[PlannerCard],
    contests: Mapping[str, ContestConfig],
    excluded_athlete_keys: set[str],
    computed_benchmarks: Mapping[str, Any],
    quality_threshold: float,
    primetime_teams: frozenset[str] | None = None,
) -> tuple[bool, str]:
    """Check if an existing plan genuinely qualifies under current roster, rules, exclusions, benchmarks, and quality."""
    if plan is None or not plan.lineups:
        return False, "Plan contains no lineups."

    # 1. Roster availability & copy constraints
    card_map = {c.card.card_id: c for c in cards if c.is_eligible}
    used_card_ids: set[str] = set()
    athlete_counts: dict[str, int] = {}
    owned_athlete_counts: dict[str, int] = {}
    for c in cards:
        if c.is_eligible:
            akey = c.card.athlete_key
            owned_athlete_counts[akey] = owned_athlete_counts.get(akey, 0) + 1

    for l in plan.lineups:
        for s in l.slots:
            if s.card is None:
                return False, f"Lineup '{l.lineup_id}' contains an empty slot '{s.slot_name}'."
            cid = s.card.card.card_id
            if cid not in card_map:
                return False, f"Card '{cid}' is not in the current eligible card pool."
            if cid in used_card_ids:
                return False, f"Card copy '{cid}' is used more than once across the plan."
            used_card_ids.add(cid)
            akey = s.card.card.athlete_key
            athlete_counts[akey] = athlete_counts.get(akey, 0) + 1
            if athlete_counts[akey] > owned_athlete_counts.get(akey, 0):
                return False, f"Athlete '{akey}' exceeds owned copy count ({athlete_counts[akey]} > {owned_athlete_counts.get(akey, 0)})."

    # 2. Exclusions
    for cid in used_card_ids:
        card = card_map[cid]
        if card.card.athlete_key in excluded_athlete_keys:
            return False, f"Athlete '{card.card.athlete_key}' is in the current exclusion list."

    # 3. Contest rules & entry limits
    contest_counts: dict[str, int] = {}
    card_usage_map: dict[str, list[str]] = {}
    for l in plan.lineups:
        for s in l.slots:
            if s.card is not None:
                card_usage_map.setdefault(s.card.card.card_id, []).append(l.lineup_id)

    for l in plan.lineups:
        cname = l.contest_name
        if cname not in contests:
            return False, f"Contest '{cname}' is not currently enabled."
        cfg = contests[cname]
        contest_counts[cname] = contest_counts.get(cname, 0) + 1
        if contest_counts[cname] > cfg.default_entry_limit:
            return False, f"Contest '{cname}' exceeds entry limit ({contest_counts[cname]} > {cfg.default_entry_limit})."

        is_valid, errors = validate_lineup(l, cfg, card_usage_map, primetime_teams=primetime_teams)
        if not is_valid:
            return False, f"Lineup '{l.lineup_id}' violates contest rules: {errors}"

    # 4. Benchmarks & quality requirement
    for l in plan.lineups if quality_threshold > 0 else []:
        cname = l.contest_name
        if cname not in computed_benchmarks:
            return False, f"Contest '{cname}' has no benchmark score."
        bench_score = float(computed_benchmarks[cname].get("benchmark_score", 0.0))
        if bench_score <= 0:
            return False, f"Contest '{cname}' benchmark score is invalid ({bench_score})."
        min_required = (bench_score * quality_threshold) - 1e-4
        if l.total_projection < min_required:
            return False, (
                f"Lineup '{l.lineup_id}' projected score ({l.total_projection:.2f}) "
                f"is below current {int(quality_threshold * 100)}% quality requirement "
                f"({min_required:.2f} pts from benchmark {bench_score:.2f})."
            )

    return True, "Plan satisfies all current rules, roster, exclusions, benchmarks, and quality requirements."


def solve_multi_lineup_allocation(
    cards: Sequence[PlannerCard],
    contests: Mapping[str, ContestConfig],
    target_count: int | None = None,
    contest_distribution: Mapping[str, int] | None = None,
    existing_plan: WeeklyPlan | None = None,
    excluded_athlete_keys: Sequence[str] | None = None,
    slate_id: str = "current_slate",
    salary_source: str = "roster",
    _time_limit: float = 20,
    quality_threshold: float = 0.0,
    contest_benchmarks: Mapping[str, dict[str, Any]] | None = None,
) -> WeeklyPlan:
    """Solve multi-lineup allocation across the weekly portfolio using PuLP MIP.

    Respects:
    - Fixed single-lineup contest benchmarks computed on the full eligible pool.
    - Quality requirement: Each active lineup must meet quality_threshold * contest_benchmark.
    - Available card copies (each copy used at most once across all lineups).
    - Athlete uniqueness within each lineup.
    - Slot position requirements.
    - Minimum and maximum contest salary limits.
    - Contest entry limits.
    - Locked lineups and locked slots.
    """
    excluded_set = set(excluded_athlete_keys or [])
    eligible_pool = [c for c in cards if c.is_eligible and c.card.athlete_key not in excluded_set]

    # Enabled contests
    enabled_contests = {k: v for k, v in contests.items() if v.enabled and v.eligible}
    if not enabled_contests:
        raise ValueError("No enabled and eligible contests available for optimization")

    if not contest_distribution and "Volcano" in enabled_contests and "Wildfire" in enabled_contests:
        del enabled_contests["Wildfire"]

    if target_count is None:
        target_count = sum(contest_distribution.values()) if contest_distribution else sum(c.default_entry_limit or 5 for c in enabled_contests.values())
    if isinstance(target_count, bool) or not isinstance(target_count, int) or target_count < 0:
        raise ValueError("Lineup limit must be a non-negative integer.")

    # Check inventory bottlenecks across eligible pool
    inventory_info = check_inventory_feasibility(eligible_pool, enabled_contests, target_count)
    max_supportable = inventory_info["max_supportable_lineups"]

    # Candidate lineup generation up to contest limits or specified distribution
    # If contest_distribution is provided, user wants explicit caps per contest (bounded by default_entry_limit)
    # If not provided, solver dynamically chooses the most profitable contest mix up to target_count
    active_contests = dict(enabled_contests)
    if not contest_distribution and "Volcano" in active_contests and "Wildfire" in active_contests:
        del active_contests["Wildfire"]

    candidate_specs: list[tuple[str, str, list[tuple[str, str]]]] = []
    contest_candidate_lids: dict[str, list[str]] = {cname: [] for cname in active_contests}

    for cname, cfg in active_contests.items():
        limit = cfg.default_entry_limit or 5
        if contest_distribution:
            # Respect user specified count capped strictly at contest limit
            requested = contest_distribution.get(cname, 0)
            count = min(requested, limit)
        else:
            # Auto-mix mode: allocate candidate slots up to limit
            count = limit

        seen_kinds: dict[str, int] = {}
        slot_tuples: list[tuple[str, str]] = []
        for kind in cfg.slots:
            seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
            label = kind if seen_kinds[kind] == 1 else f"{kind}{seen_kinds[kind]}"
            slot_tuples.append((label, kind))

        c_slug = re.sub(r"[^a-z0-9]", "", cname.lower())
        for idx in range(1, count + 1):
            lid = f"{c_slug}_{idx}"
            candidate_specs.append((lid, cname, slot_tuples))
            contest_candidate_lids[cname].append(lid)

    if not candidate_specs:
        now = datetime.now(timezone.utc).isoformat()
        return WeeklyPlan(
            plan_id="empty_plan",
            name="Empty Plan",
            created_at=now,
            updated_at=now,
            slate_id="unknown",
            salary_source="roster",
            contests=dict(contests),
            lineups=[],
            roster_cards=list(cards),
            projection_overrides={},
            excluded_athlete_keys=list(excluded_set),
            inventory_summary=inventory_info,
        )

    # Check for locked cards from existing_plan
    # Support matching by exact lid or positional contest index for backward compatibility
    locked_assignments: dict[tuple[str, str], PlannerCard] = {}
    locked_lineup_ids: set[str] = set()
    if existing_plan:
        existing_contest_counts: dict[str, int] = {}
        for l in existing_plan.lineups:
            c_idx = existing_contest_counts.get(l.contest_name, 0) + 1
            existing_contest_counts[l.contest_name] = c_idx
            c_slug = re.sub(r"[^a-z0-9]", "", l.contest_name.lower())
            canonical_lid = f"{c_slug}_{c_idx}"

            is_l_locked = l.is_locked
            if is_l_locked:
                locked_lineup_ids.add(l.lineup_id)
                locked_lineup_ids.add(canonical_lid)

            for s in l.slots:
                if (s.is_locked or is_l_locked) and s.card is not None:
                    locked_assignments[(l.lineup_id, s.slot_name)] = s.card
                    locked_assignments[(canonical_lid, s.slot_name)] = s.card

    # PuLP Optimization Model
    model = pulp.LpProblem("GameBlazers_Weekly_Allocation", pulp.LpMaximize)

    # Decision variables:
    # z[lid]: Binary activation variable indicating if candidate lineup lid is selected
    z: dict[str, pulp.LpVariable] = {}
    for lid, _, _ in candidate_specs:
        z[lid] = pulp.LpVariable(f"z_{lid}", cat=pulp.LpBinary)

    # Decision variables: x[card_id, lineup_id, slot_name]
    x: dict[tuple[str, str, str], pulp.LpVariable] = {}
    card_dict = {c.card.card_id: c for c in eligible_pool}

    # Also make sure locked cards from existing plan are in card_dict
    for (lid, sname), pcard in locked_assignments.items():
        card_dict[pcard.card.card_id] = pcard

    pt_teams = get_primetime_eligible_teams()
    for lid, cname, slots in candidate_specs:
        cfg = enabled_contests[cname]
        for sname, skind in slots:
            allowed = allowed_positions_for_slot(skind)
            for cid, pcard in card_dict.items():
                if pcard.card.position not in allowed:
                    continue
                # Primetime collection rule: Primetime collection cards can only enter Primetime Pyro
                is_pt_collection = getattr(pcard.card, "collection_group", "Core") == "Primetime"
                if is_pt_collection and cname != "Primetime Pyro":
                    continue
                var = pulp.LpVariable(f"x_{cid}_{lid}_{sname}", cat=pulp.LpBinary)

                x[(cid, lid, sname)] = var

    # Constraint 1: Lineup slot fill coupled to activation z[lid]
    # Each slot in lineup lid must have exactly 1 card if lid is active, else 0
    for lid, cname, slots in candidate_specs:
        for sname, _ in slots:
            slot_vars = [x[(cid, l_id, s_name)] for (cid, l_id, s_name) in x if l_id == lid and s_name == sname]
            model += pulp.lpSum(slot_vars) == z[lid], f"slot_fill_{lid}_{sname}"

    # Constraint 2: Each card copy used AT MOST ONCE across all active lineups
    for cid in card_dict:
        copy_vars = [x[(c_id, lid, sname)] for (c_id, lid, sname) in x if c_id == cid]
        if copy_vars:
            model += pulp.lpSum(copy_vars) <= 1, f"card_unique_{cid}"

    # Constraint 3: No duplicate athlete within the SAME lineup
    cards_by_athlete: dict[str, list[str]] = {}
    for cid, pcard in card_dict.items():
        cards_by_athlete.setdefault(pcard.card.athlete_key, []).append(cid)

    for lid, _, slots in candidate_specs:
        for akey, cids in cards_by_athlete.items():
            athlete_lineup_vars = [
                x[(cid, l_id, sname)]
                for cid in cids
                for (c_id, l_id, sname) in x
                if c_id == cid and l_id == lid
            ]
            if athlete_lineup_vars:
                model += pulp.lpSum(athlete_lineup_vars) <= z[lid], f"athlete_unique_{lid}_{akey}"

    # Constraint 4: Salary bounds per lineup coupled to activation
    for lid, cname, slots in candidate_specs:
        cfg = enabled_contests[cname]
        lineup_salary_expr = pulp.lpSum(
            card_dict[cid].weekly_salary * x[(cid, l_id, sname)]
            for (cid, l_id, sname) in x
            if l_id == lid
        )
        model += lineup_salary_expr <= cfg.maximum_salary * z[lid], f"salary_max_{lid}"
        model += lineup_salary_expr >= cfg.minimum_salary * z[lid], f"salary_min_{lid}"

    # Constraint 4b: Collection requirements per lineup (enforced when cards provide collection metadata)
    has_collection_metadata = any(bool(pcard.card.collection) for pcard in card_dict.values())
    if has_collection_metadata:
        for lid, cname, slots in candidate_specs:
            cfg = enabled_contests[cname]
            if cfg.collection_requirements:
                for col_name, (min_c, max_c) in cfg.collection_requirements.items():
                    col_vars = [
                        x[(cid, l_id, sname)]
                        for (cid, l_id, sname) in x
                        if l_id == lid and card_dict[cid].card.collection_group == col_name
                    ]
                    if col_vars:
                        if min_c is not None and min_c > 0:
                            model += pulp.lpSum(col_vars) >= min_c * z[lid], f"col_min_{lid}_{col_name}"
                        if max_c is not None:
                            model += pulp.lpSum(col_vars) <= max_c * z[lid], f"col_max_{lid}_{col_name}"
                    elif min_c is not None and min_c > 0:
                        model += z[lid] == 0, f"col_infeasible_{lid}_{col_name}"

    # Constraint 5: Pre-existing locked card assignments force activation
    activated_lids: set[str] = set()
    for (lid, sname), pcard in locked_assignments.items():
        var_key = (pcard.card.card_id, lid, sname)
        if var_key in x:
            model += x[var_key] == 1, f"lock_{lid}_{sname}_{pcard.card.card_id}"
            if lid in z and lid not in activated_lids:
                # One z constraint per lineup: several locked slots must not repeat the name.
                model += z[lid] == 1, f"lock_z_{lid}"
                activated_lids.add(lid)

    # Symmetry breaking for candidate lineups within each contest:
    # Fill candidate 1 before candidate 2, candidate 2 before candidate 3, etc.
    for cname, lids in contest_candidate_lids.items():
        for i in range(len(lids) - 1):
            model += z[lids[i]] >= z[lids[i + 1]], f"sym_break_{lids[i]}_{lids[i+1]}"

    # Constraint 6: Total active lineups and contest distribution
    total_candidates = len(candidate_specs)
    effective_target = min(target_count, total_candidates, max_supportable)

    if contest_distribution:
        # Explicit contest distribution requested by user: enforce exact or supportable counts
        for cname, lids in contest_candidate_lids.items():
            limit = enabled_contests[cname].default_entry_limit or 5
            requested = contest_distribution.get(cname, 0)
            target_for_c = min(requested, limit)
            if target_for_c > 0 and lids:
                # Cap if inventory is smaller
                max_c = min(target_for_c, len(lids), effective_target)
                model += pulp.lpSum(z[lid] for lid in lids) <= max_c, f"contest_count_{cname}"
            else:
                for lid in lids:
                    model += z[lid] == 0, f"contest_zero_{lid}"
        model += pulp.lpSum(z[lid] for lid in z) <= effective_target, "total_target_cap"
    else:
        # Dynamic contest mix selection:
        # Solve for the mathematically optimal contest mix yielding highest payout up to target_count
        model += pulp.lpSum(z.values()) <= effective_target, "dynamic_mix_target_cap"

    # Fixed contest benchmarks: compute on full eligible pool if not passed in
    if contest_benchmarks is None:
        computed_benchmarks = compute_all_contest_benchmarks(eligible_pool, enabled_contests, time_limit_per_contest=3.0)
    else:
        computed_benchmarks = dict(contest_benchmarks)

    # Benchmark score lookup per contest: only include contests with genuine computed benchmarks
    bench_scores: dict[str, float] = {}
    for cname, cfg in enabled_contests.items():
        b_info = computed_benchmarks.get(cname)
        if b_info and b_info.get("status") in ("proven_optimal", "best_found") and float(b_info.get("benchmark_score", 0.0)) > 0.0:
            bench_scores[cname] = float(b_info["benchmark_score"])

    # Constraint 4c: Lineup Quality Requirement
    # Require each active initial lineup to reach at least quality_threshold of its contest benchmark.
    # If a lineup is locked or contains locked slots from an existing plan, allow it to remain active even if below threshold.
    has_locks_lid: set[str] = set(locked_lineup_ids)
    for (lid, sname) in locked_assignments:
        has_locks_lid.add(lid)

    for lid, cname, slots in candidate_specs:
        if lid not in has_locks_lid:
            cfg = enabled_contests[cname]
            c_bench = bench_scores.get(cname)
            if quality_threshold > 0 and (c_bench is None or c_bench <= 0.0):
                # Without a valid positive benchmark, this contest cannot legally form an initial lineup meeting quality requirements
                model += z[lid] == 0, f"no_bench_zero_{lid}"
            elif quality_threshold > 0.0:
                req_score = round(quality_threshold * c_bench, 4)
                lineup_score_expr = pulp.lpSum(
                    card_dict[cid].adjusted_projection * x[(cid, l_id, sname)]
                    for (cid, l_id, sname) in x
                    if l_id == lid
                )
                model += lineup_score_expr >= req_score * z[lid], f"quality_req_{lid}"

    # Count first, then projected points. Historical payouts never select cards.
    total_score = pulp.lpSum(card_dict[cid].adjusted_projection * var
                             for (cid, lid, sname), var in x.items())
    if quality_threshold > 0:
        contest_by_lid = {lid: cname for lid, cname, _ in candidate_specs}
        relative_score_objective = pulp.lpSum(
            card_dict[cid].adjusted_projection / bench_scores.get(contest_by_lid[lid], 1.0) * var
            for (cid, lid, sname), var in x.items())
    else:
        relative_score_objective = total_score
    total_salary_expr = pulp.lpSum(card_dict[cid].weekly_salary * var
                                  for (cid, lid, sname), var in x.items())
    count_weight = 1 + sum(max(0, c.adjusted_projection) for c in card_dict.values())
    model += count_weight * pulp.lpSum(z.values()) + relative_score_objective

    # Solve Stage 1 with dynamic candidate portfolio discovery and MIP optimization
    # For auto-mix (when contest_distribution is None and no locks exist), dynamically derive candidate portfolio mixes
    # from enabled contests, entry limits, and inventory feasibility, testing them within a practical time budget.
    def plan_rank_key(p: WeeklyPlan) -> tuple[int, float, float]:
        v_lineups = [l for l in p.lineups if l.is_valid]
        cnt = len(v_lineups)
        norm_score = 0.0
        for l in v_lineups:
            if l.contest_benchmark > 0:
                norm_score += l.total_projection / l.contest_benchmark
            else:
                norm_score += 1.0
        tot_sal = sum(l.total_salary for l in v_lineups)
        # Rank by qualifying lineup count, then normalized score, with salary only breaking ties
        score = norm_score if quality_threshold > 0 else sum(l.total_projection for l in v_lineups)
        return (cnt, round(score, 6), -tot_sal)

    portfolio_seed_plan: WeeklyPlan | None = None
    if quality_threshold <= 0 and _time_limit > 1:
        # ponytail: sequential exact lineups seed the global solve; CBC can improve their allocation.
        remaining = dict(card_dict)
        seed_lineups = []
        reserved = {c.card.card_id for c in locked_assignments.values()}
        # Complete locked entries first, then scarce collections and smaller entries.
        ordered_specs = sorted(candidate_specs, key=lambda spec: (
            spec[0] not in has_locks_lid,
            not bool(enabled_contests[spec[1]].collection_requirements),
            len(spec[2]), spec[0]))
        for lid, cname, slots in ordered_specs:
            if len(seed_lineups) >= effective_target:
                break
            fixed = {sname: c.card.card_id for (l_id, sname), c in locked_assignments.items() if l_id == lid}
            pool = [c for cid, c in remaining.items() if cid not in reserved or cid in fixed.values()]
            result = compute_contest_benchmark(pool, enabled_contests[cname], time_limit=1.0, locked_cards=fixed)
            if not result["cards"]:
                if fixed:
                    raise ValueError("A locked lineup cannot be completed with the available cards. Unlock or edit it first.")
                continue
            assignments = [LineupSlotAssignment(sn, sk, remaining[cid], sn in fixed)
                           for (sn, sk), cid in zip(slots, result["cards"])]
            lineup = PlannerLineup(lid, cname, assignments, is_locked=lid in locked_lineup_ids,
                                   contest_benchmark=bench_scores.get(cname, 0))
            recalculate_lineup(lineup, enabled_contests[cname])
            if not lineup.is_valid:
                continue
            seed_lineups.append(lineup)
            for cid in result["cards"]:
                remaining.pop(cid)
        if seed_lineups:
            now = datetime.now(timezone.utc).isoformat()
            portfolio_seed_plan = WeeklyPlan(
                f"plan_{now[:10]}", f"Weekly Plan {now[:10]}", now, now, slate_id, salary_source,
                dict(contests), seed_lineups, list(cards),
                dict(existing_plan.projection_overrides) if existing_plan else {}, list(excluded_set),
                total_weekly_projection=round(sum(l.total_projection for l in seed_lineups), 2),
                inventory_summary=dict(inventory_info), contest_benchmarks=computed_benchmarks,
                quality_threshold=0.0)
            # Reorder seed IDs within each contest to satisfy the solver's symmetry constraints.
            for cname, lids in contest_candidate_lids.items():
                group = [l for l in seed_lineups if l.contest_name == cname]
                if not locked_assignments:
                    for l, lid in zip(group, lids):
                        l.lineup_id = lid
            active_ids = {l.lineup_id for l in seed_lineups}
            chosen = {(s.card.card.card_id, l.lineup_id, s.slot_name)
                      for l in seed_lineups for s in l.slots if s.card}
            for lid, var in z.items():
                var.setInitialValue(int(lid in active_ids))
            for key, var in x.items():
                var.setInitialValue(int(key in chosen))
    if not contest_distribution and not locked_assignments and quality_threshold > 0.0 and _time_limit > 1.0:
        candidate_dists = generate_candidate_portfolio_distributions(
            active_contests,
            eligible_pool,
            max_target=effective_target,
            benchmarks=computed_benchmarks,
            max_candidates=15,
        )
        seed_deadline = time.time() + min(8.0, _time_limit * 0.4)
        for d in candidate_dists:
            if time.time() >= seed_deadline:
                break
            cnt = sum(d.values())
            if cnt > effective_target:
                continue
            if any(c not in active_contests for c in d):
                continue
            if any(c_cnt > (active_contests[c].default_entry_limit or 5) for c, c_cnt in d.items()):
                continue
            try:
                p = solve_multi_lineup_allocation(
                    cards=eligible_pool,
                    contests=active_contests,
                    target_count=cnt,
                    contest_distribution=d,
                    quality_threshold=quality_threshold,
                    contest_benchmarks=computed_benchmarks,
                    _time_limit=2.0,
                )
                if len(p.lineups) == cnt and all(l.is_valid for l in p.lineups):
                    if portfolio_seed_plan is None or plan_rank_key(p) > plan_rank_key(portfolio_seed_plan):
                        portfolio_seed_plan = p
                        if len(p.lineups) >= min(effective_target, 6):
                            break
            except Exception:
                pass

    def finalize_seed_plan(plan: WeeklyPlan) -> WeeklyPlan:
        inv = dict(plan.inventory_summary)
        inv["solver_status"] = "time_limit_feasible"
        inv["lineup_count_proven_maximum"] = False
        assigned_ids = {s.card.card.card_id for l in plan.lineups for s in l.slots if s.card}
        unassigned_count = sum(1 for c in eligible_pool if c.card.card_id not in assigned_ids)
        reasons = [
            f"Generated {len(plan.lineups)} competitive lineups meeting the {int(quality_threshold*100)}% quality requirement before reaching the time limit. This count is the best feasible allocation found, not a proven maximum.",
            f"{unassigned_count} eligible cards remain unassigned. Generation stopped due to the time limit rather than proven inventory exhaustion.",
        ]
        inv["stopping_reasons"] = reasons
        plan.inventory_summary = inv
        return plan

    # Do not short-circuit before running the global MIP solver so that
    # LpSolutionOptimal can be proven when possible. portfolio_seed_plan remains
    # available as a fallback and ranking competitor if the global MIP solver times out.
    solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=_time_limit, warmStart=portfolio_seed_plan is not None)
    try:
        model.solve(solver)
    except Exception:
        pass

    if model.sol_status not in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
        if portfolio_seed_plan and portfolio_seed_plan.lineups:
            return finalize_seed_plan(portfolio_seed_plan)
        qualifies, reason = is_plan_fully_qualifying(
            existing_plan, cards, contests, excluded_set, computed_benchmarks, quality_threshold, primetime_teams=pt_teams
        )
        if qualifies and existing_plan:
            # Preserve existing plan only if it genuinely qualifies under current rules and quality
            preserved = WeeklyPlan(
                plan_id=existing_plan.plan_id,
                name=existing_plan.name,
                created_at=existing_plan.created_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                slate_id=slate_id,
                salary_source=salary_source,
                contests=dict(contests),
                lineups=list(existing_plan.lineups),
                roster_cards=list(cards),
                projection_overrides=dict(existing_plan.projection_overrides),
                excluded_athlete_keys=list(excluded_set),
                total_weekly_projection=existing_plan.total_weekly_projection,
                total_weekly_estimated_payout=existing_plan.total_weekly_estimated_payout,
                inventory_summary=dict(existing_plan.inventory_summary),
                contest_benchmarks=computed_benchmarks,
                quality_threshold=quality_threshold,
            )
            preserved.inventory_summary["solver_status"] = "time_limit_feasible"
            preserved.inventory_summary["lineup_count_proven_maximum"] = False
            return preserved

        status_str = pulp.LpStatus[model.status]
        if contest_distribution:
            raise ValueError("The solver reached its time limit without finding a complete allocation. Your existing plan is unchanged.")
        # Check if any integer feasible lineup was assigned in variables despite overall time limit
        partial_active = any((v.varValue or 0.0) > 0.5 for v in z.values())
        if not partial_active:
            # If solver reached time limit in dynamic auto-mix, record status as time_limit_feasible on empty/partial plan
            inventory_info["solver_status"] = "time_limit_feasible"
            inventory_info["lineup_count_proven_maximum"] = False
            stopping_reasons = [
                f"Generated 0 competitive lineups meeting the {int(quality_threshold*100)}% quality requirement before reaching the time limit. This count is the best feasible allocation found, not a proven maximum.",
                f"{len(eligible_pool)} eligible cards remain unassigned. Generation stopped due to the time limit rather than proven inventory exhaustion.",
            ]
            if existing_plan and existing_plan.lineups:
                inventory_info["preserved_existing_lineups"] = [l.to_dict() for l in existing_plan.lineups]
                inventory_info["preserved_existing_lineups_count"] = len(existing_plan.lineups)
                stopping_reasons.append(f"Preserved {len(existing_plan.lineups)} previous lineup(s) separately because they did not qualify under the current {int(quality_threshold*100)}% quality requirement: {reason}")
            inventory_info["stopping_reasons"] = stopping_reasons
            now_iso = datetime.now(timezone.utc).isoformat()
            return WeeklyPlan(
                plan_id=existing_plan.plan_id if existing_plan else f"plan_{now_iso[:10]}",
                name=existing_plan.name if existing_plan else f"Weekly Plan {now_iso[:10]}",
                created_at=existing_plan.created_at if existing_plan else now_iso,
                updated_at=now_iso,
                slate_id=slate_id,
                salary_source=salary_source,
                contests=dict(contests),
                lineups=[],
                preserved_existing_lineups=list(existing_plan.lineups) if existing_plan else [],
                roster_cards=list(cards),
                projection_overrides=dict(existing_plan.projection_overrides) if existing_plan else {},
                excluded_athlete_keys=list(excluded_set),
                total_weekly_projection=0.0,
                total_weekly_estimated_payout=0.0,
                inventory_summary=inventory_info,
                contest_benchmarks=computed_benchmarks,
                quality_threshold=quality_threshold,
            )
    stage1_sol_status = model.sol_status
    inventory_info["solver_status"] = "optimal" if stage1_sol_status == pulp.LpSolutionOptimal else "time_limit_feasible"

    # Stage 1 variable snapshot (guaranteed integer feasible if we reached here)
    stage1_x = {k: float(v.varValue or 0.0) for k, v in x.items() if v.varValue is not None}
    stage1_z = {k: float(v.varValue or 0.0) for k, v in z.items() if v.varValue is not None}

    # Stage 2: Minimize total salary subject to achieving the exact qualifying count and score from Stage 1
    opt_z_sum = float(pulp.value(pulp.lpSum(z.values())) or 0.0)
    opt_rel_val = float(pulp.value(relative_score_objective) or 0.0)
    stage2_success = False
    if opt_z_sum > 0:
        model += pulp.lpSum(z.values()) >= opt_z_sum, "stage2_count_floor"
        model += relative_score_objective >= opt_rel_val - 1e-6, "stage2_rel_floor"
        model.setObjective(total_salary_expr)
        model.sense = pulp.LpMinimize
        stage2_solver = pulp.PULP_CBC_CMD(msg=0, timeLimit=min(5.0, _time_limit))
        model.solve(stage2_solver)
        if model.sol_status in (pulp.LpSolutionOptimal, pulp.LpSolutionIntegerFeasible):
            stage2_success = True

    # If Stage 2 did not produce an optimal/integer-feasible solution, restore Stage 1 integer values
    def get_x_val(cid: str, lid: str, sname: str) -> float:
        if stage2_success:
            var = x.get((cid, lid, sname))
            if var is not None and pulp.value(var) is not None:
                return float(pulp.value(var))
        return stage1_x.get((cid, lid, sname), 0.0)

    def get_z_val(lid: str) -> float:
        if stage2_success:
            var = z.get(lid)
            if var is not None and pulp.value(var) is not None:
                return float(pulp.value(var))
        return stage1_z.get(lid, 0.0)

    solved_lineups: list[PlannerLineup] = []
    # Stable ordering: retain active candidate lineups
    active_specs = [
        (lid, cname, slots)
        for (lid, cname, slots) in candidate_specs
        if get_z_val(lid) > 0.5
    ]

    for lid, cname, slots in active_specs:
        cfg = enabled_contests[cname]
        slot_assignments: list[LineupSlotAssignment] = []
        for sname, skind in slots:
            assigned_card: PlannerCard | None = None
            for cid, pcard in card_dict.items():
                if get_x_val(cid, lid, sname) > 0.5:
                    assigned_card = pcard
                    break
            is_locked = (lid, sname) in locked_assignments or lid in locked_lineup_ids
            slot_assignments.append(
                LineupSlotAssignment(
                    slot_name=sname,
                    slot_kind=skind,
                    card=assigned_card,
                    is_locked=is_locked,
                )
            )

        c_bench = bench_scores.get(cname, 0.0)
        pl = PlannerLineup(
            lineup_id=lid,
            contest_name=cname,
            slots=slot_assignments,
            is_locked=lid in locked_lineup_ids,
            contest_benchmark=c_bench,
        )
        solved_lineups.append(pl)

    # Build card usage map and recalculate all lineups
    card_usage: dict[str, list[str]] = {}
    for l in solved_lineups:
        for s in l.slots:
            if s.card is not None:
                card_usage.setdefault(s.card.card.card_id, []).append(l.lineup_id)

    # Unassigned eligible cards available for unused salary assessment
    assigned_card_ids = set(card_usage.keys())
    unassigned_pool = [c for c in eligible_pool if c.card.card_id not in assigned_card_ids]

    for l in solved_lineups:
        cfg = enabled_contests[l.contest_name]
        recalculate_lineup(l, cfg, card_usage)
        # Compute benchmark percentage
        if l.contest_benchmark > 0:
            l.benchmark_percentage = round((l.total_projection / l.contest_benchmark) * 100, 1)
        else:
            l.benchmark_percentage = 100.0
        # Assess unused salary (>10% of cap)
        is_unused, unused_note = assess_unused_salary(l, cfg, unassigned_pool, card_usage_across_plan=card_usage)
        l.unused_salary_flag = is_unused
        l.unused_salary_note = unused_note

    if any(not lineup.is_valid for lineup in solved_lineups):
        qualifies, reason = is_plan_fully_qualifying(
            existing_plan, cards, contests, excluded_set, computed_benchmarks, quality_threshold, primetime_teams=pt_teams
        )
        if qualifies and existing_plan:
            preserved = WeeklyPlan(
                plan_id=existing_plan.plan_id,
                name=existing_plan.name,
                created_at=existing_plan.created_at,
                updated_at=datetime.now(timezone.utc).isoformat(),
                slate_id=slate_id,
                salary_source=salary_source,
                contests=dict(contests),
                lineups=list(existing_plan.lineups),
                roster_cards=list(cards),
                projection_overrides=dict(existing_plan.projection_overrides),
                excluded_athlete_keys=list(excluded_set),
                total_weekly_projection=existing_plan.total_weekly_projection,
                total_weekly_estimated_payout=existing_plan.total_weekly_estimated_payout,
                inventory_summary=dict(existing_plan.inventory_summary),
                contest_benchmarks=computed_benchmarks,
                quality_threshold=quality_threshold,
            )
            return preserved
        now_iso = datetime.now(timezone.utc).isoformat()
        inv = dict(existing_plan.inventory_summary) if existing_plan else dict(inventory_info)
        if existing_plan and existing_plan.lineups:
            inv["preserved_existing_lineups"] = [l.to_dict() for l in existing_plan.lineups]
            inv["preserved_existing_lineups_count"] = len(existing_plan.lineups)
        return WeeklyPlan(
            plan_id=existing_plan.plan_id if existing_plan else f"plan_{now_iso[:10]}",
            name=existing_plan.name if existing_plan else f"Weekly Plan {now_iso[:10]}",
            created_at=existing_plan.created_at if existing_plan else now_iso,
            updated_at=now_iso,
            slate_id=slate_id,
            salary_source=salary_source,
            contests=dict(contests),
            lineups=[],
            preserved_existing_lineups=list(existing_plan.lineups) if existing_plan else [],
            roster_cards=list(cards),
            projection_overrides=dict(existing_plan.projection_overrides) if existing_plan else {},
            excluded_athlete_keys=list(excluded_set),
            total_weekly_projection=0.0,
            total_weekly_estimated_payout=0.0,
            inventory_summary=inv,
            contest_benchmarks=computed_benchmarks,
            quality_threshold=quality_threshold,
        )
    # Maximum-count proof derives ONLY from the count optimization stage (Stage 1), or when the user target is met.
    # Stage 2 (salary minimization) status must never be mistaken for maximum count proof.
    if stage1_sol_status == pulp.LpSolutionOptimal or len(solved_lineups) >= target_count:
        inventory_info["lineup_count_proven_maximum"] = True
    else:
        inventory_info["lineup_count_proven_maximum"] = False

    # Record explanation of why generation stopped if fewer than target
    stopping_reasons: list[str] = []
    if len(solved_lineups) < target_count:
        if inventory_info.get("solver_status") == "time_limit_feasible" and not inventory_info["lineup_count_proven_maximum"]:
            stopping_reasons.append(
                f"Generated {len(solved_lineups)} competitive lineups meeting the {int(quality_threshold*100)}% quality requirement before reaching the time limit. This count is the best feasible allocation found, not a proven maximum."
            )
            stopping_reasons.append(
                f"{len(unassigned_pool)} eligible cards remain unassigned. Generation stopped due to the time limit rather than proven inventory exhaustion."
            )
        else:
            stopping_reasons.append(
                f"Generated {len(solved_lineups)} competitive lineups meeting the {int(quality_threshold*100)}% quality requirement."
            )
            if unassigned_pool:
                stopping_reasons.append(
                    f"{len(unassigned_pool)} eligible cards remain, but cannot form an additional legal lineup meeting {int(quality_threshold*100)}% of contest benchmark."
                )
        for b in inventory_info.get("bottlenecks", []):
            stopping_reasons.append(b)
    inventory_info["stopping_reasons"] = stopping_reasons

    now_iso = datetime.now(timezone.utc).isoformat()
    total_proj = round(sum(l.total_projection for l in solved_lineups), 2)
    total_ev = round(sum(l.estimated_payout for l in solved_lineups if l.is_valid), 2)

    main_plan = WeeklyPlan(
        plan_id=f"plan_{now_iso[:10]}",
        name=f"Weekly Plan {now_iso[:10]}",
        created_at=now_iso,
        updated_at=now_iso,
        slate_id=slate_id,
        salary_source=salary_source,
        contests=dict(contests),
        lineups=solved_lineups,
        roster_cards=list(cards),
        projection_overrides=dict(existing_plan.projection_overrides) if existing_plan else {},
        excluded_athlete_keys=list(excluded_set),
        total_weekly_projection=total_proj,
        total_weekly_estimated_payout=total_ev,
        inventory_summary=inventory_info,
        contest_benchmarks=computed_benchmarks,
        quality_threshold=quality_threshold,
    )

    best_candidate_plan = main_plan
    if portfolio_seed_plan and plan_rank_key(portfolio_seed_plan) > plan_rank_key(best_candidate_plan):
        best_candidate_plan = finalize_seed_plan(portfolio_seed_plan)

    if existing_plan and existing_plan.lineups:
        qualifies, reason = is_plan_fully_qualifying(
            existing_plan, cards, contests, excluded_set, computed_benchmarks, quality_threshold, primetime_teams=pt_teams
        )
        if qualifies and len(existing_plan.lineups) <= effective_target and all(
            l.contest_name in active_contests and (not contest_distribution or
            sum(v.contest_name == l.contest_name for v in existing_plan.lineups) <= contest_distribution.get(l.contest_name, 0))
            for l in existing_plan.lineups
        ):
            if plan_rank_key(existing_plan) > plan_rank_key(best_candidate_plan):
                preserved = WeeklyPlan(
                    plan_id=existing_plan.plan_id,
                    name=existing_plan.name,
                    created_at=existing_plan.created_at,
                    updated_at=datetime.now(timezone.utc).isoformat(),
                    slate_id=slate_id,
                    salary_source=salary_source,
                    contests=dict(contests),
                    lineups=list(existing_plan.lineups),
                    roster_cards=list(cards),
                    projection_overrides=dict(existing_plan.projection_overrides),
                    excluded_athlete_keys=list(excluded_set),
                    total_weekly_projection=existing_plan.total_weekly_projection,
                    total_weekly_estimated_payout=existing_plan.total_weekly_estimated_payout,
                    inventory_summary=dict(existing_plan.inventory_summary),
                    contest_benchmarks=computed_benchmarks,
                    quality_threshold=quality_threshold,
                )
                return preserved
        else:
            # Old plan does not satisfy current roster, contest rules, exclusions, benchmarks, or quality requirement.
            # Preserve existing entries separately without allowing them to win selection as qualifying lineups.
            best_candidate_plan.preserved_existing_lineups = list(existing_plan.lineups)
            best_candidate_plan.inventory_summary["preserved_existing_lineups"] = [
                l.to_dict() for l in existing_plan.lineups
            ]
            best_candidate_plan.inventory_summary["preserved_existing_lineups_count"] = len(existing_plan.lineups)
            if not best_candidate_plan.lineups:
                best_candidate_plan.inventory_summary.setdefault("stopping_reasons", []).append(
                    f"Preserved {len(existing_plan.lineups)} previous lineup(s) separately because they did not meet current {int(quality_threshold*100)}% quality requirements: {reason}"
                )

    return best_candidate_plan


# ---------------------------------------------------------------------------
# V1 manual construction: empty entries, partial drafts, and transfers
# ---------------------------------------------------------------------------

# These describe an unfinished draft rather than a broken rule. They are the
# only errors an edit may introduce, so a draft stays editable while it is
# missing cards or under the minimum salary.
DRAFT_TOLERABLE_ERROR_MARKERS = (
    " is empty.",
    "is below minimum requirement",
)


def blocking_lineup_errors(
    lineup: PlannerLineup,
    contest: ContestConfig,
    card_usage_across_plan: Mapping[str, Sequence[str]] | None = None,
    primetime_teams: frozenset[str] | Sequence[str] | None = None,
) -> list[str]:
    """Return the rule violations that block an edit, ignoring incomplete-draft notices."""
    _, errors = validate_lineup(
        lineup,
        contest,
        card_usage_across_plan=card_usage_across_plan,
        primetime_teams=primetime_teams,
    )
    return [e for e in errors if not any(marker in e for marker in DRAFT_TOLERABLE_ERROR_MARKERS)]


def plan_card_usage(plan: WeeklyPlan) -> dict[str, list[str]]:
    """Map each assigned card copy id to the lineup ids that currently hold it."""
    usage: dict[str, list[str]] = {}
    for lineup in plan.lineups:
        for slot in lineup.slots:
            if slot.card is not None:
                usage.setdefault(slot.card.card.card_id, []).append(lineup.lineup_id)
    return usage


def revalidate_plan(plan: WeeklyPlan) -> None:
    """Recalculate every lineup, card-copy usage, and plan total after a mutation."""
    card_usage = plan_card_usage(plan)
    for lineup in plan.lineups:
        contest = plan.contests.get(lineup.contest_name)
        if contest:
            recalculate_lineup(lineup, contest, card_usage)
    plan.total_weekly_projection = round(sum(l.total_projection for l in plan.lineups), 2)
    plan.total_weekly_estimated_payout = round(
        sum(l.estimated_payout for l in plan.lineups if l.is_valid), 2
    )
    plan.updated_at = datetime.now(timezone.utc).isoformat()


def add_empty_lineup(
    plan: WeeklyPlan,
    contest_name: str,
    entry_limit: int | None = None,
) -> tuple[bool, str, PlannerLineup | None]:
    """Append an unfilled entry for a contest, bounded by the contest entry limit.

    The entry keeps every contest slot with no card so the user can fill it one
    slot at a time. It stays editable while incomplete.
    """
    contest = plan.contests.get(contest_name)
    if contest is None:
        return False, f"Contest '{contest_name}' is not available.", None
    if not (contest.enabled and contest.eligible):
        return False, f"Contest '{contest_name}' is not open for entry.", None

    limit = contest.default_entry_limit
    if entry_limit is not None:
        limit = min(limit, entry_limit) if limit is not None else entry_limit
    existing = [l for l in plan.lineups if l.contest_name == contest_name]
    if limit is not None and len(existing) >= limit:
        return False, f"Contest '{contest_name}' already has {len(existing)} of {limit} allowed entries.", None

    slug = re.sub(r"[^a-z0-9]", "", contest_name.lower())
    used_ids = {l.lineup_id for l in plan.lineups}
    index = len(existing) + 1
    while f"{slug}_{index}" in used_ids:
        index += 1

    seen_kinds: dict[str, int] = {}
    slots: list[LineupSlotAssignment] = []
    for kind in contest.slots:
        seen_kinds[kind] = seen_kinds.get(kind, 0) + 1
        label = kind if seen_kinds[kind] == 1 else f"{kind}{seen_kinds[kind]}"
        slots.append(LineupSlotAssignment(slot_name=label, slot_kind=kind, card=None, is_locked=False))

    lineup = PlannerLineup(lineup_id=f"{slug}_{index}", contest_name=contest_name, slots=slots)
    plan.lineups.append(lineup)
    revalidate_plan(plan)
    return True, f"Added an empty {contest_name} entry ({len(slots)} slots to fill).", lineup


def clear_slot(plan: WeeklyPlan, lineup_id: str, slot_name: str) -> tuple[bool, str]:
    """Empty an unlocked slot, leaving every other placement untouched."""
    lineup = next((l for l in plan.lineups if l.lineup_id == lineup_id), None)
    if lineup is None:
        return False, f"Lineup '{lineup_id}' not found in plan"
    if lineup.is_locked:
        return False, "Unlock this lineup before clearing a slot."
    slot = next((s for s in lineup.slots if s.slot_name == slot_name), None)
    if slot is None:
        return False, f"Slot '{slot_name}' not found in lineup '{lineup_id}'"
    if slot.is_locked:
        return False, "This slot is locked. Unlock it before clearing."
    if slot.card is None:
        return True, f"Slot '{slot_name}' was already empty."
    slot.card = None
    slot.is_locked = False
    revalidate_plan(plan)
    return True, f"Cleared {slot_name}."


def _copy_lineup(lineup: PlannerLineup) -> PlannerLineup:
    """Copy a lineup and its slots so a proposed change can be validated in place."""
    return PlannerLineup(
        lineup_id=lineup.lineup_id,
        contest_name=lineup.contest_name,
        slots=[
            LineupSlotAssignment(
                slot_name=s.slot_name,
                slot_kind=s.slot_kind,
                card=s.card,
                is_locked=s.is_locked,
            )
            for s in lineup.slots
        ],
        is_locked=lineup.is_locked,
        contest_benchmark=lineup.contest_benchmark,
        benchmark_percentage=lineup.benchmark_percentage,
        unused_salary_flag=lineup.unused_salary_flag,
        unused_salary_note=lineup.unused_salary_note,
    )


def move_or_exchange_card(
    plan: WeeklyPlan,
    source_lineup_id: str,
    source_slot_name: str,
    destination_lineup_id: str,
    destination_slot_name: str,
) -> tuple[bool, str, list[str]]:
    """Move a card into an empty slot or exchange two occupied slots in one transaction.

    Both resulting entries are validated on a copy first. If either entry would
    gain a rule violation it did not already have, nothing changes. Returns the
    lineup ids the caller must refresh.
    """
    source_lineup = next((l for l in plan.lineups if l.lineup_id == source_lineup_id), None)
    destination_lineup = next((l for l in plan.lineups if l.lineup_id == destination_lineup_id), None)
    if source_lineup is None:
        return False, f"Source lineup '{source_lineup_id}' not found in plan", []
    if destination_lineup is None:
        return False, f"Destination lineup '{destination_lineup_id}' not found in plan", []
    source_slot = next((s for s in source_lineup.slots if s.slot_name == source_slot_name), None)
    destination_slot = next((s for s in destination_lineup.slots if s.slot_name == destination_slot_name), None)
    if source_slot is None:
        return False, f"Source slot '{source_slot_name}' not found in lineup '{source_lineup_id}'", []
    if destination_slot is None:
        return False, f"Destination slot '{destination_slot_name}' not found in lineup '{destination_lineup_id}'", []
    if source_slot.card is None:
        return False, "The source slot is empty.", []
    if source_lineup is destination_lineup and source_slot is destination_slot:
        return False, "Source and destination are the same slot.", []

    for lineup, slot, label in (
        (source_lineup, source_slot, "Source"),
        (destination_lineup, destination_slot, "Destination"),
    ):
        if lineup.is_locked:
            return False, f"{label} lineup is locked. Unlock it before moving cards.", []
        if slot.is_locked:
            return False, f"{label} slot '{slot.slot_name}' is locked. Unlock it before moving cards.", []

    transferred = source_slot.card
    displaced = destination_slot.card

    trial_source = _copy_lineup(source_lineup)
    trial_destination = trial_source if source_lineup is destination_lineup else _copy_lineup(destination_lineup)
    trial_source_slot = next(s for s in trial_source.slots if s.slot_name == source_slot_name)
    trial_destination_slot = next(s for s in trial_destination.slots if s.slot_name == destination_slot_name)
    trial_destination_slot.card = transferred
    trial_source_slot.card = displaced
    trial_destination_slot.is_locked = True
    trial_source_slot.is_locked = displaced is not None

    baseline_usage = plan_card_usage(plan)
    trials = {source_lineup.lineup_id: trial_source, destination_lineup.lineup_id: trial_destination}
    trial_usage: dict[str, list[str]] = {}
    for lineup in plan.lineups:
        active = trials.get(lineup.lineup_id, lineup)
        for slot in active.slots:
            if slot.card is not None:
                trial_usage.setdefault(slot.card.card.card_id, []).append(lineup.lineup_id)

    def _new_blocking_errors(lineup: PlannerLineup, trial: PlannerLineup) -> list[str]:
        contest = plan.contests.get(lineup.contest_name)
        if contest is None:
            return [f"Contest '{lineup.contest_name}' is not available."]
        before = set(blocking_lineup_errors(lineup, contest, baseline_usage))
        after = blocking_lineup_errors(trial, contest, trial_usage)
        return [e for e in after if e not in before]

    problems = _new_blocking_errors(source_lineup, trial_source)
    if source_lineup is not destination_lineup:
        problems += _new_blocking_errors(destination_lineup, trial_destination)
    if problems:
        return False, " ".join(problems), []

    destination_slot.card = transferred
    source_slot.card = displaced
    destination_slot.is_locked = True
    source_slot.is_locked = displaced is not None
    revalidate_plan(plan)

    affected = list(dict.fromkeys([source_lineup.lineup_id, destination_lineup.lineup_id]))
    action = "exchanged" if displaced is not None else "moved"
    return True, f"Card {action} successfully.", affected


def swap_card_in_lineup(
    plan: WeeklyPlan,
    lineup_id: str,
    slot_name: str,
    new_card_id: str | None,
) -> tuple[bool, str]:
    """Swap a card into a slot in a lineup and immediately recalculate totals and validity across all lineups."""
    lineup = next((l for l in plan.lineups if l.lineup_id == lineup_id), None)
    if not lineup:
        return False, f"Lineup '{lineup_id}' not found in plan"

    slot = next((s for s in lineup.slots if s.slot_name == slot_name), None)
    if not slot:
        return False, f"Slot '{slot_name}' not found in lineup '{lineup_id}'"

    if new_card_id is None or new_card_id == "":
        slot.card = None
    else:
        new_card = next((c for c in plan.roster_cards if c.card.card_id == new_card_id), None)
        if not new_card:
            return False, f"Card '{new_card_id}' not found in roster pool"
        slot.card = new_card

    revalidate_plan(plan)
    return True, "Card swapped successfully"


def export_plan_to_csv(plan: WeeklyPlan) -> str:
    """Export weekly plan lineups to CSV string."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Lineup ID",
        "Contest",
        "Slot",
        "Player",
        "Position",
        "Team",
        "Multiplier",
        "Weekly Salary",
        "Raw Projection",
        "Adjusted Projection",
        "Card ID",
        "Lineup Total Salary",
        "Lineup Projected Score",
        "Estimated Finish Tier",
        "Estimated Payout ($)",
        "Lineup Status",
    ])
    for l in plan.lineups:
        status_text = "Valid" if l.is_valid else f"Invalid: {'; '.join(l.validation_errors)}"
        for s in l.slots:
            if s.card:
                c = s.card
                writer.writerow([
                    l.lineup_id,
                    l.contest_name,
                    s.slot_name,
                    c.card.player_name,
                    c.card.position,
                    c.card.team,
                    f"{c.card.multiplier:.2f}x",
                    c.weekly_salary,
                    round(c.raw_projection, 2),
                    round(c.adjusted_projection, 2),
                    c.card.card_id,
                    l.total_salary,
                    l.total_projection,
                    l.estimated_tier or "N/A",
                    l.estimated_payout,
                    status_text,
                ])
            else:
                writer.writerow([
                    l.lineup_id,
                    l.contest_name,
                    s.slot_name,
                    "EMPTY",
                    "",
                    "",
                    "",
                    0,
                    0.0,
                    0.0,
                    "",
                    l.total_salary,
                    l.total_projection,
                    l.estimated_tier or "N/A",
                    l.estimated_payout,
                    status_text,
                ])
    return output.getvalue()
