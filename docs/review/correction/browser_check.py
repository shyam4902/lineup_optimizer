"""Browser check for the two V1 corrections on the isolated preview (127.0.0.1:5097).

Phase 1: first manual draft, selected-count enforcement, and remaining-estimate
state through update, clear, reload, and save.
Phase 2: the same saved plan after an isolated server restart.

Run: .venv/bin/python docs/review/correction/browser_check.py 1
     (restart via docs/review/correction/restart_preview.sh) then
     .venv/bin/python docs/review/correction/browser_check.py 2
"""

import sys

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5097"
AUTH = {"username": "friend", "password": "correctionpass"}
PHASE = sys.argv[1] if len(sys.argv) > 1 else "1"
RESULTS = []
CONSOLE_ERRORS = []


def check(label, condition, detail=""):
    RESULTS.append(bool(condition))
    print(("PASS " if condition else "FAIL ") + label + (f" :: {detail}" if detail else ""))


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 900}, http_credentials=AUTH)
        page = context.new_page()
        page.on("console", lambda m: CONSOLE_ERRORS.append(m.text)
                if m.type == "error" and not m.text.startswith("Failed to load resource") else None)
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(f"pageerror: {e}"))

        def api_state():
            return context.request.get(f"{BASE}/api/planner/state").json()

        def api_live():
            return context.request.get(f"{BASE}/api/live/state").json()

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_selector("#roster-tbody tr")

        if PHASE == "1":
            run_phase_one(page, api_state, api_live)
        else:
            run_phase_two(page, api_state, api_live)
        context.close()
        browser.close()

    print(f"\n{sum(RESULTS)}/{len(RESULTS)} checks passed")
    if CONSOLE_ERRORS:
        print("console/page errors:")
        for err in sorted(set(CONSOLE_ERRORS)):
            print(f"  {err}")
    return 0 if all(RESULTS) and not CONSOLE_ERRORS else 1


def fill_slots(page, draft_id, count):
    """Fill `count` empty slots in a draft through the real Fill slot dialog."""
    for _ in range(count):
        row = page.locator(f"#lineup-entry-{draft_id} tbody tr",
                           has=page.locator("button", has_text="Fill slot")).first
        if row.count() == 0:
            return False
        row.locator("button", has_text="Fill slot").click()
        page.wait_for_selector("#swap-modal", state="visible")
        # The candidate fetch finishes after the dialog opens, so wait for the rows.
        try:
            page.wait_for_selector("#swap-candidates-tbody tr", timeout=8000)
        except Exception:
            page.locator("#swap-modal .modal-footer button").click()
            return False
        page.locator("#swap-candidates-tbody tr").first.locator("button").click()
        page.wait_for_timeout(900)
    return True


def set_score(page, draft_id, slot_index, status, points, remaining):
    page.locator(f"#lineup-entry-{draft_id} td .slot-actions button",
                 has_text="Score / status").nth(slot_index).click()
    page.wait_for_selector("#live-player-modal", state="visible")
    page.select_option("#live-modal-status", status)
    page.wait_for_timeout(200)
    if points is not None:
        page.fill("#live-modal-points", str(points))
    if remaining is not None:
        page.fill("#live-modal-rem-proj", "" if remaining == "" else str(remaining))
    page.locator("#live-player-modal .modal-footer button", has_text="Save Updates").click()
    page.wait_for_timeout(1000)


def run_phase_one(page, api_state, api_live):
    print("--- Phase 1: first manual draft (gap 2) ---")
    state = api_state()
    check("no active plan on the fresh instance", state["active_plan"] is None)
    check("the add-entry picker is usable with no plan", page.locator("#add-entry-contest").is_enabled())
    options = page.locator("#add-entry-contest option").all_inner_texts()
    check("picker lists the selected tiers", any("Scorcher" in o for o in options), ", ".join(options[:3]))

    # Selected count of 1, below Scorcher's maximum.
    page.fill('.contest-limit[data-contest="Scorcher"]', "1")
    page.select_option("#add-entry-contest", "Scorcher")
    page.locator(".workspace-toolbar button", has_text="Add empty entry").click()
    page.wait_for_selector(".lineup-entry")
    check("first manual draft created without generation",
          len(api_state()["active_plan"]["lineups"]) == 1,
          f"{len(api_state()['active_plan']['lineups'])} entries")
    plan = api_state()["active_plan"]
    draft_id = plan["lineups"][0]["lineup_id"]
    check("manual plan records the selected tier",
          (plan["inventory_summary"] or {}).get("contest_distribution", {}).get("Scorcher") == 1,
          str(plan["inventory_summary"]))
    entry = page.locator(f"#lineup-entry-{draft_id}")
    check("draft is labelled incomplete", entry.locator(".badge", has_text="Incomplete draft").count() == 1)
    check("draft is not labelled eliminated", entry.locator(".badge", has_text="Eliminated").count() == 0)

    # The selected count of 1 is already reached, so a second add is rejected.
    page.locator(".workspace-toolbar button", has_text="Add empty entry").click()
    page.wait_for_timeout(800)
    check("add past the selected count is rejected",
          len(api_state()["active_plan"]["lineups"]) == 1,
          page.locator("#workspace-status").inner_text())
    check("rejection explains the selected limit",
          "allowed entries" in page.locator("#workspace-status").inner_text(),
          page.locator("#workspace-status").inner_text())

    # Raise the selected count to 2 and add another draft.
    page.fill('.contest-limit[data-contest="Scorcher"]', "2")
    page.locator(".workspace-toolbar button", has_text="Add empty entry").click()
    page.wait_for_timeout(900)
    check("raising the selected count allows another draft",
          len(api_state()["active_plan"]["lineups"]) == 2,
          f"{len(api_state()['active_plan']['lineups'])} entries")

    # Fill two slots so two athletes can carry separate live states.
    check("manual draft fills slot by slot", fill_slots(page, draft_id, 2))
    lineup = next(l for l in api_state()["active_plan"]["lineups"] if l["lineup_id"] == draft_id)
    filled = [s["card"]["athlete_key"] for s in lineup["slots"] if s["card"]]
    check("two slots hold cards", len(filled) == 2, f"{len(filled)} filled")
    key_a, key_b = filled[0], filled[1]

    print("--- Phase 1: remaining estimate state (gap 1) ---")
    # Blank: in-progress with no estimate.
    set_score(page, draft_id, 0, "IN_PROGRESS", 8.5, "")
    live_a = api_live()["player_states"][key_a]
    check("blank estimate is stored as unknown", live_a["remaining_is_estimate"] is False,
          f"flag={live_a['remaining_is_estimate']}")
    entry = page.locator(f"#lineup-entry-{draft_id}")
    check("workspace shows unknown remaining after a blank save",
          entry.locator("td .cell-note", has_text="unknown").count() >= 1)

    page.reload(wait_until="networkidle")
    page.wait_for_selector(f"#lineup-entry-{draft_id}")
    entry = page.locator(f"#lineup-entry-{draft_id}")
    check("unknown remaining survives reload",
          entry.locator("td .cell-note", has_text="unknown").count() >= 1,
          entry.inner_text().replace("\n", " | ")[:120])
    entry.locator("td .slot-actions button", has_text="Score / status").first.click()
    page.wait_for_selector("#live-player-modal", state="visible")
    check("reopened dialog keeps the estimate blank",
          page.locator("#live-modal-rem-proj").input_value() == "",
          f"value={page.locator('#live-modal-rem-proj').input_value()!r}")
    page.locator("#live-player-modal .modal-footer button").first.click()
    page.wait_for_timeout(200)

    # Explicit zero is a known estimate.
    set_score(page, draft_id, 0, "IN_PROGRESS", 8.5, 0)
    live_a = api_live()["player_states"][key_a]
    check("explicit zero is a known estimate",
          live_a["remaining_is_estimate"] is True and live_a["remaining_projection"] == 0.0,
          f"flag={live_a['remaining_is_estimate']} value={live_a['remaining_projection']}")
    page.reload(wait_until="networkidle")
    page.wait_for_selector(f"#lineup-entry-{draft_id}")
    check("explicit zero survives reload",
          api_live()["player_states"][key_a]["remaining_is_estimate"] is True)

    # Positive estimate, then clear it back to unknown, then set it again for the restart.
    set_score(page, draft_id, 0, "IN_PROGRESS", 8.5, 4.0)
    live_a = api_live()["player_states"][key_a]
    check("positive estimate is stored and known",
          live_a["remaining_is_estimate"] is True and live_a["remaining_projection"] == 4.0,
          f"flag={live_a['remaining_is_estimate']} value={live_a['remaining_projection']}")
    set_score(page, draft_id, 0, "IN_PROGRESS", 8.5, "")
    check("clearing the estimate returns it to unknown",
          api_live()["player_states"][key_a]["remaining_is_estimate"] is False)
    set_score(page, draft_id, 0, "IN_PROGRESS", 8.5, 6.5)
    check("re-supplied estimate is known again",
          api_live()["player_states"][key_a]["remaining_projection"] == 6.5)

    # Second athlete stays final with zero remaining.
    set_score(page, draft_id, 1, "FINAL", 12.0, None)
    live_b = api_live()["player_states"][key_b]
    check("final keeps zero remaining projection",
          live_b["game_status"] == "FINAL" and live_b["remaining_projection"] == 0.0,
          f"status={live_b['game_status']} remaining={live_b['remaining_projection']}")

    page.locator(".workspace-toolbar button", has_text="Save plan").click()
    page.wait_for_timeout(900)
    check("save confirms the plan is on disk",
          "Saved" in page.locator("#save-state-badge").inner_text(),
          page.locator("#save-state-badge").inner_text())

    page.screenshot(path="/tmp/gb_correction_preview/phase1_manual.png", full_page=True)
    print(f"phase 1 estimate athlete for phase 2: {key_a}")
    print(f"phase 1 final athlete for phase 2: {key_b}")


def run_phase_two(page, api_state, api_live):
    print("--- Phase 2: after an isolated restart ---")
    state = api_state()
    check("restart starts with no in-memory plan", state["active_plan"] is None)
    page.locator(".workspace-toolbar button", has_text="Load plan").click()
    page.wait_for_timeout(1800)
    loaded = api_state()["active_plan"]
    check("load restores the saved plan",
          loaded is not None and len(loaded["lineups"]) == 2,
          f"{len(loaded['lineups']) if loaded else 0} entries")
    restored = api_live()["player_states"]
    estimate_players = [v for v in restored.values()
                        if v["remaining_is_estimate"] is True and v["remaining_projection"] == 6.5]
    check("explicit estimate survives the restart", len(estimate_players) == 1,
          f"{len(estimate_players)} players with a 6.5 explicit estimate")
    final_players = [v for v in restored.values()
                     if v["game_status"] == "FINAL" and v["final_points"] == 12.0
                     and v["remaining_projection"] == 0.0]
    check("final score and zero remaining survive the restart", len(final_players) == 1,
          f"{len(final_players)} final players at 12.0")
    page.wait_for_selector(".lineup-entry")
    check("workspace renders the loaded entries",
          page.locator("#lineups-container section.lineup-entry").count() == 2,
          f"{page.locator('#lineups-container section.lineup-entry').count()} entries")
    page.screenshot(path="/tmp/gb_correction_preview/phase2_after_restart.png", full_page=True)


if __name__ == "__main__":
    raise SystemExit(main())
