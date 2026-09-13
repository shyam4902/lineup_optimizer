# V1 correction prompt

Copy the following into one agent task. This is a bounded follow-up to the four completed packets.

```text
Work in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Read docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md and V1_AGENT_RESULTS.md. All four packets have result entries. A fresh reviewer run on September 12 passed 133 tests. Fix only the two remaining V1 gaps below, with their directly related checks.

1. Persist whether an in-progress player has an explicitly supplied remaining projection. templates/index.html stores this only in appState.remainingSet. Reload resets it, so a supplied estimate is no longer recognized; the backend also still carries a seeded projection when no estimate was supplied. Store this distinction in the existing live player state and save/load path, return it to the page, and remove reliance on a browser-session-only flag. Explicit zero is a known estimate; a blank or cleared estimate is unknown. Do not silently add the pregame projection to live actual points. Preserve raw score units and apply card multipliers once. Handle existing saved plans conservatively without guessing that a seeded value was manually entered. Test blank, zero, and positive estimates through update, clear, reload, save/load, and an isolated restart. Keep final players at zero remaining projection. Do not change payout or risk models beyond any minimal compatibility adjustment required by this state fix.

2. Allow the first empty manual entry without generating lineups first. api_planner_edit_lineup currently rejects every action when STATE.active_plan is absent. Reuse WeeklyPlan, selected contest controls, and add_empty_lineup to initialize a manual plan from validated current selections. Do not invoke the solver just to create that first draft. Enforce the selected entry count as well as the contest maximum; add_empty_lineup already accepts entry_limit, but the route currently does not pass the selected count. Reject zero-count and unselected tiers. Test a first draft, filling it manually, reaching a selected limit below the contest maximum, and save/load. Keep existing plans and selected tiers intact on rejected requests.

You own only the affected code in app.py, live_lineup_manager.py, templates/index.html, multi_lineup_planner.py if necessary, and existing relevant tests. You are not alone in this codebase; preserve other agents' changes and pre-existing dirty work. Reuse existing helpers. No redesign, framework changes, optimization tuning, strategy features, new agents, or private-server deployment.

Reproduce the gaps first. Make the smallest fixes and test the actual page on an isolated preview. Run the relevant tests during edits and the full existing suite once after the final correction. Append measured results, files changed, and preview details to V1_AGENT_RESULTS.md. Stop after these fixes and report anything unresolved.
```
