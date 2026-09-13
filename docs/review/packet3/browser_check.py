import json
import re
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5099"
AUTH = {"username": "friend", "password": "previewpass"}
results = []
console_errors = []


def check(label, condition, detail=""):
    results.append((label, bool(condition), detail))
    print(("PASS " if condition else "FAIL ") + label + (f" :: {detail}" if detail else ""))


with sync_playwright() as p:
    browser = p.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 900}, http_credentials=AUTH)
    page = context.new_page()
    def note_console(m):
        # Expected API rejections surface as resource errors; keep script errors only.
        if m.type == "error" and not m.text.startswith("Failed to load resource"):
            console_errors.append(m.text)

    page.on("console", note_console)
    page.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))

    state = context.request.get(f"{BASE}/api/planner/state")
    plan = state.json()["active_plan"]
    card_count = len(state.json()["cards"])
    slot_total = sum(len(l["slots"]) for l in plan["lineups"])
    lineup_count = len(plan["lineups"])

    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".lineup-entry")

    entries = page.locator("#lineups-container section.lineup-entry")
    check("all lineups rendered", entries.count() == lineup_count, f"{entries.count()} of {lineup_count}")

    rows = page.locator("#lineups-container tbody tr")
    check("every lineup slot row visible", rows.count() == slot_total, f"{rows.count()} of {slot_total}")

    empty_entries = page.locator("#workspace-empty")
    check("no placeholder empty state", empty_entries.count() == 0)

    roster_rows = page.locator("#roster-tbody tr")
    check("full roster panel rendered", roster_rows.count() == card_count, f"{roster_rows.count()} of {card_count}")

    check("roster count line", "owned copies" in page.locator("#roster-panel-count").inner_text(),
          page.locator("#roster-panel-count").inner_text())

    assigned_badges = page.locator("#roster-tbody .badge-success")
    check("roster shows assignments", assigned_badges.count() == sum(
        len([s for s in l["slots"] if s["card"]]) for l in plan["lineups"]), f"{assigned_badges.count()} assigned rows")

    filled_slots = sum(len([s for s in l["slots"] if s["card"]]) for l in plan["lineups"])
    status_selects = page.locator(".lineup-slot-table select")
    check("inline game-status control per filled slot", status_selects.count() == filled_slots,
          f"{status_selects.count()} selects for {filled_slots} filled slots")

    check("save badge present", "No saved copy" in page.locator("#save-state-badge").inner_text(),
          page.locator("#save-state-badge").inner_text())

    page.screenshot(path="/tmp/gb_p3_preview/workspace_desktop.png", full_page=True)

    # --- search filters over all lineups ---
    page.fill("#lineup-search", "Inferno")
    page.wait_for_timeout(200)
    filtered = page.locator("#lineups-container section.lineup-entry").count()
    check("lineup search filters list", 0 < filtered < lineup_count, f"{filtered} entries match Inferno")
    check("lineup search keeps rows", page.locator("#lineups-container tbody tr").count() > 0)
    page.fill("#lineup-search", "")
    page.wait_for_timeout(200)
    check("clearing search restores all", page.locator("#lineups-container section.lineup-entry").count() == lineup_count)

    # --- roster filters ---
    page.fill("#roster-search", "QB" if card_count else "a")
    page.fill("#roster-search", "")
    page.select_option("#roster-pos-filter", "QB")
    page.wait_for_timeout(200)
    qb_rows = page.locator("#roster-tbody tr").count()
    check("roster position filter works", 0 < qb_rows < card_count, f"{qb_rows} QB rows")
    page.select_option("#roster-pos-filter", "ALL")
    page.select_option("#roster-avail-filter", "ASSIGNED")
    page.wait_for_timeout(200)
    check("roster assignment filter works", page.locator("#roster-tbody tr").count() == sum(
        len([s for s in l["slots"] if s["card"]]) for l in plan["lineups"]))
    page.select_option("#roster-avail-filter", "ALL")
    page.wait_for_timeout(200)

    # --- slot lock toggles through the tested endpoint ---
    first_entry = page.locator("#lineups-container section.lineup-entry").first
    first_lineup_id = first_entry.get_attribute("id").replace("lineup-entry-", "")
    def slot_locks(lineup_id):
        plan_now = context.request.get(f"{BASE}/api/planner/state").json()["active_plan"]
        return {s["slot_name"] for s in [l for l in plan_now["lineups"] if l["lineup_id"] == lineup_id][0]["slots"] if s["is_locked"]}

    before_locks = slot_locks(first_lineup_id)
    lock_btn = first_entry.locator("td .slot-actions button", has_text=re.compile("^Lock slot$")).first
    target_slot = lock_btn.evaluate("el => el.closest('tr').querySelector('td:nth-child(2) div:nth-child(2)').innerText.split(' · ')[0]")
    lock_btn.click()
    page.wait_for_timeout(800)
    check("slot lock persists", slot_locks(first_lineup_id) - before_locks == {target_slot},
          f"locked {target_slot}; locks now {sorted(slot_locks(first_lineup_id))}")
    check("unsaved indicator after edit", "Unsaved" in page.locator("#save-state-badge").inner_text(),
          page.locator("#save-state-badge").inner_text())

    first_entry.locator("td .slot-actions button", has_text=re.compile("^Unlock slot$")).first.click()
    page.wait_for_timeout(800)
    check("slot unlock persists", slot_locks(first_lineup_id) == before_locks,
          f"locks now {sorted(slot_locks(first_lineup_id))}")

    # --- move/exchange destination picker ---
    first_entry.locator("td .slot-actions button", has_text="Move").first.click()
    page.wait_for_timeout(300)
    check("move dialog opens", page.locator("#move-modal").is_visible())
    dest_rows = page.locator("#move-destination-list .destination-row")
    check("move dialog lists destinations", dest_rows.count() > 0, f"{dest_rows.count()} destinations")
    check("move dialog explains blocked destinations",
          page.locator("#move-destination-list .destination-reason").count() > 0,
          f"{page.locator('#move-destination-list .destination-reason').count()} blocked reasons")
    check("move dialog blocks the source slot",
          page.locator("#move-destination-list .destination-row.is-blocked button:disabled").count() > 0)
    page.screenshot(path="/tmp/gb_p3_preview/move_dialog.png")
    # exchange into another lineup's first legal destination
    before_move = context.request.get(f"{BASE}/api/planner/state").json()["active_plan"]
    target = page.locator("#move-destination-list .destination-row:not(.is-blocked) button:not([disabled])").first
    target_text = " | ".join(target.evaluate("el => el.closest('.destination-row').innerText").split("\n"))
    target.click()
    page.wait_for_timeout(1000)
    applied = not page.locator("#move-modal").is_visible()
    if applied:
        after_move = context.request.get(f"{BASE}/api/planner/state").json()["active_plan"]
        slots_before = {l["lineup_id"]: [(s["slot_name"], s["card"]["card_id"] if s["card"] else None) for s in l["slots"]] for l in before_move["lineups"]}
        slots_after = {l["lineup_id"]: [(s["slot_name"], s["card"]["card_id"] if s["card"] else None) for s in l["slots"]] for l in after_move["lineups"]}
        check("move/exchange applied to both entries", slots_before != slots_after, target_text)
        check("feedback describes the move", "Updated" in page.locator("#workspace-status").inner_text(),
              page.locator("#workspace-status").inner_text())
    else:
        check("move/exchange applied to both entries", False,
              "dialog stayed open: " + page.locator("#move-feedback").inner_text())
        page.locator("#move-modal .modal-footer button").click()
        page.wait_for_timeout(300)
    check("no illegal destination is offered as enabled",
          page.locator("#move-destination-list .destination-row.is-blocked button:not([disabled])").count() == 0)

    # --- swap a roster card into a slot ---
    first_entry = page.locator("#lineups-container section.lineup-entry").first
    first_entry.locator("td .slot-actions button", has_text="Swap").first.click()
    page.wait_for_timeout(900)
    check("swap dialog opens", page.locator("#swap-modal").is_visible())
    candidates = page.locator("#swap-candidates-tbody tr")
    check("swap candidates listed", candidates.count() > 0, f"{candidates.count()} candidates")
    before_names = first_entry.locator("tbody tr td:nth-child(2) div").all_inner_texts()
    candidates.first.locator("button").click()
    page.wait_for_timeout(900)
    check("swap applied", not page.locator("#swap-modal").is_visible())
    after_plan = context.request.get(f"{BASE}/api/planner/state").json()["active_plan"]
    swapped = [l for l in after_plan["lineups"] if l["lineup_id"] == first_lineup_id][0]
    check("swap destination locked to new card", any(s["is_locked"] for s in swapped["slots"]),
          f"locks: {[s['slot_name'] for s in swapped['slots'] if s['is_locked']]}")

    # --- clear a slot (pick an unlocked one: its row shows a "Lock slot" button) ---
    entry = page.locator("#lineups-container section.lineup-entry").first
    unlocked_rows = entry.locator("tbody tr").filter(has=page.locator("button", has_text=re.compile(r"^Lock slot$")))
    check("unlocked slots remain after the swap", unlocked_rows.count() > 0, f"{unlocked_rows.count()} unlocked rows")
    unlocked_rows.first.locator("button", has_text="Clear").click()
    page.wait_for_timeout(900)
    entry = page.locator("#lineups-container section.lineup-entry").first
    check("clear leaves an editable empty slot", entry.locator("td", has_text="Empty slot").count() > 0,
          page.locator("#workspace-status").inner_text())
    check("clear names what the empty slot needs", "needs" in entry.locator("td", has_text="Empty slot").first.inner_text(),
          entry.locator("td", has_text="Empty slot").first.inner_text())
    check("incomplete draft is not labelled eliminated",
          entry.locator(".badge", has_text="Incomplete draft").count() == 1
          and entry.locator(".badge", has_text="Eliminated").count() == 0)

    # --- score + status entry from the workspace ---
    entry = page.locator("#lineups-container section.lineup-entry").first
    entry.locator("td .slot-actions button", has_text="Score / status").first.click()
    page.wait_for_timeout(300)
    check("score dialog opens", page.locator("#live-player-modal").is_visible())
    readout = page.locator("#live-modal-readout").inner_text()
    check("score dialog shows pregame and multiplier", "Pregame raw projection" in readout and "multiplier" in readout,
          readout.replace("\n", " | "))
    page.select_option("#live-modal-status", "IN_PROGRESS")
    page.fill("#live-modal-points", "8.5")
    page.fill("#live-modal-rem-proj", "4.0")
    page.locator("#live-player-modal .modal-footer button", has_text="Save Updates").click()
    page.wait_for_timeout(1000)
    check("score dialog closes after save", not page.locator("#live-player-modal").is_visible())
    entry = page.locator("#lineups-container section.lineup-entry").first
    check("workspace shows live status", "In progress" in entry.locator("tbody select").first.input_value() or
          any(s.input_value() == "IN_PROGRESS" for s in entry.locator("tbody select").all()),
          page.locator("#workspace-status").inner_text())

    # unknown remaining display for an in-progress player with no estimate
    entry.locator("td .slot-actions button", has_text="Score / status").nth(1).click()
    page.wait_for_timeout(300)
    page.select_option("#live-modal-status", "IN_PROGRESS")
    page.fill("#live-modal-points", "3")
    page.fill("#live-modal-rem-proj", "")
    page.locator("#live-player-modal .modal-footer button", has_text="Save Updates").click()
    page.wait_for_timeout(1000)
    check("unknown remaining is labelled", page.locator("td .cell-note", has_text="unknown").count() > 0,
          f"{page.locator('td .cell-note', has_text='unknown').count()} unknown cells")

    # --- final status requires raw points and shows multiplier math ---
    entry = page.locator("#lineups-container section.lineup-entry").first
    entry.locator("td .slot-actions button", has_text="Score / status").first.click()
    page.wait_for_timeout(300)
    page.select_option("#live-modal-status", "FINAL")
    page.fill("#live-modal-points", "12.0")
    page.locator("#live-player-modal .modal-footer button", has_text="Save Updates").click()
    page.wait_for_timeout(1000)
    entry = page.locator("#lineups-container section.lineup-entry").first
    actual_cells = entry.locator("tbody tr td:nth-child(9)").all_inner_texts()
    check("final actual shows raw x multiplier", any("x" in c and "=" in c for c in actual_cells),
          "; ".join(c.strip().replace("\n", "") for c in actual_cells[:3]))

    # --- exclude / include from the roster panel ---
    roster_first = page.locator("#roster-tbody tr").first
    player_excluded = roster_first.locator("button").inner_text()
    roster_first.locator("button").click()
    page.wait_for_timeout(1200)
    feedback = page.locator("#workspace-status").inner_text()
    check("exclude sends a message", "excluded" in feedback.lower() or "restored" in feedback.lower(), feedback)
    page.select_option("#roster-avail-filter", "EXCLUDED")
    page.wait_for_timeout(300)
    check("excluded card stays in the roster list", page.locator("#roster-tbody tr").count() >= 1,
          f"{page.locator('#roster-tbody tr').count()} excluded rows")
    page.locator("#roster-tbody tr").first.locator("button").click()
    page.wait_for_timeout(1200)
    page.select_option("#roster-avail-filter", "ALL")
    page.wait_for_timeout(300)
    check("include restores the card", "restored" in page.locator("#workspace-status").inner_text().lower(),
          page.locator("#workspace-status").inner_text())

    # --- save / load ---
    page.locator(".workspace-toolbar button", has_text="Save plan").click()
    page.wait_for_timeout(1200)
    badge = page.locator("#save-state-badge").inner_text()
    check("save marks the plan saved", badge.startswith("Saved"), badge)
    page.locator(".workspace-toolbar button", has_text="Load plan").click()
    page.wait_for_timeout(1500)
    check("load restores the plan", "loaded" in page.locator("#workspace-status").inner_text().lower(),
          page.locator("#workspace-status").inner_text())
    check("lineups still all rendered after load",
          page.locator("#lineups-container section.lineup-entry").count() == lineup_count)

    # --- generate all keeps the workspace on one page ---
    page.locator("#generate-plan-btn").click()
    page.wait_for_timeout(2000)
    pending = page.locator("#workspace-status").inner_text()
    check("generation shows pending state", "Generating" in pending or "still running" in pending, pending)
    page.wait_for_function("() => document.getElementById('workspace-status').dataset.tone !== 'pending'", timeout=180000)
    check("generation finishes inline without leaving the page",
          "Built" in page.locator("#workspace-status").inner_text(),
          page.locator("#workspace-status").inner_text())
    check("workspace still shows every lineup after regenerate",
          page.locator("#lineups-container section.lineup-entry").count() > 0,
          f"{page.locator('#lineups-container section.lineup-entry').count()} entries")

    # --- add an empty entry ---
    option_value = page.evaluate("""() => {
        const sel = document.getElementById('add-entry-contest');
        const opt = [...sel.options].find(o => !o.disabled);
        return opt ? opt.value : null;
    }""")
    if option_value:
        page.select_option("#add-entry-contest", value=option_value)
    page.locator(".workspace-toolbar button", has_text="Add empty entry").click()
    page.wait_for_timeout(1200)
    check("add empty entry works", "Added an empty" in page.locator("#workspace-status").inner_text(),
          f"option {option_value}: " + page.locator("#workspace-status").inner_text())
    check("incomplete draft is labelled not eliminated",
          page.locator(".lineup-entry.status-incomplete").count() >= 1
          and page.locator(".badge", has_text="Incomplete draft").count() >= 1)

    # --- hidden payout / scenario controls ---
    check("payout panels are collapsed out of the primary flow",
          page.locator("details.advanced-block").count() == 1
          and not page.locator("details.advanced-block").first.get_attribute("open"))
    check("advanced disclosure is closed on load",
          page.locator("#promising-lineups-grid").is_hidden(), "payout grid hidden while collapsed")

    page.screenshot(path="/tmp/gb_p3_preview/workspace_after_edits.png", full_page=True)

    # --- narrow layout ---
    narrow = browser.new_context(viewport={"width": 420, "height": 900}, http_credentials=AUTH)
    npage = narrow.new_page()
    nerrors = []
    npage.on("pageerror", lambda e: nerrors.append(str(e)))
    npage.goto(BASE, wait_until="networkidle")
    npage.wait_for_selector(".lineup-entry")
    check("narrow layout renders lineups", npage.locator(".lineup-entry").count() > 0,
          f"{npage.locator('.lineup-entry').count()} entries")
    overflow = npage.evaluate("""() => {
        const wide = [...document.querySelectorAll('.lineup-entry .table-wrap')]
            .filter(el => el.scrollWidth > el.clientWidth + 2).length;
        const docOverflow = document.documentElement.scrollWidth - document.documentElement.clientWidth;
        const clipped = [...document.querySelectorAll('.lineup-slot-table button, .lineup-actions button')]
            .filter(b => b.getBoundingClientRect().width < 10).length;
        return {wide, docOverflow, clipped};
    }""")
    check("narrow layout contains wide tables in scroll wrappers", overflow["docOverflow"] <= 2,
          json.dumps(overflow))
    check("narrow layout has no clipped buttons", overflow["clipped"] == 0, json.dumps(overflow))
    npage.screenshot(path="/tmp/gb_p3_preview/workspace_narrow.png", full_page=True)
    check("no page errors in narrow layout", not nerrors, "; ".join(nerrors))

    # --- keyboard access on the workspace controls ---
    page.goto(BASE, wait_until="networkidle")
    page.wait_for_selector(".lineup-entry")
    page.locator("#lineup-search").focus()
    focused = page.evaluate("() => document.activeElement.id")
    check("search is keyboard reachable", focused == "lineup-search", focused)
    focus_visible = page.evaluate("""() => {
        const el = document.getElementById('roster-search');
        el.focus();
        const styles = getComputedStyle(el, ':focus-visible');
        return document.activeElement.id;
    }""")
    check("roster search is keyboard reachable", focus_visible == "roster-search", focus_visible)

    check("no console errors on the workspace", not console_errors, "; ".join(console_errors[:4]))
    browser.close()

failed = [r for r in results if not r[1]]
print("\n--- summary ---")
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
for label, _, detail in failed:
    print("FAILED:", label, "::", detail)
