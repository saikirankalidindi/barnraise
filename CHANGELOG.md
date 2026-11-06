# Changelog

## 0.1.0 (2026-11-06)

First complete version (PRD v0.1, milestones M0–M4).

- `/barnraise:init`, `plan`, `run`, `status`, `pause`, `cancel`, `replan`.
- Contract-first planner with server-side validation and line-accurate errors for `plan.yaml` edits.
- Deterministic scheduler with agent limit, budget cap, contract-gated early starts, and blockers
  answered by the user.
- One worktree and branch per task. Verification (acceptance checks, owned paths, published
  contracts), merging under a lock with regression-based project checks, and final green
  checks before a run counts as done.
- Hooks for worker identity, write confinement, message delivery, a stop gate, and spend estimates.
- Crash recovery, `events.jsonl`, and the user guide.
