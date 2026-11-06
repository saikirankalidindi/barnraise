---
name: plan
description: Turn a project prompt into a validated, contract-first Barnraise plan of parallel tasks, or re-validate plan.yaml after edits with --sync. Nothing runs yet.
argument-hint: "<what to build>  |  --sync"
disable-model-invocation: true
---

Arguments from the user: $ARGUMENTS

## If the arguments are `--sync`

Call the barnraise `sync_plan` tool. If it returns errors, list each one as `plan.yaml line N: message` and stop. Otherwise say whether the plan changed, its version, and the new critical path. Stop.

## Otherwise: make a plan

1. Call `status`. If the project is not initialised, tell the user to run `/barnraise:init` first and stop. If a plan already exists, say so and confirm the user wants to replace it (if tasks have already started, suggest `/barnraise:replan` instead) before going on.
2. The arguments are the project prompt. If they are empty, ask the user what to build and wait.
3. Clarify only what would change the architecture: language or framework, storage, auth, deployment target, or scope. If the prompt leaves such choices open, ask at most 3 short questions in one message and wait. If the user said to use defaults, or the prompt is clear enough, skip this step.
4. Start the `barnraise:planner` subagent in the foreground (wait for it). Its prompt must contain: the project prompt, the user's answers, the repository root, the project checks from `status` (or "none yet"), and a one-paragraph summary of what already exists in the repo (look at the top-level files first). The planner calls `propose_plan` until the plan is valid.
5. Call `get_plan`. If it has no valid plan, report the planner's last errors and stop.
6. Present the plan:
   - a table: key, title, size, after, consumes (with the producing task), owns
   - the contracts and who produces them
   - the critical path, the estimated max speedup (`analysis.max_speedup`), and peak parallel tasks
   - the plan file path (`.barnraise/plan.yaml`)
7. Tell the user the next steps: review the plan; change it by editing `plan.yaml` and running `/barnraise:plan --sync`, or by asking you here; then `/barnraise:run` to start. Nothing has run yet.

If the user then asks for changes in chat, edit `.barnraise/plan.yaml` yourself with the Edit tool, call `sync_plan`, and show what changed. Never start the run from this skill.
