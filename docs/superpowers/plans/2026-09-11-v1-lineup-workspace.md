# V1 lineup workspace implementation plan

For agentic workers: implement one numbered packet at a time using the executing-plans workflow. The user will hand off the prompts. Do not launch other agents or continue into the next packet automatically.

Goal: make the app useful for viewing and manually managing all of the user's lineups and roster during the week.

Architecture: keep the existing Flask app, card and lineup models, live player state, persistence, and solver. Put the existing capabilities into one workspace and fix the specific state rules that prevent editing. No framework rewrite or new optimization model.

Tech stack: Python, Flask, PuLP, Jinja, vanilla JavaScript and CSS, existing unittest suite.

## Scope and precedence

This September 11 plan supersedes the product priorities and execution prompts in `TARGETED_FIX_HANDOFF.md`, `NEXT_AGENT_PROMPTS.md`, and earlier implementation documents. The older targeted handoff still contains useful backup and runtime references. Current user instructions supersede older preferences for payout optimization.

V1 is a manual lineup workspace with generation as a helper. Success means the user can see, arrange, score, lock, and regenerate their real lineups without losing choices. A larger lineup count alone is not acceptance.

"All lineups" means all created entries in the selected contest tiers, bounded by entry limits and owned card copies. It does not mean enumerating every possible combination. Use the existing generation objective and remove any remaining payout or quality gates from this workflow. Do not spend this pass tuning optimality or adding strategy recommendations.

## Required workspace

- One main page shows every created lineup with its player rows visible by default. Use a compact grid or list with normal scrolling. Selecting one lineup must not hide all the others.
- A roster panel on that same page shows all owned copies, including assigned, excluded, and ineligible cards. Search and filters operate on the full roster. Each row identifies player, position, card multiplier, adjusted projection, salary, assignment, and availability reason.
- Each lineup shows contest, player slots, projections, salary used and remaining, actual points, remaining projected points, and validity or incomplete status. Do not label an unfinished lineup eliminated because of a payout model.
- Keep selected tiers, generation, add entry, save/load, and edit feedback within this workspace. Keep existing upload/setup available without making users leave the workspace for routine edits.
- Explicit Swap, Move, and Exchange controls are sufficient. Drag and drop is outside this pass. Show legal destinations and explain rejected actions inline.
- Hide payout estimates, return estimates, automatic rescue recommendations, and demo scenarios from the normal V1 flow. Preserve unrelated backend code unless it directly prevents V1 behavior.

## State rules agents must share

1. Card copies and athletes are different identities. A physical copy occupies at most one slot across the plan; an athlete appears at most once within a lineup. Different owned copies may appear in different lineups. Use existing IDs, never player-name guesses.
2. An explicit manual placement locks that destination slot against regeneration. A manual lock can be unlocked. A started or final player has a game lock that the manual unlock control cannot remove. A whole-lineup lock freezes all its slots.
3. Regenerate all preserves lineup IDs and every locked placement and score, then fills or adjusts unlocked slots using the selected tiers. Rebuild one lineup does the same for that lineup and does not take cards from other entries. A Thursday player must not prevent rebuilding the rest of their lineup.
4. Started players cannot be newly assigned elsewhere. If schedule data is missing, show that status and allow manual Upcoming, In progress, and Final state entry. Do not pretend kickoff protection exists without supporting state or time data.
5. Exclusion applies to future selection across the athlete's copies. Excluding someone must never silently unlock or remove a protected placement. Keep existing protected copies visible with a warning; excluded unlocked copies can be manually cleared or replaced on regeneration. Including restores selection eligibility only when the normal roster/projection rules also pass.
6. Add an empty entry within a selected contest's limit, fill or clear unlocked slots, and keep incomplete drafts editable. Show which required slots remain. Validate position, salary ceiling, collection restrictions, and identity during edits; enforce minimum salary and full composition before calling an entry valid. Do not trap the user because an intermediate draft is incomplete.
7. Moving a card into an empty slot clears its source. Exchanging occupied slots swaps both cards in one transaction. Validate both resulting entries before applying either change. Illegal moves, locked sources or destinations, or unavailable copies leave the original state unchanged.
8. Score entry is an athlete's raw fantasy points before card multiplier, clearly labeled. Apply the multiplier once for each assigned copy. Preserve the original projection as a separate value. Zero and negative actual points are valid; missing score is distinct from zero. Reject nonnumeric or nonfinite scores before mutating state.
9. Final players contribute actual points and zero remaining projection. Upcoming players contribute their projection as remaining. For in-progress players, show actual points and a separately entered remaining estimate; if no remaining estimate is supplied, show it as unknown rather than silently adding the full pregame projection again. No automatic live-score feed is required.
10. Save/load and restart preserve assignments, locks, scores/status, exclusions, selected tiers, and projection source. Reuse existing Save with a visible unsaved/saved indicator. Failed saves retain the last saved file. A generation result must not overwrite edits made while it was running; disable conflicting edits during the request or reject stale results using an existing mechanism.
11. Conflicting selections, such as deselecting a tier containing protected entries, produce an explanation and preserve the plan. Solver failure or timeout cannot discard protected entries or return entries outside selected limits. Missing roster matches stay visible as unresolved; never silently substitute another card.

## Current evidence and code map

The September 10 suite passed 113 tests before the last small edits. An isolated 331-card roster generated 35 valid lineups without duplicate card copies. These are previous results, not current acceptance or deployment evidence. This planning pass did not run tests or restart servers.

Relevant existing files, all relative to `/Users/shyampatel/Desktop/GB/lineup_optimizer`:

| File | Reuse or inspect |
| --- | --- |
| `app.py` | `api_planner_generate`, `api_planner_swap`, `_swap_candidates`, `api_planner_exclude`, `api_planner_edit_lineup`, `api_planner_toggle_lock`, save/load, `api_live_update_player_state` |
| `multi_lineup_planner.py` | `WeeklyPlan`, existing slot/card models, `validate_lineup`, `swap_card_in_lineup`, `compute_contest_benchmark`, `solve_multi_lineup_allocation` |
| `live_lineup_manager.py` | `PlayerLiveState`, `is_locked`, effective score calculations; reuse score state, skip strategy expansion |
| `templates/index.html` | Existing lineup rendering, swap dialog, roster rendering, live score controls, app state and API calls |
| `test_planner_api.py`, `test_weekly_planner.py`, `test_live_manager.py` | Existing regression suite and fixtures |

Source inspection on September 11 found these specific starting gaps:

- `api_planner_edit_lineup` rejects rebuilding the entire lineup when any player has started.
- `api_planner_exclude` clears slot and whole-lineup locks for excluded players. This conflicts with the agreed protection rules.
- `_swap_candidates` filters out assigned copies, so the current swap endpoint does not provide a two-lineup exchange.
- The viewer uses `renderSidebarLineups` and `renderLineupDetail`; all entries are listed, but only one has full detail at a time.
- Actual-score state and save/load already exist. Reuse them; the score update route currently converts inputs while mutating state, so validate the full update first.

## Execution rules

Work in the nested `lineup_optimizer` repository. It has extensive pre-existing uncommitted work. Do not reset files, stage everything, commit other agents' work, or reconstruct a checkout from HEAD and lose the current implementation. Each agent owns only its packet's files, must preserve others' edits, and must stop at its boundary.

Run packets sequentially because they share app state and files. Read the preceding packet's result before editing. If a missing decision prevents a safe implementation, record one specific question instead of inventing a strategy system. Do not ask again about selected tiers or the scope already agreed here.

### Packet 1: Preserve locked placements and scores

Ownership: `app.py`, `multi_lineup_planner.py`, `live_lineup_manager.py` only where needed, and their existing test files. No UI edits.

- [ ] Run the baseline once with `.venv/bin/python -m unittest discover -v` and record failures before editing.
- [ ] Trace generation, single-lineup rebuild, exclusions, and game/manual locks using the listed functions. Reuse the existing validators and persistence.
- [ ] Add narrow regression cases for rules 2 through 5 and 8 through 11, then fix only demonstrated gaps. Preserve a final Thursday slot while replacing an unlocked Sunday slot. Confirm exclusion does not remove its protection.
- [ ] Check score handling with raw scores of 0, -2, and 10 on a 1.5x copy, expecting 0, -3, and 15 actual points. Final status must leave zero remaining projection. Invalid updates leave previous values intact.
- [ ] Save/load that plan and confirm IDs, locks, scores, exclusions, and selected tiers survive. Test generation failure leaves the plan intact.
- [ ] Run the relevant existing test modules after changes. Record the exact endpoint fields and score meanings the UI must use.

Stop when these rules pass. Do not tune solver objectives or add recommendations.

### Packet 2: Manual construction and transfers

Dependency: packet 1's state rules and handoff are complete.

Ownership: `app.py`, existing mutation/validation functions in `multi_lineup_planner.py`, and `test_planner_api.py`. No UI edits.

- [ ] Reuse the existing slot model and edit routes to support an empty entry, clearing an unlocked slot, and partial drafts. Add only missing operations.
- [ ] Add a move/exchange operation using existing card and lineup IDs. Validate a proposed copy of the affected entries before changing the active plan. Return enough updated state for both entries and roster assignments to refresh.
- [ ] Check an available-card swap, a move into an empty slot, and an exchange between two unlocked entries. Confirm each explicit placement locks its destination.
- [ ] Check rejection of an exchange that breaks either salary cap, repeats an athlete, violates a collection/position rule, or touches a game/manual lock. Both entries must remain unchanged.
- [ ] Check that an incomplete draft can be filled step by step, that exclusions prevent new placements, and that adding beyond the selected entry cap fails without mutation.
- [ ] Run `.venv/bin/python -m unittest test_planner_api test_weekly_planner -v`. Record request/response examples for the UI agent, with no private roster data.

Stop after the operations work and their contracts are documented. No drag-and-drop system or generalized transaction framework.

### Packet 3: One-page workspace

Dependency: packets 1 and 2 passed and their endpoint contracts are recorded.

Ownership: `templates/index.html`. Backend changes are limited to a demonstrated missing field, recorded explicitly.

- [ ] Make the main view render every lineup's player rows and a roster panel together. Reuse the existing renderers and controls. Keep full data available to search/filter; no arbitrary four-entry or truncated-player limit.
- [ ] Wire Swap, Move/Exchange, Clear, Lock/Unlock, Exclude/Include, Add entry, Rebuild unlocked, Generate all, and Save/Load to the tested operations. Use a small destination picker where needed. No drag-and-drop dependency.
- [ ] Bring manual actual-score and game-status entry into the workspace. Show pregame projection, actual points, and remaining estimate distinctly; label multiplier treatment.
- [ ] Refresh every affected lineup and roster assignment after each edit. Retain search, scroll, and selections where practical. Show pending requests, failures, incomplete entries, unsaved state, and protected slots without browser alert chains.
- [ ] Hide payout and scenario controls from the primary flow. Keep projection uploads and contest rules available. Do not remove unrelated backend capabilities.
- [ ] Browser-check a desktop layout with many entries and a narrow layout. All lineup rows must be reachable, controls must not clip, and forms/dialogs need labels, keyboard access, and visible focus. Record the viewport and observed result.

Stop after the page supports the specified actions. No branding exercise, new framework, animations, or speculative panels.

### Packet 4: Acceptance and corrections

Dependency: packet 3 complete. Ownership: existing tests and only files needed to fix reproduced acceptance failures.

- [ ] Use an isolated copy of the private roster. Verify its instance root and roster count against the copied input before testing; the old 331-card count is a reference, not a hardcoded expectation.
- [ ] Generate selected tiers and confirm every resulting lineup is visible on the workspace, with no card-copy reuse, no unselected contests, and no cap violations. Confirm filtered-out roster rows are still searchable.
- [ ] Assemble one real-looking lineup manually. Swap a roster card, exchange two lineup cards, clear/refill a slot, exclude/restore a player, and inspect salary/score changes on both affected entries.
- [ ] Mark one player Final with a low actual score. Keep that slot fixed while manually choosing another available player and rebuilding the other unlocked slots. Confirm no automatic risk or payout decision occurs.
- [ ] Regenerate all, then save and reload. Restart the isolated preview and compare placements, scores, locks, exclusions, and selections with the saved snapshot.
- [ ] Run `.venv/bin/python -m unittest discover -v` after the last correction. Report observed browser behavior separately from automated test results.

Stop with a reviewable local preview and a short result. Do not deploy or replace the user's active plan. Private deployment is a later explicit step that preserves the authenticated runtime before restarting it.

## Result format for every packet

Append a dated entry to `V1_AGENT_RESULTS.md` with packet number, files changed, checks actually run and outcomes, endpoint changes relevant to the next packet, and unresolved issues. Distinguish previous evidence from new checks. Include a screenshot or local preview reference for UI work when available. Do not include passwords, private snapshots, or claims of verification based only on source inspection.

## Future versions, outside today's work

V2 candidate: import DraftSharks PPR central, floor, and ceiling projections when the user provides or authorizes a usable data source. This records the user's historical workflow; current availability and column definitions have not been verified. Preserve source, week/slate, and scoring format; distinguish raw athlete values from card-adjusted values and apply multipliers once. Allow users to compare and sort by floor/ceiling to choose safer or more volatile replacements. Missing bounds remain unavailable, not fabricated.

V3 candidates: optional strategy assistance for rescuing an underperforming lineup, preserving stronger lineups, or choosing risk levels. Add these after observing real use. Payout optimization requires verified contest payout data and separate validation. Floor and ceiling numbers alone are not calibrated probabilities. Do not create thresholds, fetch paid data, add risk controls, or change the current projection feed in V1.
