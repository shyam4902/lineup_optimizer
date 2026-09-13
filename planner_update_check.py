"""Runnable slate and projection update check for GameBlazers weekly planner.

Distinguishes:
1. NEW_SLATE: A new playable weekly slate is available.
2. PROJECTIONS_CHANGED: Projections or salaries have updated on the existing slate.
3. NO_CHANGE: Data is identical to the last good snapshot.
4. FAILED: Extraction or network connection failed without corrupting previous good snapshot.

Can be run via CLI:
    .venv/bin/python planner_update_check.py
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dff_client import (
    SnapshotStore,
    compare_snapshots,
    fetch_dff_player_details,
    fetch_dff_slates,
    parse_dff_players,
)


def run_weekly_update_check(
    slate_id: str | None = None,
    store_dir: str = "data/snapshots",
    persist: bool = True,
) -> dict[str, Any]:
    """Execute update check and return structured result."""
    store = SnapshotStore(store_dir)
    old_snapshot = store.load_latest()
    overrides = store.load_overrides()

    try:
        slates = fetch_dff_slates()
    except Exception as exc:
        return {
            "status": "FAILED",
            "message": f"Failed to fetch slates from DFF: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "has_previous_snapshot": old_snapshot is not None,
        }

    if not slates:
        return {
            "status": "FAILED",
            "message": "DFF returned empty slate list.",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "has_previous_snapshot": old_snapshot is not None,
        }

    # Select target slate
    selected_slate = None
    if slate_id:
        selected_slate = next((s for s in slates if s.slate_id == slate_id), None)
        if not selected_slate:
            # Maybe a custom or non-listed slate
            from dff_client import SlateInfo
            selected_slate = SlateInfo(slate_id, "Custom", 0, 0, "")
    else:
        # Default to first non-showdown or multi-game slate
        multi_game = [s for s in slates if s.game_count > 1 and s.showdown_flag == 0]
        selected_slate = multi_game[0] if multi_game else slates[0]

    try:
        raw_players = fetch_dff_player_details(selected_slate.slate_id)
        if len(raw_players) < 20:
            raise ValueError(f"Extracted row count ({len(raw_players)}) is suspiciously low.")
        new_snapshot = parse_dff_players(raw_players, selected_slate.slate_id, selected_slate, overrides)
    except Exception as exc:
        return {
            "status": "FAILED",
            "message": f"Failed to extract or validate players for slate {selected_slate.slate_id}: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "has_previous_snapshot": old_snapshot is not None,
        }

    comparison = compare_snapshots(old_snapshot, new_snapshot)

    # Persist if valid and not failed
    if persist and comparison["status"] in ("NEW_SLATE", "PROJECTIONS_CHANGED", "INITIAL_SNAPSHOT"):
        store.save_snapshot(new_snapshot)

    result = {
        "status": comparison["status"],
        "message": comparison["message"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "slate_id": selected_slate.slate_id,
        "slate_type": selected_slate.slate_type,
        "game_count": selected_slate.game_count,
        "total_players": new_snapshot.offensive_player_count,
        "changed_player_count": comparison["changed_player_count"],
        "max_delta": comparison["max_delta"],
        "changed_sample": comparison["changed_players"][:5],
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run GameBlazers weekly slate & projection update check.")
    parser.add_argument("--slate", default=None, help="Target slate ID (optional, defaults to primary multi-game slate)")
    parser.add_argument("--dir", default="data/snapshots", help="Directory for snapshot storage")
    args = parser.parse_args()

    result = run_weekly_update_check(slate_id=args.slate, store_dir=args.dir, persist=True)
    print(json.dumps(result, indent=2))
    if result["status"] == "FAILED":
        sys.exit(1)


if __name__ == "__main__":
    main()
