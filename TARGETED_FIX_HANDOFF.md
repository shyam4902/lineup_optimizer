# Targeted fixes handoff

Superseded September 11 for product scope and execution prompts by `docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md` and `V1_AGENT_PROMPTS.md`. Keep this file as the earlier implementation and runtime record. In particular, exclusion must no longer silently clear protected placements.

Updated September 10, 2026. The user stopped the broader implementation to conserve usage. Continue with small, named edits and short handoffs. Do not restart the redesign or repeat repository discovery.

## Accepted behavior

- Generate as many valid lineups as available card copies and the user's selected contest tiers allow. The user explicitly selected "Use my selected contest tiers."
- Maximize projected points within salary and contest rules. Do not use estimated payouts or a benchmark percentage to reject otherwise valid lineups.
- Use eligible Active cards with positive matched projections. Apply each card multiplier once. Never reuse a card copy across entries.
- Make lineup viewing, legal player swaps, rebuilding unlocked slots, and excluding or restoring players easy.
- About 3 projected points per $1,000 is context, not an eligibility gate. Flamethrower has no verified payout schedule for optimization.

## Changes already on disk

- `multi_lineup_planner.py`: default quality threshold is zero; selected contest counts are upper limits; entry count is the first objective and projected points the second. Removed payout terms from allocation. Added a feasible seed using the existing exact lineup solver, then the global solver improves it.
- `app.py`: selected contest validation, persistent player exclusions, legal swap candidates, manual swap locks, and remove/rebuild entry routes. Save/load retains exclusions.
- `contest_config.py`: removed unverified Flamethrower payout and historical threshold data.
- `templates/index.html`: selected tier controls, Generate all lineups, searchable lineup viewer, swap dialog, roster exclusions, and projected score/salary summaries. Removed the payout-oriented generation controls.
- `test_planner_api.py`, `test_weekly_planner.py`, `test_updated_contests.py`: coverage and expectations for this behavior.

The repository already contained extensive unrelated changes. `optimizer_core.py` was dirty before this work and was not changed by this pass. Do not reset the worktree, stage everything, or use the git diff as the boundary of this task.

## Evidence and limits

- The last completed full suite passed 113 tests in 8.048 seconds. Log: `/tmp/gb-score-tests3.log`.
- That run predates the final small changes that allow swapping around another excluded player and unlock an excluded player's slot and parent lineup. Those changes still need a test run.
- An isolated copy of the actual 331-card roster generated 35 valid lineups in about 17 seconds: Inferno 8, Volcano 6, Flamethrower 6, Flex Appeal 6, Scorcher 5, Pyro 4. No card copy was reused. This is a tested result, not a promise of mathematically optimal projected points.
- Browser generation reached the 35-entry viewer. The final frontend edits and the complete swap/exclude/save flow have not had a final browser check.
- Source changes have not been deployed by restarting the private server. Do not claim the user's running app has all fixes yet.

## Runtime and backups

Last observed, not freshly reverified at handoff:

- The correct private instance root is `/Users/shyampatel/Library/Application Support/GameBlazers/shyam-preview`, with 331 cards and 305 eligible cards. The authenticated server was on port 5014. Credentials stay in the private password file and must not be printed.
- The runtime held two active lineups with no locked slots, while the older saved file held four. Preserve the current runtime before any restart. Do not load the older saved plan over it.
- A Chrome tab on port 5001 showed an older 322-card instance. Do not use that instance as proof of the user's data.
- `processes.json` had a stale server PID. Verify the current process and instance root before changing anything. Preserve the existing tunnel and keep-awake processes.
- A temporary preview ran on port 5017 using `/tmp/gb-score-first-preview`. Its generated lineups and browser test edits must not be copied into the private instance.
- Before-edit source copies and private test snapshots are in `/tmp/gb-score-first-backup`. These are temporary, local files. Do not commit or share the private snapshots.

## Bounded follow-up prompts

Run one prompt per pass. Update this file with the outcome and stop. Do not turn a check into a rewrite.

### 1. Finish the existing change

> Work in /Users/shyampatel/Desktop/GB/lineup_optimizer. Read TARGETED_FIX_HANDOFF.md. Run `.venv/bin/python -m unittest discover -v` once. If it fails, fix only a failure caused by the score-first generation, exclusions, swaps, or rebuild changes already described here. Reuse existing tests and helpers. Do not add features, restyle the UI, modify optimizer_core.py, restart private servers, or commit unrelated work. Record the result and exact files changed in the handoff, then stop.

### 2. Check the editing flow

> Read TARGETED_FIX_HANDOFF.md. On an isolated copy of the private roster, use the existing browser tools to check one legal swap, excluding and restoring that player, rebuilding unlocked slots, and save/load. Verify card copies are unique and manual locks survive. Inspect the viewer once for clipped controls. Fix only a reproduced failure in these flows. Do not redesign, add dependencies, regenerate the private user's plan, or deploy. Record the outcome and stop.

### 3. Check generation safeguards only if needed

> Read TARGETED_FIX_HANDOFF.md. Inspect only generation's handling of selected contest limits, locked entries, and started players. Confirm timeout fallbacks cannot return entries from deselected contests or exceed selected limits. Confirm regeneration cannot silently replace started players. If a defect is demonstrated, add one narrow regression check and the smallest fix in the existing path. Do not refactor the solver or expand the UI. Record the result and stop.

## Deployment remains a separate step

Only resume deployment when requested. First preserve the authenticated current runtime plan and live state. Restart only the correct private instance, then verify the authenticated roster count, instance root, active lineup cards, and live state against the preserved snapshot. Test the served page, not just files or solver output. Do not replace the user's current lineup choices with the temporary 35-lineup test result.
