# Barnraise

Barnraise builds a software project in parallel inside Claude Code. You describe what you want in
one prompt. Barnraise plans it as tasks with explicit interfaces (contracts) between them and shows
you the plan. Once you approve it, Barnraise runs the tasks on several Claude workers at once, each
in its own git worktree, and checks and merges each finished task into one integration branch for
you to review.

```text
/barnraise:init
/barnraise:plan A FastAPI task manager with SQLite, a Typer CLI client, and pytest tests
/barnraise:run
```

Nothing runs until you run `/barnraise:run`, and Barnraise never changes your own branch or working
tree.

- **User guide:** https://github.com/saikirankalidindi/barnraise/blob/main/docs/USER_GUIDE.md
- **Source and issues:** https://github.com/saikirankalidindi/barnraise

## Install

In Claude Code:

```text
/plugin install barnraise --marketplace saikirankalidindi/barnraise
```

Or from a shell:

```sh
claude plugin install barnraise --marketplace saikirankalidindi/barnraise
```

Requires Claude Code, `git`, and `python3` 3.10 or newer on your `PATH` (macOS and Linux; on Windows, Python must be reachable as `python3`). Nothing else is installed.

## Commands

| Command | What it does |
| --- | --- |
| `/barnraise:init` | Set up the repo: integration branch, project checks, agent limit, budget |
| `/barnraise:plan <prompt>` | Make a plan; `/barnraise:plan --sync` re-validates your edits to `plan.yaml` |
| `/barnraise:run` | Approve the plan and run it to completion (also resumes after a crash) |
| `/barnraise:status` | Progress, contracts, spend, failures |
| `/barnraise:pause` / `/barnraise:cancel` | Stop starting new tasks / stop everything (work is kept) |
| `/barnraise:replan <change>` | Re-plan the tasks that haven't started |

## What this plugin runs and accesses

Barnraise runs entirely on your machine, inside your own Claude Code session. It sends no data to
any service of its own, collects no telemetry, reads no credentials, and does not change Claude Code's
permission settings.

**A local MCP server.** Claude Code starts it with
`python3 ${CLAUDE_PLUGIN_ROOT}/server/barnraise_mcp.py`. It uses only the Python standard library
plus a vendored copy of PyYAML's pure-Python source (`server/src/barnraise/_vendor/yaml`, MIT), so
nothing is downloaded or installed. The server holds the plan and run state and makes no network
requests.

**Hooks.** `python3 ${CLAUDE_PLUGIN_ROOT}/hooks/barnraise_hook.py` (Python standard library only)
runs on `PreToolUse` (file
edits, Bash, and Barnraise's `claim_task`), `PostToolUse`, and `SubagentStop` for
`barnraise:worker`. It exits immediately in any project without a `.barnraise/state.db`, and it only
acts on Barnraise's own worker subagents. It:

- refuses a worker's file edits outside its worktree or its task's files, its `cd` outside its
  worktree, and `git push`
- passes messages from other tasks to a running worker as added context
- keeps a worker from stopping before it has completed or reported its task
- reads the worker's own Claude Code transcript (under `~/.claude/projects/`) to total its token
  usage for the spend estimate (it reads token counts only)

**Shell commands.** Barnraise runs `git` to create worktrees and branches under `.barnraise/worktrees/`
and `barnraise/*`, and to merge finished tasks into `barnraise/integration`. It never pushes and never
touches your current branch. It also runs the setup and check commands written in the plan you
approve (for example `uv sync`, `pnpm install`, `uv run pytest`), which may install your project's
dependencies. Those commands inherit your shell environment as usual; Barnraise itself never reads
or copies your environment variables.

**Workers.** Tasks run as Claude Code subagents in your session, using your Claude account and plan
limits. Like Claude Code itself, a worker's Bash commands are not sandboxed.

**Files it writes in your repository.** `.barnraise/` (plan, state database, event log,
worktrees), two lines added to `.gitignore`, and the `barnraise/*` branches.

## License

MIT
