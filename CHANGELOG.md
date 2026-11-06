# Changelog

## 0.1.1 (2026-11-06)

Fixes for the Claude plugin directory's validation.

- The MCP server and hooks now run on the Python standard library alone, started as
  `python3 ${CLAUDE_PLUGIN_ROOT}/...`. No `uv`, no package installs, and no environment
  variables. The MCP stdio protocol and the plan schema are implemented in-house, and PyYAML
  is vendored as readable pure-Python source.
- Hooks declare the interpreter in `command` and the script in `args`.
- Fixed the `/barnraise:status` skill's frontmatter, which wasn't valid YAML.
- Requirements are now Claude Code, git, and python3 3.10 or newer.

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
