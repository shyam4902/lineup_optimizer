"""Reproduce the two remaining V1 gaps on an isolated in-memory app state.

Run with: .venv/bin/python docs/review/correction/repro.py
No private roster data is used; the fixture is synthetic.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import app as app_module
from app import PlannerAppState, app
from dff_client import DFFPlayerProjection, ProjectionsSnapshot
from multi_lineup_planner import OwnedCard

RESULTS = []


def check(label, condition, detail=""):
    RESULTS.append(condition)
    print(f"{'PASS' if condition else 'FAIL'} {label}" + (f" :: {detail}" if detail else ""))


def build_state(root):
    state = PlannerAppState(root_dir=root)
    cards = [
        OwnedCard("qb1", "Patrick Mahomes", "patrickmahomes", "KC", "QB", 1.5, 7500, "Active", 1),
        OwnedCard("qb2", "Josh Allen", "joshallen", "BUF", "QB", 1.0, 8000, "Active", 2),
        OwnedCard("qb3", "Lamar Jackson", "lamarjackson", "BAL", "QB", 1.0, 7800, "Active", 9),
        OwnedCard("qb4", "Jalen Hurts", "jalenhurts", "PHI", "QB", 1.0, 7600, "Active", 10),
        OwnedCard("rb1", "Derrick Henry", "derrickhenry", "BAL", "RB", 1.0, 6500, "Active", 3),
        OwnedCard("rb2", "Breece Hall", "breecehall", "NYJ", "RB", 1.0, 6400, "Active", 4),
        OwnedCard("rb3", "Bijan Robinson", "bijanrobinson", "ATL", "RB", 1.0, 6300, "Active", 11),
        OwnedCard("rb4", "Saquon Barkley", "saquonbarkley", "PHI", "RB", 1.0, 6200, "Active", 12),
        OwnedCard("wr1", "Justin Jefferson", "justinjefferson", "MIN", "WR", 1.0, 7000, "Active", 5),
        OwnedCard("wr2", "CeeDee Lamb", "ceedeelamb", "DAL", "WR", 1.0, 7000, "Active", 6),
        OwnedCard("wr3", "Ja'Marr Chase", "jamarrchase", "CIN", "WR", 1.0, 6500, "Active", 13),
        OwnedCard("wr4", "Amon-Ra St. Brown", "amonrastbrown", "DET", "WR", 1.0, 6500, "Active", 14),
        OwnedCard("wr5", "Tyreek Hill", "tyreekhill", "MIA", "WR", 1.0, 6000, "Active", 15),
        OwnedCard("wr6", "Davante Adams", "davanteadams", "LV", "WR", 1.0, 5500, "Active", 16),
        OwnedCard("wr7", "A.J. Brown", "ajbrown", "PHI", "WR", 1.0, 6200, "Active", 17),
        OwnedCard("wr8", "Garrett Wilson", "garrettwilson", "NYJ", "WR", 1.0, 5800, "Active", 18),
        OwnedCard("te1", "Travis Kelce", "traviskelce", "KC", "TE", 1.0, 5500, "Active", 7),
        OwnedCard("te2", "Mark Andrews", "markandrews", "BAL", "TE", 1.0, 5000, "Active", 8),
        OwnedCard("te3", "Sam LaPorta", "samlaporta", "DET", "TE", 1.0, 4800, "Active", 19),
        OwnedCard("te4", "Trey McBride", "treymcbride", "ARI", "TE", 1.0, 4600, "Active", 20),
    ]
    players = [
        DFFPlayerProjection(c.player_name, c.athlete_key, c.team, c.position, c.roster_salary, 20.0, 20.0)
        for c in cards
    ]
    snapshot = ProjectionsSnapshot(
        slate_id="REPRO", source="repro", fetched_at="2026-09-12T00:00:00Z",
        total_raw_rows=len(players), offensive_player_count=len(players),
        positive_projection_count=len(players), dst_excluded_count=0,
        other_excluded_count=0, players=players, diagnostics=[],
    )
    state.roster_cards = cards
    state.snapshot = snapshot
    state.snapshot_store.save_snapshot(snapshot)
    state.recompute_planner_cards()
    return state


def main():
    app.config.update(TESTING=True, MAX_CONTENT_LENGTH=8 * 1024 * 1024)
    original_state = app_module.STATE
    tmp = tempfile.TemporaryDirectory()
    try:
        state = build_state(tmp.name)
        app_module.STATE = state
        client = app.test_client()

        print("--- Gap 1: explicit vs unknown remaining projection ---")
        state.active_plan = None
        response = client.post("/api/planner/generate", json={"contest_distribution": {"Scorcher": 1}})
        check("generate one entry", response.status_code == 200, str(response.status_code))
        akey = "patrickmahomes"

        # Unknown remaining: mark in progress with live points and no explicit estimate.
        client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "actual_score": 10.0,
        })
        live_payload = client.get("/api/live/state").get_json()["player_states"][akey]
        check("live state exposes remaining_is_estimate", "remaining_is_estimate" in live_payload,
              f"keys={sorted(live_payload.keys())}")
        check("unknown in-progress does not report a supplied estimate",
              live_payload.get("remaining_is_estimate") is False,
              f"value={live_payload.get('remaining_is_estimate')}")
        live = state.live_states[akey]
        check("in-progress unknown does not add the seeded projection",
              live.effective_mean(1.5) == 15.0, f"effective_mean={live.effective_mean(1.5)} expected 15.0")

        # Explicit zero is a known estimate.
        client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "remaining_projection": 0.0,
        })
        live = state.live_states[akey]
        check("explicit zero is a known estimate", getattr(live, "remaining_is_estimate", None) is True,
              f"flag={getattr(live, 'remaining_is_estimate', None)}")
        check("explicit zero keeps banked live points only", live.effective_mean(1.5) == 15.0,
              f"effective_mean={live.effective_mean(1.5)}")

        # Explicit positive estimate.
        client.post("/api/live/update_player_state", json={
            "athlete_key": akey, "game_status": "IN_PROGRESS", "remaining_projection": 4.0,
        })
        live = state.live_states[akey]
        check("positive estimate is added once", live.effective_mean(1.5) == 21.0,
              f"effective_mean={live.effective_mean(1.5)} expected 21.0")

        # Save, simulate a restart, load.
        client.post("/api/planner/save")
        app_module.STATE = build_state(tmp.name)
        restarted = app_module.STATE
        client = app.test_client()
        client.post("/api/planner/load")
        restored = restarted.live_states.get(akey)
        check("save/load keeps the explicit estimate across a restart",
              restored is not None and getattr(restored, "remaining_is_estimate", None) is True,
              f"flag={getattr(restored, 'remaining_is_estimate', None)}")
        check("save/load keeps the estimate value",
              restored is not None and restored.remaining_projection == 4.0,
              f"value={restored.remaining_projection if restored else None}")

        print("--- Gap 2: first manual entry without generation ---")
        app_module.STATE = build_state(tmp.name)
        fresh = app_module.STATE
        client = app.test_client()
        check("no active plan before the first manual entry", fresh.active_plan is None)
        response = client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Scorcher", "entry_limit": 2,
            "contest_distribution": {"Scorcher": 2},
        })
        check("first manual add succeeds without generation", response.status_code == 200,
              f"{response.status_code} {response.get_json().get('message')}")
        if fresh.active_plan:
            check("manual plan records the selected tier",
                  (fresh.active_plan.inventory_summary or {}).get("contest_distribution", {}).get("Scorcher") == 2,
                  str(fresh.active_plan.inventory_summary))
            check("manual plan holds one draft entry", len(fresh.active_plan.lineups) == 1,
                  f"count={len(fresh.active_plan.lineups)}")

        print("--- Gap 2: selected entry count below the contest maximum ---")
        # Fresh manual plan, Scorcher selected at 1 while the contest allows 5.
        app_module.STATE = build_state(tmp.name)
        limited = app_module.STATE
        client = app.test_client()
        client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Scorcher", "entry_limit": 1,
            "contest_distribution": {"Scorcher": 1},
        })
        first_count = len(limited.active_plan.lineups) if limited.active_plan else 0
        response = client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Scorcher", "entry_limit": 1,
            "contest_distribution": {"Scorcher": 1},
        })
        check("add past the selected count is rejected",
              response.status_code == 400,
              f"{response.status_code} {response.get_json().get('message')}")
        check("rejected add leaves the plan count intact",
              limited.active_plan is not None and len(limited.active_plan.lineups) == first_count,
              f"count={len(limited.active_plan.lineups) if limited.active_plan else None}")

        response = client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Scorcher", "entry_limit": 0,
            "contest_distribution": {"Scorcher": 0},
        })
        check("zero selected count is rejected", response.status_code == 400,
              f"{response.status_code} {response.get_json().get('message')}")

        response = client.post("/api/planner/edit_lineup", json={
            "action": "add", "contest_name": "Volcano", "entry_limit": 1,
            "contest_distribution": {"Scorcher": 1},
        })
        check("unselected tier is rejected", response.status_code == 400,
              f"{response.status_code} {response.get_json().get('message')}")
    finally:
        app_module.STATE = original_state
        tmp.cleanup()

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
