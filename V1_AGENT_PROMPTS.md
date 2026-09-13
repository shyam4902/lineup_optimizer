# V1 agent prompts

Use these in order, one agent pass at a time. Each agent must finish its result entry before the next starts. They share files, so do not run implementation packets concurrently.

## First agent

```text
Work in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Read docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md. It is the current product scope and supersedes older payout-oriented plans and prompts.
Execute packet 1 only: preserve locked placements and scores while regenerating unlocked slots. Reuse existing state, solver, routes, persistence, and tests. You own the backend files and existing tests listed in packet 1. You are not alone in this codebase. Preserve other agents' changes and all pre-existing dirty work. Do not restyle the UI, tune optimization, add strategy features, launch agents, restart the private server, or commit unrelated changes.
Append a concise result to V1_AGENT_RESULTS.md with files changed, checks actually run, any endpoint/score contract the next agent needs, and unresolved issues. Stop after packet 1.
```

## Second agent

```text
Work in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Read docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md and V1_AGENT_RESULTS.md. Confirm packet 1 passed before making dependent changes.
Execute packet 2 only: empty entries, editable drafts, clear/refill, and safe moves/exchanges between lineups. Reuse the existing card IDs, slot model, validators, and mutation routes. You own packet 2's backend files and tests. You are not alone in this codebase; preserve others' edits and adapt to packet 1. Do not build UI, add drag and drop, refactor the solver, deploy, launch agents, or commit unrelated changes.
Record tested request/response examples and results in V1_AGENT_RESULTS.md so the UI agent can wire the actual API. Stop after packet 2.
```

## Third agent

```text
Work in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Read docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md and V1_AGENT_RESULTS.md. Confirm packets 1 and 2 passed.
Execute packet 3 only. Make templates/index.html a single-page workspace with all lineup player rows visible and the full roster alongside them. Wire the tested editing, score, locking, generation, and saving controls. Reuse existing components and vanilla JavaScript. A sidebar listing lineups with only one full lineup visible does not meet this scope.
You own templates/index.html. You are not alone in this codebase; do not revert backend or other agents' changes. If a required API field is missing, record and fix only that specific gap. No framework rewrite, payout or risk features, drag-and-drop system, deployment, or agent spawning.
Check the actual page in a browser on an isolated preview. Record results and any screenshot/preview reference in V1_AGENT_RESULTS.md. Stop after packet 3.
```

## Fourth agent

```text
Work in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Read docs/superpowers/plans/2026-09-11-v1-lineup-workspace.md and V1_AGENT_RESULTS.md.
Execute packet 4 only. Verify the one-page workflow on an isolated copy of the actual private roster, including an underperforming final Thursday player that remains fixed while other slots change. Test cross-lineup exchange, exclusions, scores, regeneration, save/load, and isolated restart. Run the full existing suite after the last fix.
You own acceptance checks and minimal corrections for reproduced failures. You are not alone in this codebase; preserve all other changes. Do not reopen the design, add strategy or payout features, launch agents, deploy, or overwrite the user's active plan. Record measured outcomes, remaining problems, and the preview location in V1_AGENT_RESULTS.md. Stop after packet 4.
```

## Narrow correction prompt

```text
Read the current V1 plan and V1_AGENT_RESULTS.md in /Users/shyampatel/Desktop/GB/lineup_optimizer.
Fix only the following observed failure, which I will paste below. Reproduce it first, trace the existing shared operation, make the smallest correction, and run the relevant check. Preserve other agents' work. No unrelated refactor, feature expansion, or deployment. Append the outcome to V1_AGENT_RESULTS.md and stop.

Observed failure:
```
