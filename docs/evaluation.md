# Evaluation (v0.1)

Nov 6, 2026. Claude Code 2.1.312, Sonnet for every session (coordinator, planner, workers). All runs
used real Claude Code against throwaway repos, and each result was verified independently in a fresh
checkout. Costs are notional (subscription login); worker spend is Barnraise's own estimate from
transcripts. Scripts are in `evals/`, and raw results in `evals/results/`. These runs predate the rename from Blueprint to Barnraise, so the logs show the old command and branch names (`/blueprint:*`, `blueprint/integration`).

## Planner (M1): 5 of 5

`evals/plan_eval.py` ran `/barnraise:init` and `/barnraise:plan` on the five reference prompts in
`evals/prompts/`. The acceptance bar was 4 of 5 valid plans with tasks that can run in parallel.

| Prompt | Valid | Tasks | Contracts | Parallel pairs | Peak parallel | Est. max speedup | Rejected proposals | Time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Task manager (FastAPI + Typer CLI + page) | yes | 5 | 1 | 3 | 3 | 1.33× | 0 | 38 s |
| Blog generator (Python) | yes | 4 | – | 2 | 2 | 1.08× | 0 | 60 s |
| Chat app (TypeScript, pnpm workspace) | yes | 4 | 1 | 1 | 2 | 1.5× | 0 | 47 s |
| Log query CLI (Rust) | yes | 5 | 3 | 3 | 3 | 1.4× | 0 | 34 s |
| URL shortener (Go) | yes | 4 | 2 | 1 | 2 | 1.4× | 0 | 48 s |

Every plan passed validation on the planner's first `propose_plan` call, at about $0.30 each. The
plans are in `evals/results/plans/`.

**Finding:** estimated speedups are modest. Planners put a completion edge on T0 for every task and
use contracts mostly between the middle tasks, so the critical path is long (T0 → API →
consumers → integration). That's a reasonable choice for correctness, and it bounds parallelism.

## End to end (M4): green, but slower than one agent on a small project

`evals/e2e.py` built the task-manager prompt twice: with Barnraise (agent limit 3), and with a
single Claude Code session asked to build it and make `uv run pytest` pass.

| | Wall time | Notional cost | Fresh checkout | Tests |
| --- | --- | --- | --- | --- |
| Barnraise | **139 s** (init 8, plan 29, run 102) | $1.12 coordinator + planner, ~$0.75 workers | `uv sync` and `uv run pytest` pass | 19 |
| Single agent | **54 s** | $0.22 | `uv sync` and `uv run pytest` pass | 12 |

Barnraise's run: all 5 tasks merged on their first attempt, and the final project checks passed.
The timeline shows the contract mechanism working: T1 published `tasks-api` at 32 s, and its two
consumers (T2 CLI, T3 page) were assigned at 33.6 s and ran alongside T1, which only merged at 57 s.

```text
  0.0s  run.started
  1.6s  T0 assigned         19.5s  T0 merged
 21.6s  T1 assigned         32.0s  tasks-api v1 published by T1
 33.6s  T2, T3 assigned     (while T1 is still running)
 57.0s  T1 merged           58.7s  T3 merged          70.5s  T2 merged
 73.1s  T4 assigned         91.7s  T4 merged          93.3s  run.done
```

**Finding: for a project this size, Barnraise is not worth it.** It took 2.6× the wall time and about
8× the cost of a single agent. The overheads it adds are planning (29 s), worker start-up (about 8 s
from spawn to claim), per-task checks and merges, a coordinator turn per event, and an unavoidably
serial T0 → … → T4 chain. A single strong agent writes a small app faster than Barnraise can split
it up. Barnraise's output was better tested (19 tests against 12, each task verified separately),
but that isn't what the speed comparison measures.

This answers the PRD's open question "is the speedup worth the overhead?" for small projects: no.
Where Barnraise should pay off is larger projects with wide plans, where per-task work dwarfs the
fixed overhead. That isn't measured yet (see Next steps).

## Crash recovery (M2)

`evals/recovery.py` starts a run, kills the whole Claude Code session once two workers are mid-task,
then resumes in a new session with `/barnraise:run --recover`.

The first version of this test cancelled the SDK client after a 10 s grace period. By then every task
had finished, so it proved nothing, and the test was changed to SIGKILL every process running in the
repo (the CLI, the MCP server, and the workers' shells), as a crash would.

- **At the kill:** T0 done, T1 and T2 `in_progress`, T3 `assigned`. Three seconds later the states
  were unchanged, so no worker survived to finish its task.
- **Resume:** `/barnraise:run --recover` in a new session recovered T1, T2, and T3. All four tasks
  were merged 37 s later, the final project checks passed, and a fresh checkout passes
  `uv run pytest` (13 tests).
- **Not covered live:** neither killed worker had committed yet, so this run shows tasks restarting
  cleanly rather than resuming partial commits. Resuming from an existing branch is covered by
  `test_recovery_after_a_crash_reuses_the_branch`.

## Unit and integration tests

`server/tests/`: 69 tests on real temporary git repos with a scripted stand-in for workers, covering
validation rules and their messages, plan.yaml line numbers, the scheduler, agent limits, parallel
merges, retries then failure, regression-based project checks, final checks, conflicts, owned
paths, contracts and early start, contract v2 notification, messages and caps, blockers,
pause/resume/cancel, budget, recovery, frozen tasks in replans, `wait_for_change` (including the
missed-event race), events.jsonl, and every hook (claim mapping, confinement, `cd`/push refusal,
delivery, stop gate, spend, the launcher as Claude Code runs it).

## Bugs the live runs found

1. **Absolute project checks deadlock early merges** (smoke test). After T0 merged, `pytest` exited 5
   because no tests existed yet, and T0 couldn't add tests it didn't own. Fixed: merges are rejected
   only for regressions, and the full checks must pass once everything is merged.
2. **A race between `next_actions` and `wait_for_change`** (smoke test). A task that finished between
   the two calls cost a full 240 s timeout. Fixed with a cursor.
3. **The coordinator re-asked a blocker** when the reply was "use your judgement". Fixed in the run
   skill: every reply is passed on verbatim.

## Next steps

- Measure a larger, wider project (for example 10–15 tasks across separate services), where
  parallelism has room to beat a single agent.
- Make plans wider: encourage contracts out of T0 (schemas, shared types) so consumers start
  before T0 merges.
- Cut the fixed overhead: skip planning questions for small prompts, and reuse worker setup
  (dependency installs) across worktrees.
