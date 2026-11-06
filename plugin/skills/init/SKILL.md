---
name: init
description: Set up Barnraise in the current git repository (integration branch, project checks, agent limit, budget). Run before /barnraise:plan.
argument-hint: "[checks or settings, e.g. 'checks: pnpm test; agents: 4; budget: 20']"
disable-model-invocation: true
---

Set up Barnraise for this repository. Arguments from the user: $ARGUMENTS

1. Call the barnraise `status` tool. It reports whether the project is initialised, its current config, and `suggested_checks` detected from the repo.
2. Decide the settings:
   - **checks**: the commands that define "green" for the whole project (tests, lint, typecheck). Use what the user gave in the arguments; otherwise propose `suggested_checks`. For an empty or brand-new repo, checks may be empty: the plan will supply them.
   - **agent_limit**: workers running at once, 1 to 8 (default 3).
   - **budget_usd**: optional cap on estimated worker spend.
   - **worker_model**: `inherit` (default), `sonnet`, `opus`, or `haiku`.
   If the user did not give checks and some were detected, show them and ask the user to confirm or edit them in one short question, then wait. Skip the question if the arguments already settle it.
3. Call `init_project` with those settings.
4. Report in a few lines: the integration branch (and whether it was created), the checks, the agent limit, the budget, and that `.gitignore` now ignores `.barnraise/` except `plan.yaml`. Suggest the next step: `/barnraise:plan <what you want built>`.

If `init_project` fails (not a git repo, no commits yet), tell the user exactly what to do and stop.
