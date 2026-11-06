# Barnraise

A Claude Code plugin that builds a project in parallel. Describe what you want in one prompt.
Barnraise plans it as tasks with explicit interfaces (contracts) between them and shows you the
plan. Once you approve it, Barnraise runs the tasks on several Claude workers at once, each in its own
git worktree, and verifies and merges each finished task into one integration branch for you to
review.

```text
/barnraise:init
/barnraise:plan A FastAPI task manager with SQLite, a Typer CLI client, and pytest tests
/barnraise:run
```

**[User guide](docs/USER_GUIDE.md)**: install, every command, the plan format, how verification
works, recovery, and troubleshooting.

## What it does

- **Plans first, runs nothing until you approve.** A planner turns the prompt into tasks with owned
  paths, acceptance checks, dependencies, and contracts. The plan is validated (no cycles, no
  unproduced contracts, no overlapping paths between parallel tasks) and saved as an editable
  `.barnraise/plan.yaml`.
- **Runs tasks in parallel, safely.** Each worker has its own worktree and branch. Hooks keep its
  writes inside its worktree and its owned paths. Dependents start as soon as the contract they need
  is published, not when its producer finishes.
- **Merges only verified work.** Each task's acceptance checks must pass, and its merge must not
  break any project check that passed before it. Failures go back to the same worker, up to 3
  attempts. At the end, every project check must pass on the integration branch.
- **Never touches your branch.** Everything happens on `barnraise/*` branches and in `.barnraise/`.
- **Uses your own Claude Code**, with whatever login it already has. No API keys.
- **Survives crashes.** State lives in the repo. Run `/barnraise:run` again and it resumes.

## Requirements

Claude Code, `git`, [`uv`](https://docs.astral.sh/uv/), and `python3` 3.10 or newer.

## Install

```sh
claude plugin marketplace add saikirankalidindi/barnraise
claude plugin install barnraise@barnraise
```

Or try a local copy for one session: `claude --plugin-dir /path/to/barnraise/plugin`.

## Commands

| Command | |
| --- | --- |
| `/barnraise:init` | Set up the repo: integration branch, project checks, agent limit, budget |
| `/barnraise:plan <prompt>` | Make a plan; `/barnraise:plan --sync` re-validates your edits to `plan.yaml` |
| `/barnraise:run` | Approve the plan and run it to completion (also resumes) |
| `/barnraise:status` | Progress, contracts, spend, failures |
| `/barnraise:pause` / `/barnraise:cancel` | Stop starting new tasks / stop everything (work is kept) |
| `/barnraise:replan <change>` | Re-plan the tasks that haven't started |

## Results so far

From `evals/`, against real Claude Code (Sonnet):

- **Planning:** all 5 reference prompts (task manager, blog generator, chat app, Rust CLI, Go REST
  service) produced valid plans with parallel tasks on the planner's first attempt, in 34–60 s each.
  Estimated maximum speedups range from 1.1× to 1.5×: the plans' dependency chains, not the number
  of workers, set the limit.
- **End to end:** Barnraise built the task-manager prompt with all 5 tasks merged on the first try
  and 19 passing tests in a fresh checkout, and dependents started early on a published contract.
  On a project this small, though, a single agent was faster (54 s against 139 s) and much cheaper.
  Barnraise's fixed overhead (planning, worker start-up, per-task verification) only pays off on
  larger, wider projects. Details are in [docs/evaluation.md](docs/evaluation.md).

## How it works

A local MCP server (Python, run with `uv`) owns all decisions and state: plan validation, a
deterministic scheduler, contracts, messages, and the integrator. Your Claude Code session is a thin
driver. It asks the server what to do next and does exactly that: start background
`barnraise:worker` subagents with the briefs the server writes, wait, and report. Plugin hooks
identify each worker and enforce its limits. The design and its rationale are in
[docs/PRD-v0.1.md](docs/PRD-v0.1.md), and the platform experiments it rests on are in
[docs/spikes.md](docs/spikes.md).

```text
.claude-plugin/marketplace.json   the marketplace (one plugin, ./plugin)
plugin/                           the plugin itself: what users install
  .claude-plugin/                 manifest and listing icon
  skills/  agents/                the /barnraise:* commands, planner and worker subagents
  hooks/                          hook config and launcher (logic in server/src/barnraise/hooks.py)
  server/                         the MCP server and its tests
docs/                             PRD, user guide, evaluation, spikes
evals/                            planner and end-to-end evaluations (dev only; spend plan usage)
spikes/                           M0 platform experiments (dev only)
assets/                           script that draws the icon
```

## Develop

```sh
cd plugin/server
uv sync
uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy
claude plugin validate .. --profile directory
```

The unit and integration tests use real git repos in temp directories and need no Claude access.
The evals drive real Claude Code sessions:

```sh
cd evals
uv run --project ../plugin/server --with claude-agent-sdk python smoke.py          # tiny project, end to end
uv run --project ../plugin/server --with claude-agent-sdk python plan_eval.py      # 5 reference prompts
uv run --project ../plugin/server --with claude-agent-sdk python e2e.py sonnet 3   # Barnraise vs one agent
```

## Limits

Bash commands run by workers aren't sandboxed, and only file-tool writes are confined. Speedup is
bounded by the shape of the plan. One run per repository at a time. Claude only. See the user guide,
section 10.

## License

MIT
