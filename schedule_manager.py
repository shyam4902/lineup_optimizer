"""NFL schedule loader, game window classifier, and late-swap slot organizer."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from optimizer_core import normalize_team


WINDOW_TNF = "TNF"
WINDOW_SUN_EARLY = "SUN_EARLY"
WINDOW_SUN_LATE = "SUN_LATE"
WINDOW_SNF = "SNF"
WINDOW_MNF = "MNF"
WINDOW_OTHER = "OTHER"


def _candidate_schedule_paths() -> list[Path]:
    base_dir = Path(__file__).resolve().parent
    paths = [
        base_dir / "data" / "nfl_schedule_2026.json",
        base_dir.parent.parent / "NFL_Main" / "nfldashboard" / "props-board.json",
    ]
    env_path = os.environ.get("NFL_DASHBOARD_BOARD_PATH")
    if env_path:
        paths.insert(0, Path(env_path))
    return paths


def load_raw_schedule_games() -> list[dict[str, Any]]:
    for path in _candidate_schedule_paths():
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                games = data.get("games", [])
                if isinstance(games, list) and games:
                    return games
            except Exception:
                continue
    return []


def classify_game_window(kickoff_iso: str | None) -> str:
    if not kickoff_iso:
        return WINDOW_OTHER
    try:
        dt = datetime.fromisoformat(str(kickoff_iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            dt_et = dt.astimezone(ZoneInfo("America/New_York"))
        except Exception:
            from datetime import timedelta
            dt_et = dt.astimezone(timezone(timedelta(hours=-4)))
    except Exception:
        return WINDOW_OTHER

    weekday = dt_et.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    hour = dt_et.hour

    if weekday == 3:  # Thursday
        return WINDOW_TNF
    if weekday == 0:  # Monday
        return WINDOW_MNF
    if weekday == 6:  # Sunday
        if hour < 15:
            return WINDOW_SUN_EARLY
        elif hour < 18:
            return WINDOW_SUN_LATE
        else:
            return WINDOW_SNF

    return WINDOW_OTHER


def detect_current_nfl_week(target_time: datetime | None = None, games: Sequence[dict[str, Any]] | None = None) -> int:
    effective_time = target_time if target_time is not None else datetime.now(timezone.utc)
    if effective_time.tzinfo is None:
        effective_time = effective_time.replace(tzinfo=timezone.utc)

    all_games = games if games is not None else load_raw_schedule_games()
    if not all_games:
        return 1

    weeks: dict[int, list[datetime]] = {}
    for g in all_games:
        w = g.get("week")
        ko = g.get("kickoff")
        if w is None or not ko:
            continue
        try:
            dt = datetime.fromisoformat(str(ko).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            weeks.setdefault(int(w), []).append(dt)
        except Exception:
            continue

    if not weeks:
        return 1

    # Check which week interval effective_time falls into
    # Week interval: from 2 days before first game to 1 day after last game of that week
    for w in sorted(weeks.keys()):
        kicks = weeks[w]
        min_k = min(kicks)
        max_k = max(kicks)
        if min_k.timestamp() - 2 * 86400 <= effective_time.timestamp() <= max_k.timestamp() + 86400:
            return w

    # If before week 1
    first_week_min = min(weeks[min(weeks.keys())])
    if effective_time.timestamp() < first_week_min.timestamp():
        return min(weeks.keys())

    # Fallback to latest week or week 3
    return 3


def build_team_schedule_map(week: int | None = None, target_time: datetime | None = None) -> dict[str, dict[str, str]]:
    games = load_raw_schedule_games()
    if not games:
        return {}

    target_week = week if week is not None else detect_current_nfl_week(target_time, games)
    selected_games = [g for g in games if g.get("week") == target_week]

    team_map: dict[str, dict[str, str]] = {}
    for g in selected_games:
        ko_str = g.get("kickoff", "")
        if not ko_str:
            continue
        try:
            dt = datetime.fromisoformat(str(ko_str).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ko_iso = dt.astimezone(timezone.utc).isoformat()
            g_date = dt.strftime("%Y-%m-%d")
            window = classify_game_window(ko_iso)
        except Exception:
            continue

        away = normalize_team(str(g.get("away_team", "")))
        home = normalize_team(str(g.get("home_team", "")))
        if not away or not home:
            continue

        team_map[away] = {
            "kickoff_time": ko_iso,
            "game_date": g_date,
            "opp": f"@{home}",
            "window": window,
            "week": str(target_week),
        }
        team_map[home] = {
            "kickoff_time": ko_iso,
            "game_date": g_date,
            "opp": f"vs {away}",
            "window": window,
            "week": str(target_week),
        }

    return team_map


def _slot_flexibility_score(slot_kind: str) -> int:
    kind = slot_kind.upper()
    if kind == "SUPERFLEX":
        return 20
    if kind == "FLEX":
        return 10
    return 0


def _slot_accepts_pos(slot_kind: str, pos: str) -> bool:
    kind = slot_kind.upper()
    p = pos.upper()
    if kind == "QB":
        return p == "QB"
    if kind == "RB":
        return p == "RB"
    if kind == "WR":
        return p == "WR"
    if kind == "TE":
        return p == "TE"
    if kind == "FLEX":
        return p in ("RB", "WR", "TE")
    if kind == "SUPERFLEX":
        return p in ("QB", "RB", "WR", "TE")
    return False


def _parse_kickoff_timestamp(kickoff_time: str | None) -> float:
    if not kickoff_time:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(kickoff_time).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def order_lineup_slots_for_late_swap(slots: Sequence[Any]) -> list[Any]:
    """Rearrange existing cards across slots to maximize late-swap flexibility.

    Rules:
    - Locked slots or cards whose games have already started stay fixed.
    - Unlocked cards are reassigned across unlocked slots so earlier kickoff players
      occupy rigid position slots (QB, RB, WR, TE) and later kickoff players
      occupy flexible slots (Flex, Superflex).
    - Preserves 100% of the cards, salary, projections, and multipliers.
    """
    if not slots:
        return []

    def _get_attr(obj: Any, name: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    # Fast path: check if any unlocked slot has card
    has_unlocked_cards = any(_get_attr(s, "card") is not None and not _get_attr(s, "is_locked", False) for s in slots)
    if not has_unlocked_cards:
        return list(slots)

    fixed_assignment: dict[str, Any] = {}
    unlocked_slots: list[Any] = []
    unlocked_cards: list[Any] = []

    for s in slots:
        is_locked = _get_attr(s, "is_locked", False)
        card = _get_attr(s, "card", None)
        card_locked = is_locked
        if card:
            live_locked = _get_attr(card, "is_locked", False)
            if live_locked:
                card_locked = True

        slot_name = _get_attr(s, "slot_name")
        if card_locked and card is not None:
            fixed_assignment[slot_name] = card
        else:
            unlocked_slots.append(s)
            if card is not None:
                unlocked_cards.append(card)

    if not unlocked_cards:
        return list(slots)

    # Sort unlocked slots: primary position slots first, then Flex, then Superflex
    unlocked_slots_sorted = sorted(unlocked_slots, key=lambda s: _slot_flexibility_score(_get_attr(s, "slot_kind", "")))

    # Sort cards by kickoff timestamp ascending (earliest first)
    # Tie-break by salary descending
    unlocked_cards_sorted = sorted(
        unlocked_cards,
        key=lambda c: (
            _parse_kickoff_timestamp(_get_attr(c, "kickoff_time", "")),
            -_get_attr(c, "weekly_salary", 0),
        ),
    )

    best_score = [-1.0]
    best_assignment: list[dict[str, Any] | None] = [None]

    def backtrack(slot_idx: int, available_cards: list[Any], current_map: dict[str, Any], current_score: float) -> None:
        if slot_idx == len(unlocked_slots_sorted):
            if current_score > best_score[0]:
                best_score[0] = current_score
                best_assignment[0] = dict(current_map)
            return

        slot = unlocked_slots_sorted[slot_idx]
        skind = _get_attr(slot, "slot_kind", "")
        flex_weight = _slot_flexibility_score(skind)

        # For rigid slots (flex_weight == 0), prefer earlier cards first
        # For flex slots (flex_weight > 0), prefer later cards first
        indexed_candidates = list(enumerate(available_cards))
        if flex_weight > 0:
            indexed_candidates.reverse()

        for idx, card in indexed_candidates:
            raw_card = _get_attr(card, "card", card)
            pos = _get_attr(raw_card, "position", "")
            if _slot_accepts_pos(skind, pos):
                ko_ts = _parse_kickoff_timestamp(_get_attr(card, "kickoff_time", ""))
                # Scale kickoff timestamp so hours matter
                score_gain = (ko_ts / 3600.0) * flex_weight
                rem = available_cards[:idx] + available_cards[idx + 1:]
                sname = _get_attr(slot, "slot_name")
                current_map[sname] = card
                backtrack(slot_idx + 1, rem, current_map, current_score + score_gain)
                del current_map[sname]

    backtrack(0, unlocked_cards_sorted, {}, 0.0)

    if not best_assignment[0]:
        # Fallback: if backtracking found no match, return original slots
        return list(slots)

    # Reconstruct slots list in original slot ordering
    final_slots: list[Any] = []
    chosen_map = best_assignment[0]
    for s in slots:
        sname = _get_attr(s, "slot_name")
        new_card = fixed_assignment.get(sname) or chosen_map.get(sname)
        if isinstance(s, dict):
            new_s = dict(s)
            new_s["card"] = new_card
            final_slots.append(new_s)
        else:
            slot_class = s.__class__
            final_slots.append(
                slot_class(
                    slot_name=s.slot_name,
                    slot_kind=s.slot_kind,
                    card=new_card,
                    is_locked=s.is_locked,
                )
            )

    return final_slots

