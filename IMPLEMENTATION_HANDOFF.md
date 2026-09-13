# GameBlazers Weekly Lineup Planner: Implementation Handoff

## Local App URL and Launch Command

- Local URL: `http://127.0.0.1:5005` (or default port `5000`)
- Launch command:
```bash
cd /Users/shyampatel/Desktop/GB/lineup_optimizer
PORT=5005 .venv/bin/python app.py
```
- Test runner:
```bash
cd /Users/shyampatel/Desktop/GB/lineup_optimizer
.venv/bin/python -m unittest discover -v
```
- Standalone update checker:
```bash
cd /Users/shyampatel/Desktop/GB/lineup_optimizer
.venv/bin/python planner_update_check.py
```

---

## Changed and New Files

### Unmodified Core
- `optimizer_core.py`: Kept stable as required. Existing exact single-lineup branch-and-bound logic remains intact.

### New and Updated Modules
1. `contest_config.py`
   - Formalizes contest rules, eligibility, entry caps, and payout bands for Scorcher, Wildfire, and Inferno.
   - Sets Spark to ineligible and disabled (excluded from recommendations).
   - Encodes historical Bayesian model outputs:
     - Scorcher: Weeks 8, 10, 11, 13 (Bayesian cutoffs for Top 300 down to Top 7).
     - Wildfire: Weeks 11, 12, 13 (Bayesian cutoffs for Top 312 down to Top 7).
     - Inferno: Weeks 10, 13 from `Contest_Analyzer/GB_modeling.Rmd` (cutoffs for Top 400 down to Top 10).
2. `dff_client.py`
   - Pulls live structured projections from Daily Fantasy Fuel via the official JSON endpoints:
     - `/data/slates/next/nfl/dk?x=1` (slates)
     - `/data/playerdetails/nfl/dk/{slate_id}?x=1` (players)
   - Filters out 32 DST rows, parses player IDs, draft IDs, salaries, and raw PPG.
   - Parses untouched cheatsheet CSV streams (`first_name`, `last_name`, `ppg_projection`, `draftkings` salary).
   - Preserves user projection overrides across refreshes.
   - Compares consecutive snapshots to distinguish slate changes, projection updates, or unchanged data without overwriting good data on failure.
3. `payout_evaluator.py`
   - Strictly isolates:
     1. Projected lineup score.
     2. Score cutoffs and distribution.
     3. Mutually exclusive finishing bands and prizes.
   - Evaluates deterministic cutoffs as provisional "payout at projected score" estimates, not live expected values.
   - Provides probabilistic evaluator with disjoint bands to avoid double-counting overlapping tiers.
4. `multi_lineup_planner.py`
   - Handles multi-lineup portfolio allocation using PuLP integer linear programming.
   - **Dynamic Contest Mix MIP Selection**: Associates each candidate lineup with a binary activation variable $z_{c, l}$. Mathematically optimizes the selection of Scorcher, Wildfire, and Inferno lineups up to the user target count to maximize total expected weekly payout, strictly bounded by each contest's entry limit.
   - Eliminates manual preset requirements while retaining manual count distribution controls when requested.
   - Tracks owned cards as distinct assets, preventing cross-lineup reuse of the same card copy while allowing separate copies of the same athlete in different lineups.
   - Enforces slot rules, single-lineup athlete uniqueness, contest salary caps, entry limits, and locks.
   - Joins weekly DFF salaries cleanly, applying card multipliers solely to projections.
   - Provides instant recalculation and validation for manual card replacements.
5. `planner_update_check.py`
   - Runnable script that detects `NEW_SLATE`, `PROJECTIONS_CHANGED`, `NO_CHANGE`, or `FAILED` without generating alert spam.
6. `test_weekly_planner.py`
   - 11 focused tests covering multiplier isolation, salary joins, copy count tracking, manual swapping, lock preservation, disjoint payout bands, cheatsheet parsing, and proof that portfolio MIP beats greedy allocation.
7. `test_planner_api.py`
   - 9 integration tests covering Flask REST endpoints for state, slates, dynamic auto-mix generation, card swapping, overrides, and CSV export.

### Modified Files
- `app.py`:
  - Automatically seeds backend projection cheatsheets from `Projections/` on startup.
  - Added REST API routes for multi-lineup planning, DFF refreshes, overrides, locks, swaps, and CSV export.
  - Kept existing `/` and `/optimize` routes backwards compatible so all 14 legacy Flask tests pass without changes.
- `templates/index.html`:
  - Made the **Upload Roster CSV** button prominent and styled as a primary action.
  - Added an **Auto-Optimize Contest Mix (Recommended)** toggle vs manual entry distributions.
  - Indicated backend auto-seeded projection status.
  - Interactive lineup cards with lock and swap controls, real-time metric updates, and dark mode interface.

---

## Test and Verification Results

1. **Automated Test Suite**:
   - Total tests: 54
   - Failures: 0
   - Errors: 0
   - Execution time: ~14.8s
   - Breakdown:
     - 14 tests in `test_app.py` (legacy Flask optimizer tests)
     - 14 tests in `test_optimizer_core.py` (solver and normalization tests)
     - 6 tests in `test_optimizer_fixtures.py` (fixture tests)
     - 11 tests in `test_weekly_planner.py` (planner mechanics, payout MIP formulation, cheatsheet import, and cross-lineup copy reuse tests)
     - 9 tests in `test_planner_api.py` (planner REST APIs, isolated tempdir storage, slot locking, dynamic auto-mix selection, and update checks)

2. **Demonstrated Scheduled Cron Alerts**:
   - Registered a standing background daemon cron job using the `schedule` tool:
     - Cron expression: `0 14 * * 0,2,4` (Sunday, Tuesday, Thursday at 14:00 UTC)
     - Prompt: "Run weekly GameBlazers projection and slate update check: call /api/check_update or execute planner_update_check.run_weekly_update_check() and report any projection deltas or new slates."
     - Status: Active daemon task `task-1024`.

3. **Live Server and API Verification**:
   - Dev server running on `http://127.0.0.1:5005` (Task `task-1030`).
   - Auto-seeded 305 active cards from the user's latest exported roster (`My_roster.csv`).
   - Auto-seeded projections from `Projections/DFF_NFL_cheatsheet_2026-09-09.csv`.
   - Verified dynamic mix generation via live API:
     - 10-lineup request dynamically selected 5 Wildfire and 5 Inferno lineups ($135.00 estimated payout), respecting the 5-entry cap per contest.
     - 12-lineup request dynamically selected 2 Scorcher, 5 Wildfire, and 5 Inferno lineups ($138.00 estimated payout), mathematically allocating inventory across all three active contests.

---

## Review Checklist

1. **Contest Mix**: Solver dynamically chooses the most profitable mix of Scorcher, Wildfire, and Inferno entries. Entry caps are strictly respected without overflow.
2. **Prominent Roster Upload**: The upload button is now a high-contrast primary callout in Step 1.
3. **Backend Projections**: Projections load automatically from repository cheatsheets on startup without manual file upload.
4. **Scheduled Alerts**: Background cron job is active and verified.
5. **No AI Tells**: Prose adheres strictly to unslop rules with no em dashes, active voice, and concrete terms.

- 2026-09-10 wrap: final Week 1 preview review passed 111 tests, verified 331 cards and four valid 90% lineups; current context and limits are in /Users/shyampatel/Desktop/GB/STATE.md and /Users/shyampatel/Desktop/GB/docs/session-handoff.md. Earlier setup/results above are historical.
