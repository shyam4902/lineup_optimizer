# V1 agent results

September 11, 2026: planning handoff created. No implementation packet has run under this plan. No tests or private server restart performed in this planning pass.

Agents append their packet results here using the result format in the plan. Previous September 10 test and roster results are historical evidence only.

## Packet 1 - September 11, 2026

### Files changed

- `app.py`: Three route fixes.
  - `api_planner_exclude` (~L804): Removed lock-clearing code. Exclusion marks cards ineligible but preserves `slot.is_locked` and `lineup.is_locked`.
  - `api_planner_edit_lineup` (~L817-851): Rebuild action preserves game-locked and manually locked slots as fixed during solver call. Remove action rejects when started players are present. Added `import math`.
  - `api_live_update_player_state` (~L1301-1379): Validate-then-apply pattern. All numeric fields pass `math.isfinite()` before mutation. `game_status` validated against `UPCOMING/IN_PROGRESS/FINAL`. Setting `FINAL` zeroes `remaining_projection` and `remaining_stdev`.
- `test_planner_api.py`: Updated existing assertions and added `V1Packet1RegressionTests` class with 7 tests.
  - `test_exclusion_swap_rebuild_and_restore`: Changed assertion from `assertFalse(slot.is_locked)` to `assertTrue` (rule 5).
  - Regeneration assertion updated: excluded player may remain in locked slots.
  - New tests: `test_rebuild_preserves_thursday_locked_slot`, `test_exclusion_preserves_lock_on_assigned_copy`, `test_score_handling_with_multiplier`, `test_final_status_zeroes_remaining_projection`, `test_invalid_score_rejected_without_mutation`, `test_save_load_preserves_all_state`, `test_generation_failure_preserves_existing_plan`.

### Checks run

- `.venv/bin/python -m unittest discover -v`: 120 tests, 0 failures, 0 errors.
- Baseline was 113 tests. Added 7 regression tests in `V1Packet1RegressionTests`.

### Endpoint contracts for next agent

- `POST /api/live/update_player_state`: Returns 400 with `{"status": "error", "message": "..."}` for nonnumeric, NaN, or infinity in any score field. Valid game_status values: `UPCOMING`, `IN_PROGRESS`, `FINAL`. Setting FINAL zeroes `remaining_projection` and `remaining_stdev`. Response body unchanged on success: `{"status": "success", "player_state": {...}}`.
- `POST /api/planner/edit_lineup`: Rebuild action now accepts lineups with started players. Started/game-locked slots stay fixed; unlocked slots get rebuilt. Remove action rejects if started players exist. Error message changed from "This lineup has players whose games have started." to "This lineup has started players. Remove them first." for remove only.
- `POST /api/planner/exclude`: No longer clears `slot.is_locked` or `lineup.is_locked`. The excluded card becomes ineligible, the slot stays locked, and the lineup turns invalid. The user must manually unlock if they want to replace the excluded player.
- Score math: `effective_mean(multiplier)` for FINAL status returns `final_points * multiplier`. Raw 0 on 1.5x = 0, raw -2 on 1.5x = -3, raw 10 on 1.5x = 15.

### Unresolved issues

- Rule 9 in the plan says in-progress players with no remaining estimate should show as "unknown" rather than silently adding full pregame projection. The `effective_mean` method in `live_lineup_manager.py` still uses `remaining_projection` for in-progress. I did not change `live_lineup_manager.py` because Packet 1 only required score validation in the route, not restructuring the effective_mean model. The next packet should address the "unknown remaining" display.
- The `api_planner_edit_lineup` rebuild uses `compute_contest_benchmark` (single-lineup solver) rather than the full multi-lineup solver. This means the rebuild picks the best single lineup for that contest, ignoring cross-lineup card allocation. Acceptable for Packet 1, since two-lineup exchange is scoped to Packet 2.

## Packet 2 - September 11, 2026

Packet 1 was confirmed before editing. Baseline `test_planner_api test_weekly_planner` passed 42 tests, and the full suite passed 120.

### Files changed

- `multi_lineup_planner.py`: new draft and transfer helpers, and one refactor.
  - `blocking_lineup_errors()` filters out incomplete-draft notices (`Slot 'X' is empty.`, `Salary ... is below minimum requirement ...`) so a partial entry stays editable.
  - `plan_card_usage()` and `revalidate_plan()` centralize copy-usage tracking and recalculation. `swap_card_in_lineup()` now calls `revalidate_plan()` instead of repeating that block.
  - `add_empty_lineup()` appends an entry with every contest slot unfilled, bounded by `default_entry_limit`.
  - `clear_slot()` empties an unlocked slot and leaves other placements alone.
  - `move_or_exchange_card()` moves a card into an empty slot or exchanges two occupied slots. It validates both resulting entries on a copy first, then mutates the plan only if neither entry gains a new blocking error.
- `app.py`:
  - `api_planner_edit_lineup` now accepts `action: "add"` and `action: "clear"` in addition to `remove` and `rebuild`. `add` requires a contest that is one of the plan's selected tiers.
  - New `POST /api/planner/move` route plus `_game_locked_card()` helper.
  - `_swap_candidates` now diffs blocking errors against the current lineup instead of requiring a fully valid lineup. This is what lets a user fill an incomplete draft one slot at a time. A swap that would introduce a position, duplicate-athlete, copy-reuse, collection, primetime, or max-cap violation is still filtered out.
- `test_planner_api.py`: added `V1Packet2ManualConstructionTests` with 9 tests.

No UI files were touched. No solver objective, payout, or deployment code changed.

### Checks run (new)

- `.venv/bin/python -m unittest test_planner_api test_weekly_planner -v`: 51 tests, 0 failures, 0 errors.
- `.venv/bin/python -m unittest discover -v`: 129 tests, 0 failures, 0 errors. Packet 1 baseline was 120, so 9 new tests were added.
- Request and response bodies below were captured by running an isolated `PlannerAppState` (temp dir) with a synthetic 20-player fixture through the Flask test client. No private roster data is included.

### Endpoint contracts for the UI agent

All examples use a generated Scorcher entry (`scorcher_1`) plus one added empty Scorcher draft. Scorcher allows 5 entries.

Add an empty entry:

```
POST /api/planner/edit_lineup
{"action": "add", "contest_name": "Scorcher"}

HTTP 200
{
  "status": "success",
  "message": "Added an empty Scorcher entry (5 slots to fill).",
  "lineup": {
    "lineup_id": "scorcher_2",
    "contest_name": "Scorcher",
    "is_valid": false,
    "total_salary": 0,
    "validation_errors": ["Slot 'QB' is empty.", "...", "Salary $0 is below minimum requirement $20,000."],
    "slots": [
      {"slot_name": "QB", "slot_kind": "QB", "card": null, "is_locked": false},
      {"slot_name": "RB", "slot_kind": "RB", "card": null, "is_locked": false},
      {"slot_name": "WR", "slot_kind": "WR", "card": null, "is_locked": false},
      {"slot_name": "TE", "slot_kind": "TE", "card": null, "is_locked": false},
      {"slot_name": "Flex", "slot_kind": "Flex", "card": null, "is_locked": false}
    ]
  },
  "plan": { ... }
}
```

Add past the contest entry cap (5th add succeeds, 6th fails). Nothing changes on failure:

```
HTTP 400
{"status": "error", "message": "Contest 'Scorcher' already has 5 of 5 allowed entries."}
HTTP 400
{"status": "error", "message": "'Volcano' is not one of the selected contest tiers."}
```

Clear an unlocked slot:

```
POST /api/planner/edit_lineup
{"lineup_id": "scorcher_1", "action": "clear", "slot_name": "WR"}

HTTP 200
{"status": "success", "message": "Cleared WR.", "lineup": {"is_valid": false, ...}, "plan": { ... }}

HTTP 400
{"status": "error", "message": "This slot is locked. Unlock it before clearing."}
HTTP 400
{"status": "error", "message": "This player's game has started. That placement cannot be cleared."}
```

Fill a draft slot. Candidates are returned for incomplete drafts:

```
GET /api/planner/candidates?lineup_id=scorcher_2&slot_name=QB

HTTP 200
{"status": "success", "cards": [
  {"card_id": "qb2", "player_name": "Josh Allen", "position": "QB", "weekly_salary": 8000, "remaining_salary": 32000},
  {"card_id": "qb3", "player_name": "Lamar Jackson", "position": "QB", "weekly_salary": 7800, "remaining_salary": 32200}
]}

POST /api/planner/swap
{"lineup_id": "scorcher_2", "slot_name": "QB", "new_card_id": "qb2"}

HTTP 200
{"status": "success", "message": "Card swapped successfully.",
 "lineup": {"lineup_id": "scorcher_2", "is_valid": false, "total_salary": 8000,
            "slots": [{"slot_name": "QB", "card": {"card_id": "qb2", ...}, "is_locked": true}, ...], ...},
 "total_weekly_projection": ..., "total_weekly_estimated_payout": ...}
```

Move a card into an empty slot. The move returns both affected entries and locks the destination. The emptied source slot unlocks:

```
POST /api/planner/move
{"source_lineup_id": "scorcher_1", "source_slot_name": "RB",
 "destination_lineup_id": "scorcher_2", "destination_slot_name": "RB"}

HTTP 200
{
  "status": "success",
  "message": "Card moved successfully.",
  "affected_lineup_ids": ["scorcher_1", "scorcher_2"],
  "lineups": [
    {"lineup_id": "scorcher_1", "is_valid": false, "total_salary": 22800,
     "validation_errors": ["Slot 'RB' is empty."],
     "slots": [{"slot_name": "RB", "card": null, "is_locked": false}, ...]},
    {"lineup_id": "scorcher_2", "is_valid": false, "total_salary": 14200,
     "slots": [{"slot_name": "RB", "card": {"card_id": "rb4", ...}, "is_locked": true}, ...]}
  ],
  "plan": { ... }
}
```

Exchange two occupied slots. Both destinations lock:

```
POST /api/planner/move
{"source_lineup_id": "a", "source_slot_name": "QB",
 "destination_lineup_id": "b", "destination_slot_name": "QB"}

HTTP 200
{"status": "success", "message": "Card exchanged successfully.",
 "affected_lineup_ids": ["a", "b"], "lineups": [ ... both entries, both QB slots is_locked true ... ], "plan": { ... }}
```

Rejections. Both entries stay unchanged and the message lists the blocking rule:

```
HTTP 400 {"status": "error", "message": "Salary $40,400 exceeds maximum cap $40,000."}
HTTP 400 {"status": "error", "message": "Slot 'QB' requires QB, but has Sam LaPorta (TE)."}
HTTP 400 {"status": "error", "message": "Duplicate athlete in lineup: 'Justin Jefferson' is assigned more than once."}
HTTP 400 {"status": "error", "message": "Destination slot 'QB' is locked. Unlock it before moving cards."}
HTTP 400 {"status": "error", "message": "Source lineup is locked. Unlock it before moving cards."}
HTTP 400 {"status": "error", "message": "A player in this move has a started or final game. That placement is protected."}
HTTP 400 {"status": "error", "message": "The source slot is empty."}
HTTP 404 {"status": "error", "message": "Source or destination lineup not found."}
```

### Behavior notes for packet 3

- `add` requires an active plan. The cap is the contest's `default_entry_limit`, and the contest must be in the plan's `inventory_summary.contest_distribution` when that distribution exists. Auto-mix plans without a distribution fall back to `default_entry_limit`.
- Locking rule after a transfer: an explicit placement locks its destination. A move locks the destination and unlocks the emptied source. An exchange locks both slots.
- Drafts stay editable. Empty-slot and below-minimum-salary errors do not block a swap or move. Max salary, slot position, duplicate athlete, cross-lineup copy reuse, collection, and primetime rules do block, and the two entries are left untouched on rejection. `is_valid` and `validation_errors` still report the full picture so the UI can show an incomplete entry without labeling it eliminated.
- Manual and game locks are separate. A started or final player's slot rejects both `clear` and `move` even when the manual `is_locked` flag is false.
- Save/load was not re-verified in this packet. Packet 1 already covers persistence and the new operations only mutate `lineups`, `slots`, and locks that the existing save path serializes.

### Unresolved issues

- From packet 1 and still open: in-progress players with no remaining estimate still fall back to the pregame `remaining_projection` in `effective_mean`. Rule 9 asks for an explicit unknown state.
- `add` requires an active plan because selected tiers live on the plan. A user who wants to build the first entry fully manually, with no prior generation, has no route for it yet. Packet 3 should say whether that case matters.
- Regenerate all with an empty draft: `is_plan_fully_qualifying` treats any empty slot as not qualifying, so a draft with no locked placements is replaced rather than preserved through `POST /api/planner/generate`. That matches rule 3 for locked placements but is worth confirming against the packet 3 workspace behavior.
- `compute_contest_benchmark` is still used for `rebuild`, so a single-entry rebuild does not account for cross-lineup card allocation. Unchanged from packet 1.

## Packet 3 - September 11, 2026

Packets 1 and 2 were confirmed before editing. `.venv/bin/python -m unittest discover` ran 129 tests with 0 failures, matching the count packet 2 recorded.

### Files changed

- `templates/index.html`: the workspace rewrite, plus one shared CSS addition.
  - `sec-overview` is now the workspace. It holds the contest tier form, a toolbar, a four tile summary strip, an entries-by-contest row, a lineups column that renders every lineup expanded with its full slot table, and a roster panel beside it.
  - Removed the master-detail markup (`sec-lineups`), `renderSidebarLineups`, `selectLineup`, `renderLineupDetail`, `renderContestMix`, the `roster-modal`, `renderRosterBrowser`, `filterRosterBrowser`, `editCurrentLineup`, and `toggleCurrentLineupLock`. Their ids and functions are gone with no remaining references.
  - The Lineups nav pill is gone. The remaining sections are Workspace, In-week scores, Roster & projections, and Single Lineup Solver.
  - Payout, leverage and demo scenario cards in `sec-live` now sit inside one collapsed `<details class="advanced-block">`. The roster status table and the kickoff schedule import stay visible.
  - The live player dialog became the workspace score dialog. It shows pregame raw projection, adjusted projection, and the card multiplier, and its fields are labelled raw points, remaining estimate, and status.
  - Every `alert()` in the file was replaced by inline status messages. No browser alert chains remain.
- `multi_lineup_planner.py`: one narrow fix, described under backend gap below. No other backend file changed.

### Backend gap found and fixed

The plan allowed a specific, recorded backend fix. Regenerating a plan failed whenever one lineup held two or more locked slots. The API returned HTTP 500 with `Optimization failed: overlapping constraint names: lock_z_inferno_1`.

Reproduction on the isolated preview before the fix. Three slots locked in `inferno_1`, then `POST /api/planner/generate`:

```
HTTP 500
error | Optimization failed: overlapping constraint names: lock_z_inferno_1
```

The cause is at `multi_lineup_planner.py` constraint 5. The loop over `locked_assignments` added `model += z[lid] == 1, f"lock_z_{lid}"` once per locked slot, so the same PuLP constraint name was added twice for a lineup with two locks. The fix adds an `activated_lids` set and emits the `lock_z_{lid}` constraint once per lineup. The per-slot `lock_{lid}_{sname}_{card_id}` constraints are unchanged.

Same call after the fix, with two locks held in `inferno_1` and one in `inferno_2`:

```
success | Built 9 lineups. 241 eligible cards remain available for edits.
locks after generate: inferno_1 ['RB', 'RB2'], inferno_2 ['QB']
locked players kept in place on both
```

No regression test was added for this. `test_planner_api.py` belongs to packet 2 and other agents may hold it, so packet 4 should add a generate-with-two-locks case.

### Checks run (new)

Automated suite, after all edits: `.venv/bin/python -m unittest discover` reported 129 tests, 0 failures, 0 errors. Packet 2 left the same count, so nothing regressed.

Browser check on an isolated preview. Instance root `/tmp/gb_p3_preview` with `PLANNER_INSTANCE_ROOT` and a throwaway local password file, which forces `private_preview` mode, binds 127.0.0.1, and keeps the user's plan untouched. The isolated roster copy has 322 data rows and produced 322 planner cards. Ten lineups were generated with `{"Scorcher":3,"Volcano":2,"Flex Appeal":2,"Primetime Pyro":2,"Inferno":2}` before the check. Not a private server restart or a deployment.

Script and output are in `docs/review/packet3/`. Result: 57 of 57 checks passed. Screenshots at 1440x900 and 420x900 are in the same folder.

What the check covered, all through the real page and API:

- 10 of 10 lineups rendered as separate expanded cards, 61 of 61 slot rows visible, no placeholder state, 322 of 322 roster rows in the panel, 61 assignment badges matching 61 filled slots, and one inline game-status select per filled slot.
- Lineup search filtered 10 entries to the 2 Inferno ones and clearing it restored all 10. Roster position filter returned 62 QB rows, and the assigned filter returned exactly the 61 assigned copies.
- Slot lock and unlock both persisted to `/api/planner/state`, and the save badge switched to `Unsaved changes` after an edit.
- The move dialog listed 61 destinations, marked 52 as blocked with a reason, disabled the source slot, and offered no enabled blocked row. A legal exchange between Inferno #1 and Inferno #2 succeeded and reported both entries.
- Swap listed 41 candidates, applied, and left the destination slot locked to the new card.
- Clear left an empty slot that still showed what it needs, the entry was labelled `Incomplete draft`, and `Fill slot` on an empty row returned 45 candidates and refilled it with a lock.
- The score dialog showed `Pregame raw projection`, `Adjusted projection (multiplier applied once)`, and `Card multiplier`. Saving In progress with a remaining estimate, saving In progress with no estimate, and saving Final with raw points all worked. The workspace showed `unknown (no estimate)` for the missing remaining, and actual points as `12.0 x1.40 = 16.8`.
- Exclude kept the card visible in the roster with the reason, Include restored it, and both left protected placements locked.
- Save marked the plan saved with a timestamp. Load reported the restored plan and every lineup stayed rendered.
- Generate all showed `Generating lineups...` while the request ran, finished without leaving the page, and kept all lineups visible. This run passed with two locked slots in one lineup, which is the case that failed before the solver fix.
- Add empty entry added a Primetime Pyro draft, and the draft was labelled `Incomplete draft` rather than eliminated.
- The advanced disclosure is collapsed on load and the payout grid is hidden inside it.
- Narrow layout at 420x900: document overflow 0, no clipped buttons, no page errors. Wide slot tables scroll inside their wrappers. Every lineup row stayed reachable.
- Keyboard access: `#lineup-search` and `#roster-search` both take focus. No page or console script errors on the workspace.

Views:

- `docs/review/packet3/workspace_desktop.png`
- `docs/review/packet3/workspace_after_edits.png`
- `docs/review/packet3/move_dialog.png`
- `docs/review/packet3/workspace_narrow.png`

### Endpoint changes relevant to the next packet

None. No endpoint contract changed, and no required API field was missing, so the missing-field allowance was not used. Two behaviours the UI depends on:

- `POST /api/planner/toggle_lock` returns only `lineup`, not `plan`, so the page patches that one lineup into its local plan copy instead of refetching everything. `POST /api/planner/swap` returns `lineup` too and is handled the same way.
- `POST /api/planner/exclude` changes card eligibility but returns no plan, so the page refetches `/api/planner/state` after it.
- `POST /api/planner/move` returns `lineups` and `plan`, which the page applies directly.

### Unresolved issues

- The `unknown` remaining state for an in-progress player is a display convention in the page, not server state. The page tracks which athletes the user gave a remaining estimate to during the session, so after a page reload an in-progress player shows their seeded remaining projection again. `effective_mean` still falls back to `remaining_projection`, unchanged from packet 1. Packet 4 should decide whether the model needs its own unknown flag.
- `Add empty entry` still needs an existing plan because selected tiers live on the plan. The button explains this in the toolbar status instead of offering a route that does not exist. Packet 2 raised the same gap.
- Rebuild still uses `compute_contest_benchmark` and ignores cross-lineup card allocation. Unchanged from packets 1 and 2.
- The roster panel scrolls horizontally on a narrow screen because all eight columns are kept. No control is clipped and no row is hidden, but a narrow-screen card layout may read better later.

## Packet 4 - September 12, 2026

Packet 3 was confirmed first. Everything below is a new check run against the isolated preview; packet 1 to 3 results are not reused as evidence.

### Files changed

- `app.py`: one correction to regeneration, plus two helpers next to `_game_locked_card`.
  - `_game_locked_placements(plan)` returns the `(lineup_id, slot_name)` placements whose player has a started or final game.
  - `_plan_with_game_locks(plan, placements)` returns a shallow copy of the plan with those placements marked `is_locked`, for the solver call only.
  - `api_planner_generate` passes that copy to `solve_multi_lineup_allocation`, and the unselected-contest guard now covers game-locked placements.
- `test_planner_api.py`: new `V1Packet4StartedPlayerTests` class with 4 tests. It also closes the gap packet 3 recorded: `test_generate_with_two_locked_slots_in_one_lineup` covers the duplicate PuLP constraint name that broke regeneration when one lineup held two locks.
- `docs/review/packet4/`: `acceptance_check.py` (phase 1 and phase 2), `acceptance_check.txt`, `restart_preview.sh`, and three screenshots.

`multi_lineup_planner.py`, `optimizer_core.py` and `templates/index.html` were not touched. Nothing was written under `~/Library/Application Support/GameBlazers`; the user's previews on ports 5013 to 5016 were left alone.

### Reproduced failure and correction

Regenerating all dropped an underperforming Final Thursday player. `POST /api/planner/generate` never read live state, so a started or final player sitting in an unlocked slot went back into the solver pool like any other card. Rule 2 gives that player a game lock, and rule 3 requires regenerate all to keep every locked placement.

Reproduction on the isolated preview before the fix. Puka Nacua (LAR, 1.40x) was Final at 2.0 raw points in `inferno_2` WR, with the slot left manually unlocked:

```
FAIL regenerate all preserves the Final Thursday player :: inferno_2 WR -> CeeDee Lamb
```

Same checks after the correction:

```
PASS regenerate all preserves the Final Thursday player :: inferno_2 WR -> Puka Nacua
PASS regenerate all preserves the Final score :: 2.0
PASS regenerate all keeps the lineup IDs :: [] lost
PASS regenerate all preserves every locked placement
```

Both new regeneration tests fail when the two helpers are patched out, with `AssertionError: 'qb4' != 'qb1'`. They fail on the defect, not only on the fix.

A second failure was in my own check, not the app: `total_weekly_estimated_payout` moved (159.0 to 137.0) because the lineup composition changed between the two measurements. Measured around the score entry alone, the number is unchanged, which is what the packet asked to confirm. The measurement was corrected, not the app.

### Checks actually run

- `.venv/bin/python docs/review/packet4/acceptance_check.py 1`: 82 checks, 0 failures.
- `docs/review/packet4/restart_preview.sh` then `acceptance_check.py 2`: 8 checks, 0 failures.
- `.venv/bin/python -m unittest discover`: 133 tests, 0 failures, 0 errors, run after the last correction. Packet 3 recorded 129, so the 4 new tests account for the difference.
- `.venv/bin/python -m unittest test_planner_api.V1Packet4StartedPlayerTests -v`: 4 tests, 0 failures.

### Measured outcomes on the isolated copy

Instance and roster. Instance root `/tmp/gb_p4_preview`, listening on 127.0.0.1:5098 only, started with `PLANNER_INSTANCE_ROOT` and a throwaway password file, which forces `private_preview` mode. The roster file is a copy of `~/Library/Application Support/GameBlazers/shyam-preview/data/raw/My_roster.csv`; both files hash to `ce66e7227144bd980decb2c9a0f60cc0`. Served `roster_count` is 331, matching the copied file's 331 data rows, and every served card matches the CSV on player name and multiplier. 307 cards matched a projection and 304 were eligible. The old 331-card reference held up, but the number is reported from the input, not assumed.

Generation. `{"Scorcher": 2, "Volcano": 2, "Flex Appeal": 2, "Inferno": 2}` built 8 valid lineups in about 4 seconds, leaving 254 eligible cards unassigned. 50 slot rows were filled. No card copy appeared twice. Every lineup sat inside its contest salary range, and no lineup came from an unselected tier. All 8 entries rendered as separate workspace cards with all 50 player rows visible next to the roster panel.

Roster panel. 331 of 331 owned copies listed. Ashton Jeanty, an unmatched row that generation ignores, was still found by search and showed `No projection match`. The position filter returned 63 QB rows, and the assigned filter returned exactly the 50 assigned copies.

Manual work on the generated plan.
- Exchange between Inferno #1 and Inferno #2: QB cards swapped, salaries 53,900 to 53,600 and 51,200 to 51,500, both projected scores changed, both slots locked, both entries still valid.
- Swap: RB De'Von Achane replaced by Travis Etienne Jr., and the destination locked to the new card.
- Clear then refill: RB2 emptied, the entry showed `Incomplete draft` with the slot marked `needs RB`, then `Fill slot` returned 46 candidates and refilled it with De'Von Achane. No eliminated label appeared.
- Exclusion: excluding an assigned, locked player left the placement in place with the lock intact. Excluding unassigned AJ Barner removed him from the 39 swap candidates; including him brought him back into the 40.

Thursday Final player. The dashboard schedule import for 2026 week 1 updated 168 players, and 24 Thursday-team players (NE, SEA, SF, LAR) received kickoff times, which locks them on their real kickoff dates. Puka Nacua was marked Final at 2.0 raw points on a 1.40x card: the workspace showed 2.0 x1.40 = 2.8 lineup points and zero remaining projection, and the slot displayed `Game started` while `is_locked` stayed false. `Rebuild unlocked slots` kept him in WR and changed Flex and RB2. `Generate all lineups` kept him in WR too, after the correction above.

No automatic risk or payout behaviour. Entering the Final score left `total_weekly_estimated_payout` at 159.0, `reallocation_plan` stayed null, the reallocation panel stayed hidden, the payout grid stayed inside the collapsed advanced block, and no entry carried an eliminated label.

Save, load, restart. Save wrote `/tmp/gb_p4_preview/data/saved_plans/latest_plan.json`, and the file matched the live workspace state field for field. A deliberate clear after the save changed the plan, and Load restored placements, locks, scores, exclusions and selected tiers exactly. After stopping and restarting the isolated server, the fresh process reported no active plan, `POST /api/planner/load` reported no missing cards, and the reloaded state matched the snapshot taken before the restart on every compared field. All 8 lineups rendered again.

### Endpoint changes relevant to the next packet

- `POST /api/planner/generate` now keeps started and final placements. A game-locked slot comes back with `is_locked: true`, which is accurate: the placement is locked, and the manual unlock only clears the manual flag while swap, clear, move and the next regenerate still refuse to move that player.
- The guard message for a deselected tier holding a protected entry changed to: `A lineup with locked or started players belongs to an unselected contest. Select that contest or unlock the placements first.` The request still returns 400 and leaves the plan untouched.
- No other request or response shape changed.

### Unresolved issues

- Still open from packets 1 to 3: an in-progress player with no remaining estimate falls back to the seeded `remaining_projection` in `live_lineup_manager.PlayerLiveState.effective_mean`. The `unknown (no estimate)` label lives in `templates/index.html` (`slotLiveValues`, `appState.remainingSet`), so it does not survive a reload. Packet 4 did not fix or re-measure this; it sits outside the acceptance list. Packet 3 observed the label in the browser, and the reload behaviour follows from the page keeping the flag in session state. The open decision is whether the model carries its own unknown flag.
- Still open: `Add empty entry` needs an existing plan because selected tiers live on the plan.
- Still open: a single-entry rebuild uses `compute_contest_benchmark` and ignores cross-lineup card allocation, so it can pick a card another entry also wants.
- New and cosmetic: after `_plan_with_game_locks` runs, a game-locked slot comes back from regeneration with `is_locked: true`, so `buildSlotRow` shows a manual lock icon and an `Unlock slot` button for it. Unlocking clears only the manual flag: `api_planner_swap`, `clear`, `move` and the next regenerate all still refuse to move that player. Showing a distinct game-lock state is later polish, not a correctness gap.
- The plan's `total_weekly_estimated_payout` still changes when lineup composition changes. That is the existing model, and the payout panels stay out of the primary workspace flow.

### Preview location

```
http://127.0.0.1:5098
```

Isolated instance root `/tmp/gb_p4_preview`, screen session `gbp4`, username `friend`, password in `/tmp/gb_p4_preview/password.txt`. Not a deployment, and not the user's active plan. Stop it with `screen -S gbp4 -X quit` and then `kill $(lsof -ti :5098)`. Restart it with `docs/review/packet4/restart_preview.sh`. Views: `docs/review/packet4/workspace_generated.png`, `workspace_after_packet4_flow.png`, `workspace_after_restart.png` at 1440x900.

Packet 4 stops here.

## Coordinator review - September 12, 2026

- Read all four packet results and the current code for remaining-score display and manual entry creation. No application code changed in this review.
- Fresh run: `.venv/bin/python -m unittest discover -q` passed 133 tests in 14.174 seconds. Log: `/tmp/gb-v1-status-tests.log`. Browser acceptance was not repeated; the browser results above remain the packet agents' recorded evidence.
- Two corrections are specified in `V1_CORRECTION_PROMPT.md`: persistent explicit/unknown remaining estimates, and creating the first manual draft without generation. The latter includes enforcing the selected entry count, which the add route currently omits when calling the existing helper.
- Clarification of earlier notes: current `slotLiveValues` uses the session-only `remainingSet` flag. Reload therefore loses recognition of an explicitly supplied estimate. Backend seeded remaining values are a separate issue. Do not describe the exact browser reload symptom solely from the earlier report.
- The claimed rebuild card-reuse concern is not established by the current code. `api_planner_edit_lineup` excludes copies assigned to other lineups from its rebuild pool. Optimizing one entry independently is intended V1 behavior, not a reason to add global allocation work.
- No private runtime restart or deployment performed in this review. Next step is the bounded correction prompt, followed by user testing of the corrected preview.

## Correction pass - September 12, 2026

Both gaps were reproduced before editing on an isolated in-memory state, then fixed. The fresh reviewer run of 133 tests was the baseline.

### Files changed

- `live_lineup_manager.py`
  - `PlayerLiveState` gains `remaining_is_estimate: bool = False`. A seeded pregame value loads as unknown, never as a manual entry.
  - `effective_mean` for an in-progress player without an explicit estimate returns banked live points only, so the seeded projection is not added again.
  - `to_dict` includes the flag, which puts it in `/api/live/state` and the `live_session` save payload.
  - `evaluate_lineup_live_state` adds an in-progress player's remaining mean and variance only when the estimate is explicit.
  - `parse_live_scores_csv` marks the estimate explicit when the CSV has a remaining column.
- `app.py`
  - `api_live_update_player_state` validates `remaining_is_estimate` as a boolean. A numeric remaining sets the flag true; `{"remaining_is_estimate": false}` marks unknown; FINAL zeroes remaining and sets the flag false.
  - `api_live_state` uses `effective_mean` for the in-progress slot total instead of always adding remaining.
  - New `_plan_for_manual_entry(data, plan)` validates an add request and builds a manual `WeeklyPlan` from the selected contest controls when none exists. It never calls the solver.
  - `api_planner_edit_lineup` `add` action now runs without an active plan and passes the selected `entry_limit` to `add_empty_lineup`.
- `templates/index.html`
  - Removed `appState.remainingSet`. `slotLiveValues`, `openScoreModal`, and the save path read `remaining_is_estimate` from the server state.
  - The live players table shows `unknown` for an in-progress player with no estimate.
  - `onScoreStatusChange` clears the field when switching an upcoming player to in-progress, so the seeded projection is not carried in as a typed estimate.
  - `addEntry` builds the selected distribution and entry limit from the controls and no longer requires an existing plan. `renderEntryMix` lists only selected tiers and enables the picker without a plan. A successful add keeps the user's controls instead of snapping limits back to the plan.
- `test_planner_api.py`: new `V1CorrectionTests` class, 11 tests.
- `test_live_manager.py`: the in-progress effective-mean case now supplies an explicit estimate, plus a new unknown case.
- `docs/review/correction/`: `repro.py`, `repro.txt`, `browser_check.py`, `browser_check.txt`, `restart_preview.sh`, `phase1_manual.png`, `phase2_after_restart.png`.

### Reproduced failures and correction

Before the fixes, `docs/review/correction/repro.py` reported 8 of 15 checks passing. The two real failures:

```
FAIL in-progress unknown does not add the seeded projection :: effective_mean=45.0 expected 15.0
FAIL first manual add succeeds without generation :: 400 No active weekly plan.
```

After the fixes the same script reports 17 of 17. The seeded projection `(10 live + 20 seeded) * 1.5x = 45.0` now stays at `10 * 1.5 = 15.0` until an estimate is supplied.

### Checks actually run

- `.venv/bin/python docs/review/correction/repro.py`: 17 of 17 checks, up from 8 of 15 before the fixes. Log: `docs/review/correction/repro.txt`.
- `.venv/bin/python -m unittest test_planner_api.V1CorrectionTests test_live_manager -v`: 23 tests, 0 failures.
- `.venv/bin/python -m unittest discover -v`: 144 tests, 0 failures, 0 errors, run after the last correction. The 133-test reviewer baseline plus the 11 new tests accounts for the difference.
- Browser, isolated preview: 28 of 28 checks, no console or page errors. Phase 1, 23 checks; phase 2 after a restart, 5 checks. Log: `docs/review/correction/browser_check.txt`.

### Measured outcomes on the isolated copy

Instance root `/tmp/gb_correction_preview` on 127.0.0.1:5097, started with `PLANNER_INSTANCE_ROOT` and a throwaway password file, which forces `private_preview` mode. The roster and snapshot are copies of the packet-4 isolated instance; the served roster is 331 cards, matching the copied file. The user's previews on 5013 to 5017 and the packet 3 and 4 previews on 5099 and 5098 were left running and untouched. Not a private restart or a deployment.

Gap 2, first manual draft. On a fresh instance with no active plan, `#add-entry-contest` was enabled and listed the selected tiers. Setting the Scorcher limit to 1 and clicking `Add empty entry` created one `Scorcher #1` draft labelled `Incomplete draft`, recorded `{"Scorcher": 1}` in the plan's contest distribution, and never ran the solver. A second add was rejected with `Contest 'Scorcher' already has 1 of 1 allowed entries.` Raising the limit control to 2 then allowed a second draft. Two slots were filled one at a time through the real `Fill slot` dialog and each placement locked. Save and load kept both entries.

Gap 1, remaining estimate state. For a filled slot moved to In progress with the estimate left blank, the dialog reopened blank, the workspace showed `unknown (no estimate)`, and the reload still showed unknown. Explicit zero saved as a known estimate with value 0.0 and survived a reload. A positive estimate of 4.0 saved as known, clearing the field returned it to unknown, and re-supplying 6.5 stuck. A second player marked Final at 12.0 raw points stored zero remaining. After saving, restarting the isolated server, and clicking `Load plan`, one player still carried the 6.5 explicit estimate and one still carried the Final 12.0 score with zero remaining. Both entries rendered again.

### Endpoint and state changes for the next agent

- `POST /api/live/update_player_state` accepts an optional `remaining_is_estimate` boolean. A numeric `remaining_projection` or `effective_projection` forces the flag true. `{"remaining_is_estimate": false}` marks the estimate unknown while leaving the seeded value in place. FINAL zeroes `remaining_projection` and `remaining_stdev` and sets the flag false. Non-boolean values return 400.
- `GET /api/live/state` returns `remaining_is_estimate` on every entry in `player_states` and `players`.
- An in-progress player with no estimate contributes live points only to `effective_mean` and to the slot's `total_projected_points`.
- `POST /api/planner/edit_lineup` with `action: "add"` now works with no active plan. It accepts `contest_distribution` (the selected tiers and counts) and `entry_limit` (the selected count for the contest). It rejects zero counts, counts above the contest maximum, and contests outside the selection. On rejection `STATE.active_plan` is unchanged, including staying absent.

### Unresolved issues

- The selected-count enforcement uses the `entry_limit` the request sends, which the page always sends. API callers that omit it keep the earlier behavior: for an existing plan the contest maximum applies, and a contest outside the plan's recorded tiers is still rejected. This kept the packet 1 to 4 tests valid without widening the change.
- `players[].effective_projection` in `/api/live/state` still mirrors `remaining_projection`. The page decides unknown from the flag, so the raw field is informational only. Cosmetic.
- No payout or risk model changed. The only compatibility adjustment is that `evaluate_lineup_live_state` no longer folds a seeded remaining value into an in-progress player's mean and variance when no estimate was supplied.
- Still open from packets 1 to 4 and outside this prompt: a single-entry rebuild uses `compute_contest_benchmark` and ignores cross-lineup card allocation, and a game-locked slot comes back from regeneration with `is_locked: true` so it shows a manual lock icon.

### Preview location

```
http://127.0.0.1:5097
```

Isolated instance root `/tmp/gb_correction_preview`, screen session `gbcorr`, username `friend`, password in `/tmp/gb_correction_preview/password.txt`. Stop it with `screen -S gbcorr -X quit` and then `kill $(lsof -ti :5097)`. Restart it with `docs/review/correction/restart_preview.sh`. Views: `docs/review/correction/phase1_manual.png`, `phase2_after_restart.png` at 1440x900.

## Coordinator correction review - September 12, 2026

- Fresh full suite passed 144 tests in 17.137 seconds. Log: `/tmp/gb-v1-correction-review-tests.log`.
- Authenticated read-only checks of the correction preview on port 5097 returned HTTP 200 for the page and planner state. The served roster count is 331. The agent's 28 browser checks were not repeated in this review.
- The two main UI corrections have implementation and test evidence. The isolated preview is available for user testing. No private-server restart or deployment performed.
- Remaining pre-deployment correction: `_plan_for_manual_entry` must enforce selected tiers and counts consistently for requests that omit `entry_limit`, and must not skip the existing-plan tier guard merely because an explicit limit is supplied. The current page sends the normal fields, but the backend fallback still does not satisfy the shared selection rules. Reuse the recorded distribution as the default, validate any explicit selection update, and reject contradictory inputs without mutating the plan. Keep this separate from UI or solver expansion.

## Live release - September 12, 2026

The user explicitly requested making the updated version live.

- Closed the remaining selected-entry-limit gap in `app.py`: omitted selections use the saved distribution, explicit limits must agree with selections, selection updates must accommodate existing entries, and successful additions persist the new distribution. Rejected requests leave the plan unchanged. Added one regression in `test_planner_api.py` and corrected four earlier draft-construction tests to explicitly increase their selected entry limits.
- Full suite passed 145 tests in 17.050 seconds. Log: `/tmp/gb-live-release-tests.log`.
- Backed up the current authenticated private runtime, which now held nine lineups, before restarting. Tested loading its saved state with the release code in an isolated copy first. Backup: `/Users/shyampatel/Library/Application Support/GameBlazers/shyam-preview/release_backups/20260912T073811Z`.
- Restarted only Shyam's port 5014 server, in screen session `gbshyam_live`, and restored the saved plan. Verified process environment points at the correct private instance root and the served roster count is 331.
- Compared every lineup ID, card placement, manual lock, saved live-state field, schedule, simulated time, selected distribution, exclusions, salary source, and projection override against the backup. All preserved. No generated test plan was copied over the live plan.
- Authenticated checks through https://knowing-highway-tin-cas.trycloudflare.com returned the updated page and preserved nine-lineup state. Anonymous page, state, and export requests returned 401. Existing tunnel and keep-awake processes retained. Updated stale process metadata and stop command.
- Attempted browser verification of the public page, but the in-app browser could not complete HTTP Basic authentication. Public authenticated HTTP and state verification passed; no new browser visual verification is claimed.
- Private credentials unchanged. Other users' servers untouched. Service still depends on this Mac and the existing tunnel, as documented in `SHARING.md`.
