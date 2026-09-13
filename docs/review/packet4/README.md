# Packet 4 review: acceptance on an isolated copy of the private roster

September 12, 2026. Packet 4 of `docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md`.
The full write-up lives in `V1_AGENT_RESULTS.md` under "Packet 4 - September 12, 2026".

## Result

90 of 90 acceptance checks passed, and the existing suite ran 133 tests with 0 failures after the last correction. One real defect was reproduced and fixed.

## Artifacts

| File | What it is |
| --- | --- |
| `acceptance_check.py` | Phase 1 drives the workspace and the API end to end. Phase 2 compares state after a real restart. |
| `acceptance_check.txt` | Output of both phases, 82 checks then 8 checks, all passing. |
| `restart_preview.sh` | Stops and restarts only the listener on port 5098, and refuses to touch a process that is not the packet 4 instance. |
| `workspace_generated.png` | Workspace after generating eight lineups across four tiers. |
| `workspace_after_packet4_flow.png` | Full page after the manual edits, the Thursday Final score, rebuild, regenerate, save and load. |
| `workspace_after_restart.png` | Workspace after stopping and restarting the server and loading the saved plan. |

## How to re-run

```bash
cd /Users/shyampatel/Desktop/GB/lineup_optimizer
docs/review/packet4/restart_preview.sh
.venv/bin/python docs/review/packet4/acceptance_check.py 1
docs/review/packet4/restart_preview.sh
.venv/bin/python docs/review/packet4/acceptance_check.py 2
.venv/bin/python -m unittest discover
```

The preview is `http://127.0.0.1:5098`, username `friend`, password in `/tmp/gb_p4_preview/password.txt`. Instance root `/tmp/gb_p4_preview`, roster copied from the live Shyam workspace. Stop it with `screen -S gbp4 -X quit` and `kill $(lsof -ti :5098)`.

## The defect that was fixed

Regenerating all replaced a started player. `POST /api/planner/generate` read no live state, so a Final Thursday player in an unlocked slot went back into the solver pool. Puka Nacua (LAR, 1.40x, Final at 2.0 raw points) was swapped out for CeeDee Lamb in `inferno_2`.

`app.py` now builds the solver input with started and final placements marked locked, and the unselected-contest guard covers those placements so a deselected tier returns an explanation instead of a solver failure. `test_planner_api.V1Packet4StartedPlayerTests` holds four tests for the behaviour, including the two-lock regeneration case packet 3 left uncovered. Both regeneration tests fail when the fix is reverted.

## Open items

- In-progress players with no remaining estimate fall back to the seeded `remaining_projection` in `live_lineup_manager.PlayerLiveState.effective_mean`. The `unknown (no estimate)` label lives in `templates/index.html` (`slotLiveValues`, `appState.remainingSet`), so it does not survive a reload. Needs a decision on whether the model carries its own unknown flag.
- A game-locked slot comes back from regeneration with `is_locked: true`, so `buildSlotRow` shows a manual lock icon and an `Unlock slot` button for it. Edits are still refused by the game lock and the next regenerate re-locks it. A distinct game-lock display is later polish.
- A single-entry rebuild still uses `compute_contest_benchmark` and ignores cross-lineup card allocation.
- `Add empty entry` still needs an existing plan because selected tiers live on the plan.
