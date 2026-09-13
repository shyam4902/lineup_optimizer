"""Packet 4 acceptance check.

Drives the one-page workspace on an isolated copy of the private roster
(instance root /tmp/gb_p4_preview, port 5098) and records measured outcomes.

Phase 1 covers instance/roster verification, generation, manual construction,
the underperforming Final Thursday player, regeneration, and save/load.
Phase 2 runs after the isolated preview is restarted and compares the reloaded
plan against the snapshot written at the end of phase 1.
"""

from __future__ import annotations

import base64
import csv
import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path("/tmp/gb_p4_preview")
SHOTS = Path(__file__).resolve().parent
BASE = "http://127.0.0.1:5098"
USERNAME, PASSWORD = "friend", "packet4pass"
AUTH = "Basic " + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
THURSDAY_TEAMS = {"NE", "SEA", "SF", "LAR"}
TIERS = {"Scorcher": 2, "Volcano": 2, "Flex Appeal": 2, "Inferno": 2}
SNAPSHOT_FILE = ROOT / "packet4_snapshot.json"
SAVED_PLAN = ROOT / "data" / "saved_plans" / "latest_plan.json"

results: list[tuple[str, bool, str]] = []
console_errors: list[str] = []


def check(label: str, condition, detail: object = "") -> None:
    ok = bool(condition)
    results.append((label, ok, str(detail)))
    print(("PASS " if ok else "FAIL ") + label + (f" :: {detail}" if detail else ""), flush=True)


# --------------------------------------------------------------------------
# API helpers
# --------------------------------------------------------------------------
def api(method: str, path: str, body=None, timeout: int = 900):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", AUTH)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "{}")


def app_state() -> dict:
    return api("GET", "/api/planner/state")[1]


def active_plan() -> dict:
    return app_state()["active_plan"]


def live_state() -> dict:
    return api("GET", "/api/live/state")[1]


def slot_of(plan_json: dict, lineup_id: str, slot_name: str) -> dict:
    return next(x for x in slot_of_all(plan_json, lineup_id) if x["slot_name"] == slot_name)


def slot_of_all(plan_json: dict, lineup_id: str) -> list:
    return next(l for l in plan_json["lineups"] if l["lineup_id"] == lineup_id)["slots"]


def usage_by_card(plan_json: dict) -> dict:
    usage: dict[str, list[str]] = {}
    for l in plan_json["lineups"]:
        for s in l["slots"]:
            if s["card"]:
                usage.setdefault(s["card"]["card_id"], []).append(f"{l['lineup_id']}:{s['slot_name']}")
    return usage


def game_locked(slot: dict, live: dict) -> bool:
    card = slot.get("card")
    if not card:
        return False
    player = live["player_states"].get(card["athlete_key"])
    return bool(player and player["is_locked"])


def _scores(players: dict) -> dict:
    return {
        key: {
            "game_status": v["game_status"],
            "live_points": v["live_points"],
            "final_points": v["final_points"],
            "remaining_projection": v["remaining_projection"],
        }
        for key, v in players.items()
        if v["game_status"] != "UPCOMING" or v["live_points"] or v["final_points"]
    }


def snapshot(state_json: dict, live: dict | None) -> dict:
    plan_json = state_json["active_plan"]
    return {
        "lineup_order": [l["lineup_id"] for l in plan_json["lineups"]],
        "lineups": {
            l["lineup_id"]: {
                "contest": l["contest_name"],
                "slots": [[s["slot_name"], (s["card"] or {}).get("card_id"), bool(s["is_locked"])] for s in l["slots"]],
            }
            for l in plan_json["lineups"]
        },
        "excluded": sorted(state_json["excluded_athlete_keys"]),
        "tiers": plan_json["inventory_summary"].get("contest_distribution"),
        "salary_source": plan_json["salary_source"],
        "scores": _scores({k: v for k, v in (live or {}).get("player_states", {}).items()}),
    }


def snapshot_from_file(path: Path) -> dict:
    data = json.loads(Path(path).read_text())
    live = {p["athlete_key"]: p for p in (data.get("live_session") or {}).get("players", [])}
    return {
        "lineup_order": [l["lineup_id"] for l in data["lineups"]],
        "lineups": {
            l["lineup_id"]: {
                "contest": l["contest_name"],
                "slots": [[s["slot_name"], (s.get("card") or {}).get("card_id"), bool(s.get("is_locked"))] for s in l["slots"]],
            }
            for l in data["lineups"]
        },
        "excluded": sorted(data.get("excluded_athlete_keys", [])),
        "tiers": (data.get("inventory_summary") or {}).get("contest_distribution"),
        "salary_source": data.get("salary_source"),
        "scores": _scores(live),
    }


def diff_snapshots(a: dict, b: dict) -> list:
    return [key for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key)]


# --------------------------------------------------------------------------
# Browser helpers
# --------------------------------------------------------------------------
def wait_idle(page, timeout: int = 300000) -> None:
    page.wait_for_function("() => document.getElementById('workspace-status').dataset.tone !== 'pending'", timeout=timeout)
    page.wait_for_timeout(120)


def act(page, action) -> None:
    action()
    page.wait_for_timeout(150)
    wait_idle(page)


def dom_entries(page):
    return page.locator("#lineups-container section.lineup-entry")


def slot_row(page, plan_json: dict, lineup_id: str, slot_name: str):
    index = [s["slot_name"] for s in slot_of_all(plan_json, lineup_id)].index(slot_name)
    return page.locator(f"#lineup-entry-{lineup_id} tbody tr").nth(index)


def set_tiers(page, tiers: dict) -> None:
    page.evaluate(
        """(tiers) => {
            document.querySelectorAll('.contest-selected').forEach(cb => {
                cb.checked = Object.prototype.hasOwnProperty.call(tiers, cb.dataset.contest);
            });
            document.querySelectorAll('.contest-limit').forEach(inp => {
                if (Object.prototype.hasOwnProperty.call(tiers, inp.dataset.contest)) inp.value = tiers[inp.dataset.contest];
            });
        }""",
        tiers,
    )


def generate_all(page, tiers: dict) -> None:
    set_tiers(page, tiers)
    page.click("#generate-plan-btn")
    page.wait_for_timeout(200)
    wait_idle(page)


def roster_search(page, term: str) -> None:
    page.fill("#roster-search", term)
    page.wait_for_timeout(250)


def roster_row(page, player_name: str):
    return page.locator("#roster-tbody tr").filter(has_text=player_name).first


def pick_candidate(page, exclude_name: str | None = None, last: bool = False) -> str:
    """Click a swap candidate row that is not the card already in the slot."""
    rows = page.locator("#swap-candidates-tbody tr")
    chosen = None
    for i in range(rows.count()):
        row = rows.nth(i)
        name = row.locator("td").first.locator("strong").inner_text()
        if name != exclude_name:
            chosen = row
            if not last:
                break
    assert chosen is not None, "no swap candidate other than the current card"
    name = chosen.locator("td").first.locator("strong").inner_text()
    chosen.locator("button").click()
    return name


# --------------------------------------------------------------------------
# Phase 1
# --------------------------------------------------------------------------
def phase1(page) -> None:
    # --- instance root and roster identity ---
    listeners = subprocess.run(["lsof", "-nP", "-iTCP:5098", "-sTCP:LISTEN", "-t"],
                               capture_output=True, text=True).stdout.split()
    pid = listeners[0] if listeners else ""
    cwd = subprocess.run(["lsof", "-a", "-p", pid, "-d", "cwd", "-Fn"],
                         capture_output=True, text=True).stdout if pid else ""
    check("isolated preview is the only listener on 5098", len(listeners) == 1, f"pid {pid}")
    check("isolated preview runs from the app directory", "lineup_optimizer" in cwd,
          cwd.strip().replace("\n", " "))

    csv_rows = list(csv.DictReader((ROOT / "data" / "raw" / "My_roster.csv").open(encoding="utf-8-sig")))
    state0 = app_state()
    check("roster count matches the copied private input",
          state0["roster_count"] == len(csv_rows) == 331,
          f"input {len(csv_rows)} rows, served roster_count {state0['roster_count']}")
    served = sorted((c["player_name"], round(float(c["multiplier"]), 2)) for c in state0["cards"])
    copied = sorted((r["Player"], round(float(r["Multiplier"]), 2)) for r in csv_rows)
    check("served cards are the copied private roster, copy for copy", served == copied,
          f"{len(served)} served cards vs {len(copied)} input rows")

    # --- generate the selected tiers through the page ---
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector("#lineup-search")
    wait_idle(page)
    check("a fresh instance starts with no lineups", dom_entries(page).count() == 0,
          f"{dom_entries(page).count()} entries rendered")
    generate_all(page, TIERS)
    feedback = page.locator("#generation-feedback").inner_text()
    check("generation runs from the one-page workspace", feedback.startswith("Built"), feedback[:90])

    plan_json = active_plan()
    lineup_count = len(plan_json["lineups"])
    check("generation produced lineups", lineup_count > 0, f"{lineup_count} lineups")
    contest_names = {l["contest_name"] for l in plan_json["lineups"]}
    check("every lineup belongs to a selected tier", contest_names <= set(TIERS), sorted(contest_names))

    usage = usage_by_card(plan_json)
    check("no card copy is used twice across the plan", all(len(v) == 1 for v in usage.values()),
          {cid: v for cid, v in usage.items() if len(v) > 1})
    check("every card appears at most once inside its own lineup",
          all(len({s["card"]["card_id"] for s in l["slots"] if s["card"]}) == len([s for s in l["slots"] if s["card"]])
              for l in plan_json["lineups"]))

    contests_cfg = app_state()["contests"]
    bad_caps = {l["lineup_id"]: (l["total_salary"], contests_cfg[l["contest_name"]]["minimum_salary"],
                                 contests_cfg[l["contest_name"]]["maximum_salary"])
                for l in plan_json["lineups"]
                if not contests_cfg[l["contest_name"]]["minimum_salary"] <= l["total_salary"]
                <= contests_cfg[l["contest_name"]]["maximum_salary"]}
    check("every generated lineup respects its salary range", not bad_caps, bad_caps)
    check("every generated lineup is valid", all(l["is_valid"] for l in plan_json["lineups"]),
          [l["lineup_id"] for l in plan_json["lineups"] if not l["is_valid"]])

    slots_total = sum(len(l["slots"]) for l in plan_json["lineups"])
    check("every lineup is a separate workspace entry", dom_entries(page).count() == lineup_count,
          f"{dom_entries(page).count()} of {lineup_count}")
    check("every player row is visible without selecting a lineup",
          page.locator("#lineups-container tbody tr").count() == slots_total,
          f"{page.locator('#lineups-container tbody tr').count()} of {slots_total}")
    check("roster panel lists every owned copy", page.locator("#roster-tbody tr").count() == state0["roster_count"],
          page.locator("#roster-panel-count").inner_text())
    mix = page.locator("#entry-mix").inner_text()
    check("entries-by-contest row reflects the selected tiers", all(name in mix for name in TIERS),
          mix.replace("\n", " "))
    check("the workspace is one page next to the roster panel",
          page.locator("#sec-overview .roster-panel").count() == 1)

    page.fill("#lineup-search", "Inferno")
    page.wait_for_timeout(250)
    filtered = dom_entries(page).count()
    check("lineup search filters the workspace", 0 < filtered < lineup_count, f"{filtered} entries")
    check("lineup search keeps player rows", page.locator("#lineups-container tbody tr").count() > 0)
    page.fill("#lineup-search", "")
    page.wait_for_timeout(250)
    check("clearing the lineup search restores every entry", dom_entries(page).count() == lineup_count)

    nomatch = sorted({c["player_name"] for c in state0["cards"] if not c["is_matched"]})[0]
    roster_search(page, nomatch)
    hit = roster_row(page, nomatch)
    check("a roster row filtered out of generation is still searchable", hit.count() >= 1,
          f"{nomatch}: {hit.inner_text().replace(chr(10), ' | ')[:80]}")
    roster_search(page, "")
    page.select_option("#roster-pos-filter", "QB")
    page.wait_for_timeout(250)
    qb_rows = page.locator("#roster-tbody tr").count()
    check("roster position filter works", 0 < qb_rows < state0["roster_count"], f"{qb_rows} QB rows")
    page.select_option("#roster-pos-filter", "ALL")
    page.select_option("#roster-avail-filter", "ASSIGNED")
    page.wait_for_timeout(250)
    assigned_now = len(usage_by_card(active_plan()))
    check("roster assignment filter follows the live plan",
          page.locator("#roster-tbody tr").count() == assigned_now,
          f"{page.locator('#roster-tbody tr').count()} rows vs {assigned_now} assigned copies")
    page.select_option("#roster-avail-filter", "ALL")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "workspace_generated.png"))

    # --- manual construction ---
    by_contest: dict[str, list[str]] = {}
    for l in plan_json["lineups"]:
        by_contest.setdefault(l["contest_name"], []).append(l["lineup_id"])
    contest = next(name for name, ids in by_contest.items() if len(ids) >= 2)
    l1_id, l2_id = by_contest[contest][0], by_contest[contest][1]
    kinds1 = {s["slot_name"]: s["slot_kind"] for s in slot_of_all(plan_json, l1_id)}
    kinds2 = {s["slot_name"]: s["slot_kind"] for s in slot_of_all(plan_json, l2_id)}

    def exchangeable():
        for s in slot_of_all(active_plan(), l1_id):
            if not s["card"] or s["is_locked"] or s["card"]["team"] in THURSDAY_TEAMS:
                continue
            if s["slot_name"] not in kinds2 or kinds1[s["slot_name"]] != kinds2[s["slot_name"]]:
                continue
            other = next(x for x in slot_of_all(active_plan(), l2_id) if x["slot_name"] == s["slot_name"])
            if other["card"] and not other["is_locked"] and other["card"]["team"] not in THURSDAY_TEAMS:
                return s["slot_name"]
        return None

    slot_name = exchangeable()
    check("a legal cross-lineup exchange slot exists", slot_name is not None, f"{l1_id}/{l2_id} {slot_name}")
    before = active_plan()
    source_card = slot_of(before, l1_id, slot_name)["card"]
    dest_card = slot_of(before, l2_id, slot_name)["card"]
    act(page, lambda: slot_row(page, before, l1_id, slot_name).locator("button", has_text="Move").click())
    check("move/exchange dialog opens from the workspace", page.locator("#move-modal").is_visible())
    label = f"{l2_id.rsplit('_', 1)[0].capitalize()} #{l2_id.rsplit('_', 1)[1]}"
    destination = (page.locator("#move-destination-list .destination-row:not(.is-blocked)")
                   .filter(has_text=label).filter(has_text=f"{slot_name} ({kinds2[slot_name]})").first)
    dest_text = destination.inner_text().replace("\n", " | ")
    act(page, lambda: destination.locator("button").click())
    after = active_plan()
    check("cross-lineup exchange swaps both cards",
          slot_of(after, l1_id, slot_name)["card"]["card_id"] == dest_card["card_id"]
          and slot_of(after, l2_id, slot_name)["card"]["card_id"] == source_card["card_id"], dest_text)
    sal_before = (next(l for l in before["lineups"] if l["lineup_id"] == l1_id)["total_salary"],
                  next(l for l in before["lineups"] if l["lineup_id"] == l2_id)["total_salary"])
    sal_after = (next(l for l in after["lineups"] if l["lineup_id"] == l1_id)["total_salary"],
                 next(l for l in after["lineups"] if l["lineup_id"] == l2_id)["total_salary"])
    check("both entries report updated salaries", sal_before != sal_after,
          f"{l1_id} {sal_before[0]}->{sal_after[0]}, {l2_id} {sal_before[1]}->{sal_after[1]}")
    proj_before = (next(l for l in before["lineups"] if l["lineup_id"] == l1_id)["total_projection"],
                   next(l for l in before["lineups"] if l["lineup_id"] == l2_id)["total_projection"])
    proj_after = (next(l for l in after["lineups"] if l["lineup_id"] == l1_id)["total_projection"],
                  next(l for l in after["lineups"] if l["lineup_id"] == l2_id)["total_projection"])
    check("both entries report updated projected scores", proj_before != proj_after,
          f"{l1_id} {proj_before[0]}->{proj_after[0]}, {l2_id} {proj_before[1]}->{proj_after[1]}")
    check("both rendered entries show their new projected totals",
          f"{proj_after[0]:.1f} pts" in page.locator(f"#lineup-entry-{l1_id}").inner_text()
          and f"{proj_after[1]:.1f} pts" in page.locator(f"#lineup-entry-{l2_id}").inner_text(),
          f"projected {proj_after}")
    check("both exchange destinations are locked placements",
          slot_of(after, l1_id, slot_name)["is_locked"] and slot_of(after, l2_id, slot_name)["is_locked"])
    check("both entries stay valid after the exchange",
          next(l for l in after["lineups"] if l["lineup_id"] == l1_id)["is_valid"]
          and next(l for l in after["lineups"] if l["lineup_id"] == l2_id)["is_valid"])
    check("the page shows the exchanged players in both entries",
          slot_of(after, l1_id, slot_name)["card"]["player_name"] in page.locator(f"#lineup-entry-{l1_id}").inner_text()
          and slot_of(after, l2_id, slot_name)["card"]["player_name"] in page.locator(f"#lineup-entry-{l2_id}").inner_text())
    check("the workspace reports the exchange", "exchanged" in page.locator("#workspace-status").inner_text().lower(),
          page.locator("#workspace-status").inner_text())

    target = next(s for s in slot_of_all(after, l1_id)
                  if not s["is_locked"] and s["card"] and s["card"]["team"] not in THURSDAY_TEAMS)
    current_name = target["card"]["player_name"]
    act(page, lambda: slot_row(page, after, l1_id, target["slot_name"]).locator("button", has_text="Swap").click())
    check("swap dialog lists legal replacements", page.locator("#swap-candidates-tbody tr").count() > 0,
          page.locator("#swap-feedback").inner_text())
    chosen = {}
    act(page, lambda: chosen.update(name=pick_candidate(page, exclude_name=current_name)))
    after_swap = active_plan()
    swapped = slot_of(after_swap, l1_id, target["slot_name"])
    check("swap replaces the card and locks the destination",
          swapped["card"]["player_name"] == chosen["name"] and swapped["is_locked"],
          f"{target['slot_name']}: {current_name} -> {chosen['name']}")

    clear_target = next(s for s in slot_of_all(after_swap, l1_id) if not s["is_locked"] and s["card"])
    act(page, lambda: slot_row(page, after_swap, l1_id, clear_target["slot_name"]).locator("button", has_text="Clear").click())
    cleared = active_plan()
    check("clear empties an unlocked slot", slot_of(cleared, l1_id, clear_target["slot_name"])["card"] is None,
          page.locator("#workspace-status").inner_text())
    entry_text = page.locator(f"#lineup-entry-{l1_id}").inner_text()
    check("the empty slot stays editable and names what it needs",
          "Empty slot" in entry_text and "needs" in entry_text)
    check("the incomplete draft is not labelled eliminated",
          page.locator(f"#lineup-entry-{l1_id} .badge", has_text="Incomplete draft").count() == 1
          and page.locator(f"#lineup-entry-{l1_id} .badge", has_text="Eliminated").count() == 0)
    act(page, lambda: slot_row(page, cleared, l1_id, clear_target["slot_name"]).locator("button", has_text="Fill slot").click())
    check("fill slot lists candidates again", page.locator("#swap-candidates-tbody tr").count() > 0,
          page.locator("#swap-feedback").inner_text())
    refilled = {}
    act(page, lambda: refilled.update(name=pick_candidate(page)))
    refilled_plan = active_plan()
    refilled_slot = slot_of(refilled_plan, l1_id, clear_target["slot_name"])
    check("the refilled slot holds the chosen card and is locked",
          refilled_slot["card"] and refilled_slot["card"]["player_name"] == refilled["name"] and refilled_slot["is_locked"],
          f"{clear_target['slot_name']} -> {refilled['name']}")

    # --- exclusion ---
    assigned_card = next(s["card"] for s in slot_of_all(refilled_plan, l1_id) if s["is_locked"] and s["card"])
    roster_search(page, assigned_card["player_name"])
    act(page, lambda: roster_row(page, assigned_card["player_name"]).locator("button").click())
    excluded_state = app_state()
    still_assigned = next((s for l in excluded_state["active_plan"]["lineups"] for s in l["slots"]
                           if s["card"] and s["card"]["card_id"] == assigned_card["card_id"]), None)
    check("excluding an assigned player leaves the placement in place", still_assigned is not None,
          page.locator("#workspace-status").inner_text())
    check("excluding keeps the protected placement locked", bool(still_assigned and still_assigned["is_locked"]))
    check("the excluded athlete is recorded on the plan",
          assigned_card["athlete_key"] in excluded_state["excluded_athlete_keys"])
    roster_search(page, assigned_card["player_name"])
    act(page, lambda: roster_row(page, assigned_card["player_name"]).locator("button").click())
    check("including the assigned player restores them",
          assigned_card["athlete_key"] not in app_state()["excluded_athlete_keys"])
    roster_search(page, "")

    cards = app_state()["cards"]
    singles = {c["athlete_key"] for c in cards if sum(1 for x in cards if x["athlete_key"] == c["athlete_key"]) == 1}
    assigned_ids = set(usage_by_card(active_plan()))
    free_card = next(c for c in sorted(cards, key=lambda c: c["player_name"])
                     if c["is_eligible"] and c["is_matched"] and c["card_id"] not in assigned_ids
                     and c["athlete_key"] in singles and c["athlete_key"] not in app_state()["excluded_athlete_keys"])

    def slot_for(card):
        for l in active_plan()["lineups"]:
            for s in l["slots"]:
                if s["is_locked"]:
                    continue
                kind = s["slot_kind"].upper()
                if kind == card["position"].upper() or (kind == "FLEX" and card["position"].upper() in {"RB", "WR", "TE"}) \
                        or (kind == "SUPERFLEX" and card["position"].upper() in {"QB", "RB", "WR", "TE"}):
                    return l["lineup_id"], s["slot_name"]
        return None, None

    open_lineup, open_slot = slot_for(free_card)
    check("an unassigned single-copy card has a legal slot", open_slot is not None,
          f"{free_card['player_name']} ({free_card['position']}) -> {open_lineup}/{open_slot}")
    roster_search(page, free_card["player_name"])
    act(page, lambda: roster_row(page, free_card["player_name"]).locator("button").click())
    roster_search(page, "")
    plan_now = active_plan()
    act(page, lambda: slot_row(page, plan_now, open_lineup, open_slot).locator("button", has_text="Swap").click())
    offered_after_exclude = free_card["player_name"] in page.locator("#swap-candidates-tbody").inner_text()
    check("an excluded player is not offered as a replacement", not offered_after_exclude,
          f"{free_card['player_name']}: {page.locator('#swap-feedback').inner_text()}")
    act(page, lambda: page.locator("#swap-modal .modal-footer button").click())
    page.wait_for_timeout(250)
    roster_search(page, free_card["player_name"])
    act(page, lambda: roster_row(page, free_card["player_name"]).locator("button").click())
    roster_search(page, "")
    plan_now = active_plan()
    act(page, lambda: slot_row(page, plan_now, open_lineup, open_slot).locator("button", has_text="Swap").click())
    check("including the player restores them as a replacement",
          free_card["player_name"] in page.locator("#swap-candidates-tbody").inner_text(),
          page.locator("#swap-feedback").inner_text())
    act(page, lambda: page.locator("#swap-modal .modal-footer button").click())
    page.wait_for_timeout(250)

    # --- schedule import and the underperforming Final Thursday player ---
    code, sched = api("POST", "/api/live/import_dashboard_schedule", {"season": 2026, "week": 1})
    check("dashboard schedule imports for 2026 week 1", code == 200 and sched.get("status") == "success",
          sched.get("message") or sched.get("schedule"))
    live = live_state()
    thu_count = len([v for v in live["player_states"].values() if v["team"] in THURSDAY_TEAMS and v["kickoff_time"]])
    check("Thursday kickoff times reach the roster", thu_count > 0, f"{thu_count} Thursday-team players with kickoff times")

    plan_now = active_plan()
    pick = None
    for l in plan_now["lineups"]:
        for s in l["slots"]:
            if not s["card"] or s["is_locked"] or s["card"]["team"] not in THURSDAY_TEAMS:
                continue
            others = [x for x in l["slots"]
                      if x["slot_name"] != s["slot_name"] and not x["is_locked"] and x["card"]
                      and x["card"]["team"] not in THURSDAY_TEAMS]
            if len(others) >= 3:
                pick = (l["lineup_id"], s, others)
                break
        if pick:
            break
    check("a Thursday player sits in a slot with free neighbours", pick is not None,
          f"{pick[0]} {pick[1]['slot_name']} {pick[1]['card']['player_name']}" if pick else "none found")
    thu_lineup, thu_slot, thu_others = pick
    thu_key, thu_card = thu_slot["card"]["athlete_key"], thu_slot["card"]
    check("the Thursday player is game-locked by the real kickoff time",
          game_locked(thu_slot, live), f"kickoff {live['player_states'][thu_key]['kickoff_time']}")

    payout_before_score = active_plan()["total_weekly_estimated_payout"]
    act(page, lambda: slot_row(page, active_plan(), thu_lineup, thu_slot["slot_name"])
        .locator("button", has_text="Score / status").click())
    check("score dialog separates pregame projection, multiplier and score entry",
          "Pregame raw projection" in page.locator("#live-modal-readout").inner_text()
          and "multiplier" in page.locator("#live-modal-readout").inner_text(),
          page.locator("#live-modal-readout").inner_text().replace("\n", " | "))

    def save_low_final():
        page.select_option("#live-modal-status", "FINAL")
        page.fill("#live-modal-points", "2")
        page.locator("#live-player-modal .modal-footer button", has_text="Save Updates").click()

    act(page, save_low_final)
    final_player = live_state()["player_states"][thu_key]
    check("Final status stores raw points and applies the multiplier once",
          final_player["game_status"] == "FINAL" and final_player["final_points"] == 2.0,
          f"{thu_card['player_name']} {thu_card['multiplier']}x -> {round(2.0 * float(thu_card['multiplier']), 2)} lineup points")
    check("Final status leaves zero remaining projection", final_player["remaining_projection"] == 0.0,
          f"remaining_projection={final_player['remaining_projection']}")
    entry_text = page.locator(f"#lineup-entry-{thu_lineup}").inner_text()
    check("the workspace shows the game lock and the raw x multiplier math",
          "Game started" in entry_text and f"x{float(thu_card['multiplier']):.2f}" in entry_text)
    check("the Final slot is protected by the game lock, not a manual lock",
          not slot_of(active_plan(), thu_lineup, thu_slot["slot_name"])["is_locked"],
          "slot.is_locked is false; live state carries the protection")
    check("entering a score alone does not recompute payout estimates",
          payout_before_score == active_plan()["total_weekly_estimated_payout"],
          f"payout {payout_before_score} -> {active_plan()['total_weekly_estimated_payout']}")

    manual_target = thu_others[0]
    existing_name = manual_target["card"]["player_name"]
    act(page, lambda: slot_row(page, active_plan(), thu_lineup, manual_target["slot_name"])
        .locator("button", has_text="Swap").click())
    manual = {}
    act(page, lambda: manual.update(name=pick_candidate(page, exclude_name=existing_name, last=True)))
    after_manual = active_plan()
    check("another available player can be chosen manually in the same entry",
          slot_of(after_manual, thu_lineup, manual_target["slot_name"])["card"]["player_name"] == manual["name"],
          f"{manual_target['slot_name']}: {existing_name} -> {manual['name']}")
    check("the Final player is untouched by that manual choice",
          slot_of(after_manual, thu_lineup, thu_slot["slot_name"])["card"]["card_id"] == thu_card["card_id"])

    before_rebuild = {s["slot_name"]: (s["card"] or {}).get("card_id") for s in slot_of_all(after_manual, thu_lineup)}
    act(page, lambda: page.locator(f"#lineup-entry-{thu_lineup} .lineup-actions button",
                                   has_text="Rebuild unlocked slots").click())
    after_rebuild = active_plan()
    rebuilt = {s["slot_name"]: (s["card"] or {}).get("card_id") for s in slot_of_all(after_rebuild, thu_lineup)}
    changed = sorted(name for name in rebuilt if rebuilt[name] != before_rebuild.get(name))
    check("rebuild keeps the Final Thursday player fixed in place",
          rebuilt[thu_slot["slot_name"]] == thu_card["card_id"],
          f"{thu_slot['slot_name']} still {slot_of(after_rebuild, thu_lineup, thu_slot['slot_name'])['card']['player_name']}")
    check("rebuild changes the other unlocked slots", len(changed) > 0, f"changed {changed}")
    check("the Final score survives the rebuild", live_state()["player_states"][thu_key]["final_points"] == 2.0)
    live_now = live_state()
    check("no automatic rescue or reallocation is produced",
          live_now.get("reallocation_plan") is None, live_now.get("reallocation_plan"))
    check("no payout or rescue panel appears in the primary flow",
          page.locator("#reallocation-results").is_hidden()
          and not page.locator("details.advanced-block").first.get_attribute("open"))
    check("the underperforming entry is not labelled eliminated",
          page.locator(".badge", has_text="Eliminated").count() == 0)

    # --- regenerate all ---
    locks_before = {l["lineup_id"]: [s["slot_name"] for s in l["slots"] if s["is_locked"]]
                    for l in active_plan()["lineups"]}
    ids_before = [l["lineup_id"] for l in active_plan()["lineups"]]
    generate_all(page, TIERS)
    after_regen = active_plan()
    regen_slot = slot_of(after_regen, thu_lineup, thu_slot["slot_name"])
    check("regenerate all preserves the Final Thursday player",
          regen_slot["card"] is not None and regen_slot["card"]["card_id"] == thu_card["card_id"],
          f"{thu_lineup} {thu_slot['slot_name']} -> "
          f"{regen_slot['card']['player_name'] if regen_slot['card'] else 'empty'}")
    check("regenerate all keeps the lineup IDs", set(ids_before) <= {l["lineup_id"] for l in after_regen["lineups"]},
          f"{sorted(set(ids_before) - {l['lineup_id'] for l in after_regen['lineups']})} lost")
    locks_after = {l["lineup_id"]: {s["slot_name"] for s in l["slots"] if s["is_locked"]}
                   for l in after_regen["lineups"]}
    lost_locks = {lid: sorted(set(names) - locks_after.get(lid, set()))
                  for lid, names in locks_before.items() if names and set(names) - locks_after.get(lid, set())}
    check("regenerate all preserves every locked placement", not lost_locks, lost_locks)
    check("regenerate all preserves the Final score",
          live_state()["player_states"][thu_key]["final_points"] == 2.0,
          live_state()["player_states"][thu_key]["final_points"])
    check("regenerate all keeps every lineup inside the selected tiers",
          {l["contest_name"] for l in after_regen["lineups"]} <= set(TIERS),
          sorted({l["contest_name"] for l in after_regen["lineups"]}))
    usage_after = usage_by_card(after_regen)
    check("regenerate all still reuses no card copy", all(len(v) == 1 for v in usage_after.values()),
          {cid: v for cid, v in usage_after.items() if len(v) > 1})
    regen_bad_caps = {l["lineup_id"]: l["total_salary"] for l in after_regen["lineups"]
                      if not contests_cfg[l["contest_name"]]["minimum_salary"] <= l["total_salary"]
                      <= contests_cfg[l["contest_name"]]["maximum_salary"]}
    check("regenerate all respects every salary range", not regen_bad_caps, regen_bad_caps)
    check("regenerate all leaves every entry valid", all(l["is_valid"] for l in after_regen["lineups"]),
          [l["lineup_id"] for l in after_regen["lineups"] if not l["is_valid"]])
    check("every entry is visible after regeneration",
          dom_entries(page).count() == len(after_regen["lineups"]),
          f"{dom_entries(page).count()} of {len(after_regen['lineups'])}")

    # --- save and load ---
    planned = snapshot(app_state(), live_state())
    act(page, lambda: page.locator(".workspace-toolbar button", has_text="Save plan").click())
    check("save reports a saved copy", page.locator("#save-state-badge").inner_text().startswith("Saved"),
          page.locator("#save-state-badge").inner_text())
    check("the saved plan lands in the isolated instance root", SAVED_PLAN.exists(), str(SAVED_PLAN))
    saved_snapshot = snapshot_from_file(SAVED_PLAN)
    check("the saved file matches the live workspace state", not diff_snapshots(planned, saved_snapshot),
          diff_snapshots(planned, saved_snapshot))

    mutable = next((l["lineup_id"], s) for l in active_plan()["lineups"] for s in l["slots"]
                   if s["card"] and not s["is_locked"] and not game_locked(s, live_state()))
    mutable_lineup, mutable_slot = mutable
    act(page, lambda: slot_row(page, active_plan(), mutable_lineup, mutable_slot["slot_name"])
        .locator("button", has_text="Clear").click())
    check("the unsaved edit changes the plan", active_plan() != after_regen)
    act(page, lambda: page.locator(".workspace-toolbar button", has_text="Load plan").click())
    reloaded = snapshot(app_state(), live_state())
    check("load restores placements, locks, scores, exclusions and tiers",
          not diff_snapshots(saved_snapshot, reloaded), diff_snapshots(saved_snapshot, reloaded))
    check("every lineup is rendered again after load",
          dom_entries(page).count() == len(saved_snapshot["lineups"]),
          f"{dom_entries(page).count()} of {len(saved_snapshot['lineups'])}")

    page.screenshot(path=str(SHOTS / "workspace_after_packet4_flow.png"), full_page=True)
    SNAPSHOT_FILE.write_text(json.dumps(saved_snapshot, indent=2))
    check("the restart comparison snapshot was written", SNAPSHOT_FILE.exists(), str(SNAPSHOT_FILE))
    check("no page or console script errors during the flow", not console_errors, "; ".join(console_errors[:3]))


# --------------------------------------------------------------------------
# Phase 2: after the isolated preview restarts
# --------------------------------------------------------------------------
def phase2(page) -> None:
    state0 = app_state()
    check("a restarted instance starts with no active plan", state0["active_plan"] is None,
          f"active_plan={'present' if state0['active_plan'] else 'None'}")
    check("the restarted instance reloads the private roster", state0["roster_count"] == 331, state0["roster_count"])
    code, data = api("POST", "/api/planner/load")
    check("load after restart succeeds", code == 200 and data.get("status") == "success", data.get("message"))
    check("no saved card was missing from the roster", not data.get("missing_cards"), data.get("missing_cards"))
    expected = json.loads(SNAPSHOT_FILE.read_text())
    differences = diff_snapshots(expected, snapshot(app_state(), live_state()))
    check("placements, locks, scores, exclusions and tiers match the saved snapshot",
          not differences, differences)
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".lineup-entry")
    wait_idle(page)
    check("every lineup is visible after the restart",
          dom_entries(page).count() == len(expected["lineups"]),
          f"{dom_entries(page).count()} of {len(expected['lineups'])}")
    check("the workspace still shows the Final score math after the restart",
          any("Game started" in el for el in page.locator("#lineups-container tbody tr").all_inner_texts()))
    page.screenshot(path=str(SHOTS / "workspace_after_restart.png"))
    check("no page or console script errors after the restart", not console_errors, "; ".join(console_errors[:3]))


def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "1"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900},
                                      http_credentials={"username": USERNAME, "password": PASSWORD})

        def note_console(message):
            if message.type == "error" and not message.text.startswith("Failed to load resource"):
                console_errors.append(message.text)

        page = context.new_page()
        page.on("console", note_console)
        page.on("pageerror", lambda exc: console_errors.append(f"pageerror: {exc}"))
        try:
            (phase1 if phase == "1" else phase2)(page)
        finally:
            browser.close()

    failed = [r for r in results if not r[1]]
    print("\n--- summary ---", flush=True)
    print(f"phase {phase}: {len(results) - len(failed)}/{len(results)} checks passed")
    for label, _, detail in failed:
        print("FAILED:", label, "::", detail)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
