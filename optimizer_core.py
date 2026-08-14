"""Pure GameBlazers football optimizer core.

This module deliberately has no Flask or UI dependency.  It accepts the two
small V1 inputs (a roster export and a raw weekly projection source), performs
conservative normalization, and searches every valid slot assignment exactly.
"""

from __future__ import annotations

import csv
import io
import itertools
import math
from functools import lru_cache
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, TextIO


POSITIONS = ("QB", "RB", "WR", "TE")
FLEX_POSITIONS = frozenset({"RB", "WR", "TE"})
SUPERFLEX_POSITIONS = frozenset(POSITIONS)
TEAM_ALIASES = {"LVR": "LV", "JAC": "JAX"}
POSITION_ALIASES = {
    "QB": "QB",
    "QUARTERBACK": "QB",
    "RB": "RB",
    "RUNNINGBACK": "RB",
    "WR": "WR",
    "WIDERECEIVER": "WR",
    "TE": "TE",
    "TIGHTEND": "TE",
}
_SUFFIXES = frozenset({"JR", "SR", "II", "III", "IV"})


@dataclass(frozen=True)
class ContestDefinition:
    name: str
    slots: tuple[str, ...]
    minimum_salary: int
    maximum_salary: int


CONTESTS = {
    "Spark": ContestDefinition("Spark", ("QB", "RB", "WR", "TE"), 16000, 32000),
    "Scorcher": ContestDefinition("Scorcher", ("QB", "RB", "WR", "TE", "Flex"), 18375, 36750),
    "Wildfire": ContestDefinition("Wildfire", ("QB", "RB", "WR", "TE", "Flex", "Flex"), 24000, 48000),
    "Flex Appeal": ContestDefinition("Flex Appeal", ("QB", "Flex", "Flex", "Flex", "Flex", "Flex"), 26000, 52000),
    "Flamethrower": ContestDefinition("Flamethrower", ("QB", "RB", "WR", "TE", "Flex", "Superflex"), 30000, 60000),
    "Inferno": ContestDefinition("Inferno", ("QB", "RB", "RB", "WR", "WR", "TE", "Flex", "Superflex"), 32000, 64000),
}
CONTEST_ALIASES = {"Volcano": "Wildfire"}


@dataclass(frozen=True)
class Diagnostic:
    reason: str
    source_row: int | None = None
    player_name: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class NormalizedCard:
    display_name: str
    athlete_key: str
    team: str
    position: str
    multiplier: float
    salary: int
    status: str
    source_row: int
    card_row_id: str
    source_values: Mapping[str, Any] = field(repr=False, compare=False)
    visible_duplicate_count: int = 1


@dataclass(frozen=True)
class NormalizedProjection:
    display_name: str
    athlete_key: str
    team: str
    position: str
    raw_projection: float
    source_row: int
    source_values: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass
class ParseResult:
    rows: list[Any]
    diagnostics: list[Diagnostic]
    rows_read: int
    schema_errors: list[str] = field(default_factory=list)


@dataclass
class PreparedPool:
    cards: list[NormalizedCard]
    diagnostics: list[Diagnostic]
    roster_rows_read: int
    projection_rows_read: int
    matched_cards: int
    eligible_cards: int
    schema_errors: list[str] = field(default_factory=list)
    matched_projections: dict[str, NormalizedProjection] = field(default_factory=dict)

    @property
    def diagnostics_by_reason(self) -> dict[str, list[Diagnostic]]:
        return _group_diagnostics(self.diagnostics)


@dataclass
class OptimizationResult:
    feasible: bool
    contest: str
    slots: tuple[str, ...]
    lineup: list[dict[str, Any]]
    total_salary: int
    minimum_salary: int
    maximum_salary: int
    remaining_salary: int
    total_adjusted_projection: float
    diagnostics: list[Diagnostic]
    matched_cards: int
    eligible_cards: int
    roster_rows_read: int
    projection_rows_read: int
    infeasible_reason: str | None = None

    @property
    def diagnostics_by_reason(self) -> dict[str, list[Diagnostic]]:
        return _group_diagnostics(self.diagnostics)


@dataclass(frozen=True)
class _Slot:
    label: str
    kind: str
    order: int


# ---------------------------------------------------------------------------
# Normalization and parsing


def normalize_team(value: Any) -> str:
    team = str(value or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def normalize_position(value: Any) -> str:
    raw = re.sub(r"[^A-Za-z]", "", str(value or "")).upper()
    return POSITION_ALIASES.get(raw, raw)


def normalize_name(value: Any) -> str:
    """Return a conservative identity key, not a fuzzy-search key."""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    text = text.replace("\u2010", "-").replace("\u2011", "-").replace("\u2013", "-")
    tokens = re.findall(r"[A-Za-z0-9]+", text.casefold())
    if tokens and tokens[-1].upper() in _SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _header_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").casefold())


def _read_source(source: Any) -> tuple[list[tuple[int, dict[str, Any]]], list[str], list[str]]:
    """Read a path, text stream, mapping sequence, or pandas-like frame."""
    if isinstance(source, ParseResult):
        return [], [], []

    if hasattr(source, "to_dict") and not isinstance(source, (str, bytes, Path)):
        try:
            records = source.to_dict("records")
        except TypeError:
            records = None
        if records is not None:
            rows = [(index + 2, dict(row)) for index, row in enumerate(records)]
            return rows, list(records[0].keys()) if records else [], []

    if isinstance(source, Mapping):
        return [(2, dict(source))], list(source.keys()), []

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.exists():
            with path.open("r", newline="", encoding="utf-8-sig") as handle:
                return _read_csv_stream(handle)
        if "\n" in str(source):
            return _read_csv_stream(io.StringIO(str(source)))
        return [], [], [f"input path does not exist: {source}"]

    if hasattr(source, "read"):
        return _read_csv_stream(source)

    try:
        records = [dict(row) for row in source]
    except (TypeError, ValueError):
        return [], [], ["input is not a CSV source or mapping sequence"]
    headers = list(records[0].keys()) if records else []
    return [(index + 2, row) for index, row in enumerate(records)], headers, []


def _read_csv_stream(stream: TextIO) -> tuple[list[tuple[int, dict[str, Any]]], list[str], list[str]]:
    reader = csv.DictReader(stream)
    headers = list(reader.fieldnames or [])
    if not headers:
        return [], [], ["CSV input has no header row"]
    return [(index + 2, dict(row)) for index, row in enumerate(reader)], headers, []


def _resolve_headers(headers: Sequence[str], aliases: Mapping[str, Sequence[str]]) -> tuple[dict[str, str], list[str]]:
    available = {_header_key(header): header for header in headers}
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for canonical, candidates in aliases.items():
        match = next((available[_header_key(candidate)] for candidate in candidates if _header_key(candidate) in available), None)
        if match is None:
            missing.append(canonical)
        else:
            resolved[canonical] = match
    return resolved, missing


def _number(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace(",", "").replace("$", "")
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _projection_suffix(value: Any) -> tuple[str, str, str] | None:
    match = re.match(r"^\s*(.+?)\s+(QB|RB|WR|TE)\s+([A-Za-z]{2,3})\s*$", str(value or ""), re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip(), normalize_position(match.group(2)), normalize_team(match.group(3))


def parse_roster(source: Any) -> ParseResult:
    if isinstance(source, ParseResult):
        return source
    raw_rows, headers, source_errors = _read_source(source)
    aliases = {
        "player_name": ("player_name", "Player"),
        "team": ("team", "Team"),
        "position": ("position", "Positions", "Position"),
        "multiplier": ("multiplier", "Multiplier"),
        "salary": ("salary", "Salary"),
        "status": ("status", "Status"),
    }
    resolved, missing = _resolve_headers(headers, aliases)
    schema_errors = source_errors + ([f"roster missing columns: {', '.join(missing)}"] if missing else [])
    if schema_errors:
        return ParseResult([], [Diagnostic("malformed_roster_row", detail=error) for error in schema_errors], len(raw_rows), schema_errors)

    rows: list[NormalizedCard] = []
    diagnostics: list[Diagnostic] = []
    for source_row, raw in raw_rows:
        display_name = str(raw.get(resolved["player_name"], "") or "").strip()
        team = normalize_team(raw.get(resolved["team"]))
        position = normalize_position(raw.get(resolved["position"]))
        multiplier = _number(raw.get(resolved["multiplier"]))
        salary_number = _number(raw.get(resolved["salary"]))
        status = str(raw.get(resolved["status"], "") or "").strip()
        if not display_name or not team or position not in POSITIONS or multiplier is None or multiplier <= 0 or salary_number is None or salary_number < 0 or not status:
            diagnostics.append(Diagnostic("malformed_roster_row", source_row, display_name or None, "missing or invalid roster field"))
            continue
        if not salary_number.is_integer():
            diagnostics.append(Diagnostic("malformed_roster_row", source_row, display_name, "salary must be an integer"))
            continue
        rows.append(NormalizedCard(display_name, normalize_name(display_name), team, position, multiplier, int(salary_number), status, source_row, f"roster-row-{source_row}", raw))
    return ParseResult(rows, diagnostics, len(raw_rows), schema_errors)


def parse_projections(source: Any) -> ParseResult:
    if isinstance(source, ParseResult):
        return source
    raw_rows, headers, source_errors = _read_source(source)
    aliases = {
        "player_name": ("player_name", "Player"),
        "team": ("team", "Team"),
        "position": ("position", "Positions", "Position"),
        "raw_projection": ("raw_projection", "3D Proj.", "3D Proj", "3D_Proj.", "3D_Proj", "3D Projection"),
    }
    resolved, missing = _resolve_headers(headers, aliases)
    # Historical sheets carry position/team in Player, so those two headers can be inferred.
    required_missing = [name for name in missing if name not in {"team", "position"}]
    schema_errors = source_errors + ([f"projection missing columns: {', '.join(required_missing)}"] if required_missing else [])
    if schema_errors:
        return ParseResult([], [Diagnostic("malformed_projection_row", detail=error) for error in schema_errors], len(raw_rows), schema_errors)

    rows: list[NormalizedProjection] = []
    diagnostics: list[Diagnostic] = []
    for source_row, raw in raw_rows:
        raw_player = raw.get(resolved["player_name"], "")
        display_name = str(raw_player or "").strip()
        position_value = raw.get(resolved["position"]) if "position" in resolved else None
        team_value = raw.get(resolved["team"]) if "team" in resolved else None
        if not position_value or not team_value:
            suffix = _projection_suffix(raw_player)
            if suffix:
                display_name, position_value, team_value = suffix
        position = normalize_position(position_value)
        team = normalize_team(team_value)
        raw_projection = _number(raw.get(resolved["raw_projection"]))
        if not display_name or position not in POSITIONS or not team or raw_projection is None:
            diagnostics.append(Diagnostic("malformed_projection_row", source_row, display_name or None, "missing or invalid projection field"))
            continue
        rows.append(NormalizedProjection(display_name, normalize_name(display_name), team, position, raw_projection, source_row, raw))
    return ParseResult(rows, diagnostics, len(raw_rows), schema_errors)


# ---------------------------------------------------------------------------
# Matching and eligibility


def _group_diagnostics(diagnostics: Iterable[Diagnostic]) -> dict[str, list[Diagnostic]]:
    grouped: dict[str, list[Diagnostic]] = {}
    for diagnostic in diagnostics:
        grouped.setdefault(diagnostic.reason, []).append(diagnostic)
    return grouped


def _as_parse_result(source: Any, parser: Any) -> ParseResult:
    return source if isinstance(source, ParseResult) else parser(source)


def prepare_pool(roster_source: Any, projection_source: Any) -> PreparedPool:
    roster = _as_parse_result(roster_source, parse_roster)
    projections = _as_parse_result(projection_source, parse_projections)
    diagnostics = list(roster.diagnostics) + list(projections.diagnostics)
    schema_errors = list(roster.schema_errors) + list(projections.schema_errors)
    if schema_errors:
        return PreparedPool([], diagnostics, roster.rows_read, projections.rows_read, 0, 0, schema_errors)

    projection_index: dict[str, list[NormalizedProjection]] = {}
    for projection in projections.rows:
        projection_index.setdefault(projection.athlete_key, []).append(projection)

    exact_matches: list[tuple[NormalizedCard, NormalizedProjection]] = []
    matched_cards = 0
    for card in roster.rows:
        if card.status.casefold() != "active":
            diagnostics.append(Diagnostic("non_active", card.source_row, card.display_name, f"status={card.status}"))
            continue

        same_name = projection_index.get(card.athlete_key, [])
        if not same_name:
            diagnostics.append(Diagnostic("missing_projection", card.source_row, card.display_name, "no projection row matched the normalized athlete"))
            diagnostics.append(Diagnostic("name_unmatched", card.source_row, card.display_name, "no projection row matched the normalized athlete"))
            continue
        same_position = [row for row in same_name if row.position == card.position]
        if not same_position:
            diagnostics.append(Diagnostic("position_mismatch", card.source_row, card.display_name, f"roster={card.position}; projection={same_name[0].position}"))
            continue
        same_team = [row for row in same_position if row.team == card.team]
        if not same_team:
            diagnostics.append(Diagnostic("team_mismatch", card.source_row, card.display_name, f"roster={card.team}; projection={same_position[0].team}"))
            continue
        if len(same_team) > 1:
            diagnostics.append(Diagnostic("projection_ambiguous", card.source_row, card.display_name, "multiple exact projection rows"))
            continue
        projection = same_team[0]
        matched_cards += 1
        diagnostics.append(Diagnostic("matched", card.source_row, card.display_name, f"projection_row={projection.source_row}"))
        if projection.raw_projection <= 0:
            diagnostics.append(Diagnostic("zero_projection", card.source_row, card.display_name, f"raw_projection={projection.raw_projection}"))
            continue
        exact_matches.append((card, projection))

    eligible: list[NormalizedCard] = []
    for card, projection in exact_matches:
        # The parser already validates these values; this guard keeps eligibility explicit.
        if card.multiplier <= 0 or card.salary < 0:
            diagnostics.append(Diagnostic("malformed_roster_row", card.source_row, card.display_name, "invalid multiplier or salary"))
            continue
        eligible.append(card)

    # Exact visible duplicates are interchangeable for optimization without an Item ID.
    groups: dict[tuple[Any, ...], list[NormalizedCard]] = {}
    for card in eligible:
        groups.setdefault((card.athlete_key, card.team, card.position, card.multiplier, card.salary, card.status.casefold()), []).append(card)
    collapsed: list[NormalizedCard] = []
    matched_projections: dict[str, NormalizedProjection] = {}
    for card, projection in exact_matches:
        matched_projections[card.card_row_id] = projection
    for group in groups.values():
        retained = group[0]
        if len(group) > 1:
            retained = NormalizedCard(retained.display_name, retained.athlete_key, retained.team, retained.position, retained.multiplier, retained.salary, retained.status, retained.source_row, retained.card_row_id, retained.source_values, len(group))
            for duplicate in group[1:]:
                diagnostics.append(Diagnostic("exact_visible_duplicate_collapsed", duplicate.source_row, duplicate.display_name, f"retained_source_row={retained.source_row}; count={len(group)}"))
        collapsed.append(retained)
    collapsed.sort(key=lambda card: card.source_row)
    return PreparedPool(collapsed, diagnostics, roster.rows_read, projections.rows_read, matched_cards, len(collapsed), schema_errors, {card.card_row_id: matched_projections[card.card_row_id] for card in collapsed})


# ---------------------------------------------------------------------------
# Exact slot-aware assignment


def _contest(name: str) -> ContestDefinition:
    canonical = CONTEST_ALIASES.get(name, name)
    try:
        return CONTESTS[canonical]
    except KeyError as exc:
        raise ValueError(f"unknown contest: {name}") from exc


def _slots(definition: ContestDefinition) -> list[_Slot]:
    seen: dict[str, int] = {}
    slots: list[_Slot] = []
    for order, kind in enumerate(definition.slots):
        seen[kind] = seen.get(kind, 0) + 1
        label = kind if seen[kind] == 1 else f"{kind}{seen[kind]}"
        slots.append(_Slot(label, kind, order))
    return slots


def _allowed_positions(kind: str) -> frozenset[str]:
    if kind == "Flex":
        return FLEX_POSITIONS
    if kind == "Superflex":
        return SUPERFLEX_POSITIONS
    return frozenset({kind})


def _find_assignment(cards: Sequence[NormalizedCard], slots: Sequence[_Slot], adjusted_projections: Mapping[str, float], minimum_salary: int, maximum_salary: int, enforce_athlete: bool = True) -> list[NormalizedCard] | None:
    candidates_by_slot = [[card for card in cards if card.position in _allowed_positions(slot.kind)] for slot in slots]
    if any(not candidates for candidates in candidates_by_slot):
        return None
    search_order = sorted(range(len(slots)), key=lambda index: (len(candidates_by_slot[index]), slots[index].order))
    ordered_candidates = [sorted(candidates_by_slot[index], key=lambda card: (-adjusted_projections[card.card_row_id], card.salary, card.source_row)) for index in search_order]
    best: tuple[float, int, tuple[int, ...], list[NormalizedCard]] | None = None
    selected: list[NormalizedCard | None] = [None] * len(slots)
    repeated_group_options: dict[int, list[tuple[NormalizedCard, ...]]] = {}
    for depth in range(len(search_order)):
        remaining_indices = search_order[depth:]
        if len(remaining_indices) in (2,) and len({slots[index].kind for index in remaining_indices}) == 1:
            options = itertools.combinations(ordered_candidates[depth], len(remaining_indices))
            repeated_group_options[depth] = sorted(
                options,
                key=lambda group: (-sum(adjusted_projections[card.card_row_id] for card in group), sum(card.salary for card in group), tuple(card.source_row for card in group)),
            )
    used_card_ids: set[str] = set()
    used_athletes: set[str] = set()
    state_frontier: dict[tuple[int, tuple[str, ...]], list[tuple[int, float]]] = {}

    @lru_cache(maxsize=None)
    def salary_aware_upper(depth: int, budget: int) -> float:
        if depth == len(ordered_candidates):
            return 0.0
        best_projection = float("-inf")
        for card in ordered_candidates[depth]:
            if card.salary <= budget:
                candidate_projection = adjusted_projections[card.card_row_id] + salary_aware_upper(depth + 1, budget - card.salary)
                if candidate_projection > best_projection:
                    best_projection = candidate_projection
        return best_projection

    def upper_bound(depth: int, projection: float) -> float:
        if math.isfinite(maximum_salary):
            remaining_budget = maximum_salary - sum(card.salary for card in selected if card is not None)
            return projection + salary_aware_upper(depth, remaining_budget) if remaining_budget >= 0 else float("-inf")
        return projection + sum(max(adjusted_projections[card.card_row_id] for card in candidates) for candidates in ordered_candidates[depth:])

    def lower_salary_bound(depth: int) -> int:
        return sum(min(card.salary for card in candidates) for candidates in ordered_candidates[depth:])

    def better(projection: float, salary: int, chosen: Sequence[NormalizedCard]) -> bool:
        nonlocal best
        order_key = tuple(card.source_row for card in chosen)
        if best is None:
            return True
        best_projection, best_salary, best_order, _ = best
        if projection != best_projection:
            return projection > best_projection
        if salary != best_salary:
            return salary < best_salary
        return order_key < best_order

    def visit(depth: int, salary: int, projection: float) -> None:
        nonlocal best
        if salary > maximum_salary or salary + lower_salary_bound(depth) > maximum_salary:
            return
        if enforce_athlete:
            state_key = (depth, tuple(sorted(used_athletes)))
            frontier = state_frontier.setdefault(state_key, [])
            # Equal-salary states with lower projection are dominated.  Do
            # not treat a cheaper state as dominant: V1 may require a
            # minimum salary, so the higher-salary path can be the only valid
            # completion.
            if any(previous_salary == salary and previous_projection >= projection for previous_salary, previous_projection in frontier):
                return
            frontier[:] = [
                (previous_salary, previous_projection)
                for previous_salary, previous_projection in frontier
                if not (previous_salary == salary and projection >= previous_projection)
            ]
            frontier.append((salary, projection))
        if best is not None and upper_bound(depth, projection) < best[0]:
            return
        if depth in repeated_group_options:
            remaining_indices = search_order[depth:]
            for group in repeated_group_options[depth]:
                if (enforce_athlete and len({card.athlete_key for card in group}) != len(group)) or any(card.card_row_id in used_card_ids or (enforce_athlete and card.athlete_key in used_athletes) for card in group):
                    continue
                group_salary = sum(card.salary for card in group)
                if salary + group_salary > maximum_salary or salary + group_salary < minimum_salary:
                    continue
                group_projection = sum(adjusted_projections[card.card_row_id] for card in group)
                for slot_index, card in zip(remaining_indices, group):
                    selected[slot_index] = card
                chosen = [card for card in selected if card is not None]
                if better(projection + group_projection, salary + group_salary, chosen):
                    best = (projection + group_projection, salary + group_salary, tuple(card.source_row for card in chosen), list(chosen))
                for slot_index in remaining_indices:
                    selected[slot_index] = None
                # Options are projection-descending, so the first compatible
                # option is optimal for this already-fixed prefix.
                return
        if depth == len(search_order):
            if salary < minimum_salary:
                return
            chosen = [selected[index] for index in range(len(selected))]
            concrete = [card for card in chosen if card is not None]
            if better(projection, salary, concrete):
                best = (projection, salary, tuple(card.source_row for card in concrete), concrete)
            return
        slot_index = search_order[depth]
        for card in ordered_candidates[depth]:
            if card.card_row_id in used_card_ids or (enforce_athlete and card.athlete_key in used_athletes):
                continue
            same_kind_sources = [
                selected[index].source_row
                for index, prior_slot in enumerate(slots)
                if selected[index] is not None and prior_slot.kind == slots[slot_index].kind
            ]
            # Repeated Flex/RB/WR slots are interchangeable; this removes
            # duplicate permutations without changing the assignment space.
            if same_kind_sources and card.source_row <= max(same_kind_sources):
                continue
            used_card_ids.add(card.card_row_id)
            if enforce_athlete:
                used_athletes.add(card.athlete_key)
            selected[slot_index] = card
            visit(depth + 1, salary + card.salary, projection + adjusted_projections[card.card_row_id])
            selected[slot_index] = None
            used_card_ids.remove(card.card_row_id)
            if enforce_athlete:
                used_athletes.remove(card.athlete_key)

    visit(0, 0, 0.0)
    return best[3] if best else None


def _result_lineup(cards: Sequence[NormalizedCard], slots: Sequence[_Slot], projections: Mapping[str, NormalizedProjection]) -> list[dict[str, Any]]:
    # The solver returns cards in configured slot order, while its internal search order differs.
    rows: list[dict[str, Any]] = []
    for slot, card in zip(slots, cards):
        projection = projections[card.card_row_id]
        rows.append({
            "slot": slot.label,
            "player_name": card.display_name,
            "athlete_key": card.athlete_key,
            "position": card.position,
            "team": card.team,
            "source_row": card.source_row,
            "card_row_id": card.card_row_id,
            "multiplier": card.multiplier,
            "salary": card.salary,
            "raw_projection": projection.raw_projection,
            "adjusted_projection": projection.raw_projection * card.multiplier,
            "visible_duplicate_count": card.visible_duplicate_count,
        })
    return rows


def optimize_lineup(roster_source: Any, projection_source: Any, contest: str, minimum_salary: int | None = None, maximum_salary: int | None = None) -> OptimizationResult:
    definition = _contest(contest)
    pool = prepare_pool(roster_source, projection_source)
    minimum = definition.minimum_salary if minimum_salary is None else int(minimum_salary)
    maximum = definition.maximum_salary if maximum_salary is None else int(maximum_salary)
    slots = _slots(definition)
    empty = OptimizationResult(False, definition.name, definition.slots, [], 0, minimum, maximum, maximum, 0.0, pool.diagnostics, pool.matched_cards, pool.eligible_cards, pool.roster_rows_read, pool.projection_rows_read)

    if pool.schema_errors:
        empty.infeasible_reason = "malformed_input_schema"
        return empty

    candidates_by_slot = [[card for card in pool.cards if card.position in _allowed_positions(slot.kind)] for slot in slots]
    if any(not candidates for candidates in candidates_by_slot):
        empty.infeasible_reason = "no_eligible_card_for_required_slot"
        return empty

    adjusted_projections = {
        card.card_row_id: pool.matched_projections[card.card_row_id].raw_projection * card.multiplier
        for card in pool.cards
    }
    assignment = _find_assignment(pool.cards, slots, adjusted_projections, minimum, maximum)
    if assignment is None:
        unconstrained = _find_assignment(pool.cards, slots, adjusted_projections, 0, math.inf)
        if unconstrained is None:
            without_athlete_constraint = _find_assignment(pool.cards, slots, adjusted_projections, 0, math.inf, enforce_athlete=False)
            empty.infeasible_reason = "duplicate_athlete_constraint" if without_athlete_constraint is not None else "no_feasible_assignment"
        elif _find_assignment(pool.cards, slots, adjusted_projections, 0, maximum) is None:
            empty.infeasible_reason = "maximum_cap_infeasibility"
        else:
            empty.infeasible_reason = "minimum_spend_infeasibility"
        return empty

    # _find_assignment returns configured slot order because selected is indexed by slot.
    lineup = _result_lineup(assignment, slots, pool.matched_projections)
    total_salary = sum(row["salary"] for row in lineup)
    total_projection = sum(row["adjusted_projection"] for row in lineup)
    return OptimizationResult(True, definition.name, definition.slots, lineup, total_salary, minimum, maximum, maximum - total_salary, total_projection, pool.diagnostics, pool.matched_cards, pool.eligible_cards, pool.roster_rows_read, pool.projection_rows_read)


__all__ = [
    "CONTESTS",
    "CONTEST_ALIASES",
    "ContestDefinition",
    "Diagnostic",
    "NormalizedCard",
    "NormalizedProjection",
    "OptimizationResult",
    "ParseResult",
    "PreparedPool",
    "normalize_name",
    "normalize_position",
    "normalize_team",
    "parse_projections",
    "parse_roster",
    "prepare_pool",
    "optimize_lineup",
]
