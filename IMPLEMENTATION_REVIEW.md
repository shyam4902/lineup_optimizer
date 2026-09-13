# Weekly planner review, September 8, 2026

Verdict: changes required before calling the handoff complete. The app has a working foundation, but the central payout optimization requirement is missing and several edits can produce misleading validity or stale projections.

Reviewed the current working implementation against this conversation's requirements and `IMPLEMENTATION_HANDOFF.md`. No application code was changed during review. The existing 49 tests passed in 3.339 seconds. Additional Python checks reproduced the failures below. API reproductions used temporary storage and a replacement application state. No new browser verification was performed during this review.

## Required fixes

1. **P1. Optimize the weekly payout objective.** `multi_lineup_planner.py:609` maximizes the sum of adjusted projections. It only evaluates payout after choosing the cards. Contest counts are fixed before optimization, so the planner cannot recommend a better Scorcher/Wildfire mix based on payout. Use the explicitly provisional historical payout objective until calibrated scenarios exist. Include a counterexample where the chosen weekly payout exceeds the greedy alternative. The current test named `test_synthetic_allocation_proves_joint_solver_beats_greedy` only asserts that all ten cards contribute 170 points. It never computes a greedy plan or compares payouts.

2. **P1. Accept the untouched DFF export.** `dff_client.py:306` requires `Player` or `player_name`, and its projection aliases omit `ppg_projection`. Calling the parser with the supplied file raises `ValueError`. Join `first_name` and `last_name`, accept `ppg_projection`, and preserve the existing supported CSV formats. Use the actual file below as the regression input, without modifying it. Retain week, game dates and slate provenance rather than collapsing every upload into `csv_import`. The legacy upload path also needs an adapter if it promises to accept the same source format; keep the exact solver unchanged.

3. **P1. Validate ownership across the entire plan after every edit.** `multi_lineup_planner.py:746` builds a card-to-lineup dictionary that overwrites the first use with the last. It then validates only the edited lineup. Reproduction: generate two valid Scorcher lineups and swap the first lineup's QB card into the second lineup. The API logic accepts the swap and both lineups remain valid. Count every use and revalidate all affected lineups, or reject the conflict before mutation. `validate_lineup` also ignores `is_eligible` and does not compare the actual slot structure against the contest's required slots. Setting a selected card to ineligible still leaves its lineup valid. Invalid lineups must not contribute an actionable payout total.

4. **P1. Handle infeasible solves before reading decision variables.** `multi_lineup_planner.py:668` ignores failure status and proceeds to interpret solver values. Inventory scaling at line 541 rounds every contest up to one, even when inventory supports only one lineup in total. Reproduction: one QB, two RBs, five WRs and two TEs, requesting one Scorcher and one Wildfire, returns two lineups, one invalid, despite reporting a one-lineup inventory limit. Return only validated feasible lineups, explain any shortfall, and preserve the previous plan if regeneration fails. Distinguish a validated incumbent at a time limit from a proven optimum.

5. **P1. Synchronize source projections, inventory and active assignments.** There are several connected stale-state paths:
   - `join_cards_with_projections` uses `matched_proj.raw_projection` when no override is present. A fetched snapshot can already contain the override. Reproduction: source 21, override 30, refresh, reset override. The card stays at 30 while reporting `is_overridden=False`. Reset must use `source_projection`.
   - CSV and roster upload routes recompute `STATE.planner_cards` but leave the active plan and its replacement-card pool unchanged. Refresh and override routes update selected slots but leave `active_plan.roster_cards` stale. A subsequent swap can reintroduce old values.
   - `fetchAppState` in `templates/index.html:1064` only renders the active plan. Browser inventory and swap candidates remain the values embedded at initial page load. The state endpoint does not return the full updated card pool or overrides.
   - `/api/check_update` saves a new snapshot to disk without adopting it into the running app's state.
   Route these operations through a shared refresh/revalidation step. Test an upload, refresh, override, reset and swap in the same open browser tab, without reloading the page.

6. **P1. Reject unusable snapshots before replacing good data.** A header-only CSV with `Player,Projection,Position,Team,Salary` returns HTTP 200 and saves zero offensive players over the previous good snapshot. Twenty empty JSON objects also pass the raw row-count gate and parse to zero players. Validate parsed content, finite numbers, intended slate and plausible completeness before committing a snapshot. Malformed numeric fields currently become zero, and missing DFF salaries silently fall back to roster salaries. Report unavailable weekly salary explicitly. Reject ambiguous player matches rather than keeping the first name match. The join currently accepts a different team and a zero multiplier, contrary to the existing eligibility rules.

7. **P2. Finish the requested controls and respect their values.** No editable minimum/maximum salary controls, individual slot-lock buttons or exclusion controls are exposed. The slot-lock API exists but the UI only calls the whole-lineup lock route. `generateWeeklyPlan` turns a deliberate zero contest count into five using `parseInt(...) || 5`. When a distribution is supplied, the backend ignores the total target. The backend's default-distribution overflow also exceeds its configured entry limits. Enforce confirmed limits consistently and report unknown limits as unknown. Preserve locked lineup identities when the distribution changes; positional lineup IDs currently allow locks to disappear if a preceding contest count changes.

8. **P2. Save and load the actual weekly workspace.** Generated plans hardcode `slate_id="current_slate"` and `salary_source="dff"`. The load route resolves saved cards against current inventory and ignores saved contest settings and override values. It can silently empty slots or substitute current values under old metadata. CSV/roster replacement can also leave cards in the plan that no longer belong to the current inventory. Save actual slate/source/settings and enough card identity to restore the workspace. Validate differences on load and preserve unresolved manual selections with clear errors. Test a process restart, roster reorder, changed slate and override reset.

9. **P2. Isolate tests from the user's files and the network.** `test_planner_api.py` imports the global `STATE`, uses the actual default roster/snapshot, calls a live update endpoint and writes the real override file. Its override test sets and removes Patrick Mahomes's value without preserving any existing value. The tests can modify a user's setup or fail in a clean checkout. Use temporary stores and deterministic source data. Add meaningful assertions for the failures above before repeating the completion claim.

## DFF source contract

Reference file: `/Users/shyampatel/Desktop/GB/Projections/DFF_NFL_cheatsheet_2026-09-09.csv`.

| Meaning | Exact export columns |
| --- | --- |
| Display name | `first_name` plus `last_name` |
| Position and team | `position`, `team` |
| Weekly salary | `salary` |
| Raw projected points | `ppg_projection` |
| Provenance | `week`, `game_date`, `slate` |
| Additional source context | `injury_status`, `opp` |

The file contains 477 rows: 147 RB, 33 QB, 168 WR, 97 TE and 32 DST. That leaves 445 offensive rows before duplicate reconciliation. Two normalized names repeat, Charlie Jones and James Mitchell, leaving 443 distinct normalized names. Inspect complete rows before deciding whether duplicates are equivalent. This projection-feed deduplication must remain separate from ownership copies in the roster.

The saved API snapshot for slate `255DE`, fetched at `2026-09-08T04:38:31.493109+00:00`, has 431 offensive players from 463 raw rows. Compared by normalized name, 17 names occur only in the CSV and five only in the snapshot. Common salaries match. Many projection differences are source precision, such as Jahmyr Gibbs at 23.8 in CSV and 23.81 in the snapshot. Do not treat those precision differences as column errors, or treat the different pool counts as proof that either source is complete. Reconcile the source timestamps, player filtering and missing players before declaring parity. Preserve API IDs separately because this CSV does not provide them.

## Checkpoint corrections

- Contest configuration: historical Scorcher/Wildfire thresholds and payout bands match the earlier reviewed figures. Spark is disabled for weekly recommendations. Entry-limit provenance still needs an explicit source reference; an observed entry count alone is not proof of a maximum.
- DFF import: partial. A direct fetch path exists, but native CSV support and snapshot validation fail.
- Editable workspace: partial. Swaps exist, but reuse validation, refreshed state, per-slot controls and persistence need work.
- Payout allocation: incomplete. Deterministic labels are appropriately provisional, but dollars do not influence optimization. The separate Gaussian evaluator has a default standard deviation of 16 without supplied calibration and collapses upper ranks into the lowest prize at the available cutoff. Keep it explicitly experimental; it is not a complete evaluation of all prize bands.
- Update readiness: a runnable checker exists. The handoff provides no enabled schedule, notification delivery verification, or concrete scheduling blocker. Do not label automated alerts complete based on the checker alone.

Preserve the unchanged exact optimizer core and the useful DFF fetch, contest data and editable layout. Correct the shared state and validation paths, add the missing payout objective, then rerun isolated tests and the full browser workflow before updating the implementation handoff.
