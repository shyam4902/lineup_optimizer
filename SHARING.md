# Separate sharing workspaces

## Allow Listed cards in lineups, September 12, 2026

Cards with status `Listed` are now treated as eligible and can be used in lineups alongside `Active` cards. Both `optimizer_core.py` and `multi_lineup_planner.py` now accept `("active", "listed")` status.

Deployed to all shared workspaces and local preview ports. All existing lineups, placements, locks, entered scores, and schedules were preserved. In Shyam's workspace, 17 listed cards (including Ja'Marr Chase, Nico Collins, Puka Nacua, Christian McCaffrey, Jalen Hurts, Justin Jefferson) are now matched and eligible, raising eligible cards from 304 to 321.

## Manual editing after kickoff, September 12, 2026

Manual moves, exchanges, replacements, slot clearing, and lineup removal no longer reject players because their game started or finished. Player scores remain attached to the athlete. Explicit lineup and slot locks still apply to moves and clears. Automatic generation and rebuilds continue to preserve started placements.

Deployed to all four shared workspaces and local ports 5001 and 5005 with fresh runtime backups. Existing placements and live player state were preserved. Shyam still has all 19 nonzero entered scores. The suite passed 146 tests, including final/in-progress exchanges and clearing/reselecting final players. An isolated browser test exchanged those players with the final score intact and no console errors.

## Roster salary correction, September 12, 2026

Roster-export `Salary` is now authoritative across the planner, rebuilds, saved-plan loading, and CSV exports. The exported value already includes the card multiplier. Projection feeds supply raw points only.

All four shared workspaces and both local app processes were restarted with the corrected code. Shyam retained 331 cards and all 33 lineup placements and locks. Jasmine retained 194 cards and all 17 lineups. Friend and Alex retained their empty workspaces. Local ports 5001 and 5005 each retained 322 cards and no active lineups. Live player state, projection snapshots, overrides, exclusions, and contest settings were restored per instance. Existing active plans were saved with corrected costs. Four of Shyam's existing lineups now exceed the cap; no cards were moved automatically.

The shared URLs, usernames, passwords, tunnels, and keep-awake processes are unchanged. Each private root's `processes.json` and `Stop sharing.command` now point to the current server. Servers run in screen sessions `gb_salary_shyam`, `gb_salary_friend`, `gb_salary_alex`, `gb_salary_jasmine`, `gb_salary_local`, and `gb_salary_local_alt`. Per-instance backups contain the captured runtime, prior saved data, release source, startup script, and verification results. Their paths are recorded in `/tmp/gb-salary-release-NAME.txt`; Shyam's backup is inside his private root's `release_backups/` directory.

Verification: 146 tests passed. Authenticated requests through Shyam's public link confirmed all 331 card salaries match the roster export and all 33 lineups remain present. Anonymous page, state, and export requests still return 401. Browser verification found the corrected roster-salary label and no console errors or warnings. Contest-priority generation changes remain pending.

## Shyam V1 release, September 12, 2026

The updated one-page lineup workspace is live at https://knowing-highway-tin-cas.trycloudflare.com with the existing `shyam` login and password. Only Shyam's server was restarted. The other people's workspaces were not restarted.

The authenticated runtime loaded 331 roster cards and restored all nine existing lineups, their exact card placements and locks, live player scores, kickoff schedule, selected contest tiers, exclusions, and overrides. The public page serves the updated code; anonymous page, state, and export requests return 401. The release suite passed 145 tests.

The server runs in screen session `gbshyam_live` on loopback port 5014, using `/Users/shyampatel/Library/Application Support/GameBlazers/shyam-preview`. The existing tunnel and keep-awake processes remain in place. `processes.json` and `Stop sharing.command` now identify the current server. Backups of the pre-release runtime state, previous saved file, current saved data, and release source are in that private root under `release_backups/20260912T073811Z`.

This is still the existing Mac-hosted private service. Keep the Mac awake and online. Save Plan remains explicit; after a later server restart, use Load Plan. The test instance on port 5097 is separate and is not the live workspace.

## Earlier sharing setup

Three named workspaces now run separately:

| Person / username | URL | Local port |
| --- | --- | --- |
| shyam | https://knowing-highway-tin-cas.trycloudflare.com | 5014 |
| alex | https://ages-newly-dining-reef.trycloudflare.com | 5015 |
| jasmine | https://cancel-cartridge-super-smallest.trycloudflare.com | 5016 |

Each has a different password in `/Users/shyampatel/Library/Application Support/GameBlazers/NAME-preview/password.txt`, replacing NAME with the username. Give each person only their own link and credentials. Each directory also has its own `Stop sharing.command`, logs, schedule, snapshots, roster, and saved plans. `PLANNER_USERNAME` selects the login name; existing instances default to `friend`.

At the original setup, Shyam's workspace started with 322 cards and imported Week 1 kickoff times. That count is historical; the current release verification above found 331. His original four-lineup saved plan was not copied because all 22 saved card IDs failed to match the roster at that time. The source file remains intact. Alex and Jasmine started without personal data and upload their own rosters.

The original unnamed friend preview below remains running independently.

The temporary private preview is running at https://bill-much-john-name.trycloudflare.com as of September 9, 2026. It requires username `friend` and the password in `/Users/shyampatel/Library/Application Support/GameBlazers/friend-preview/password.txt`.

Each preview has 431 Week 1 projections from the September 8 snapshot and the updated contest rules. No personal roster, entered lineups, or projection overrides were copied into friends' workspaces.

## Instructions for your friend

1. Open the link and sign in.
2. Upload your GameBlazers roster CSV using the box at the top.
3. Click Generate Lineups. The app fills as many valid entries as the inventory and contest limits allow; no target count is required. Contest settings are optional.
4. Before using live swaps, open Live Lineup Manager and import dashboard kickoff times for season 2026, week 1.
5. Click Save Plan after changes. Use Load Plan after a server restart. Saved plans include scores and kickoff times. Enter actual swaps in GameBlazers yourself.

## Running and stopping

Keep this Mac open and connected to the internet. Idle sleep is prevented while the tunnel runs; closing the lid, shutting down, or losing the network can interrupt the link. This temporary link changes when the tunnel is recreated.

To stop sharing, double-click `/Users/shyampatel/Library/Application Support/GameBlazers/friend-preview/Stop sharing.command`. Saved files remain in that folder.

The server listens only on `127.0.0.1:5013`. The tunnel exposes this instance alone. Runtime logs and process IDs are in the same private folder. This is one friend's workspace, not a multi-account service. Anyone given these credentials shares that workspace.

Actual scores still need manual input. Payout score thresholds remain modeled estimates even though contest rules and prize tables have been updated. Save Plan is explicit, not automatic.

## Verification

- 82 tests passed, including password protection, cross-site request rejection, roster isolation, saved live state, automatic lineup counts, and timeout handling.
- A temporary test workspace imported the real 322-card roster, matched 163 players to kickoff times, generated a valid Scorcher lineup, and restored its exact cards, scores, and schedule after restarting state.
- Public HTTPS rejected anonymous page, state, and export requests. Authenticated requests returned the empty friend workspace.
- The public page loaded in a mobile browser context; navigation wraps within the screen.
