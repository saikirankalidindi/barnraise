# Barnraise user guide

Barnraise is a Claude Code plugin that builds a project in parallel. You describe what you want in
one prompt. Barnraise plans it as a set of tasks with explicit interfaces between them, shows you the
plan, and once you approve it, runs the tasks on several Claude workers at once. Each worker has its
own git worktree. Barnraise checks each task's work and merges it into one integration branch, which
you review and merge yourself.

```
/barnraise:init  →  /barnraise:plan "<what to build>"  →  review / edit plan.yaml  →  /barnraise:run
                                                                                         │
            workers build tasks in parallel, each in .barnraise/worktrees/<task>  ◄──────┘
            each finished task is verified and merged into barnraise/integration
                                                                                         │
            you review barnraise/integration and merge it  ◄─────────────────────────────┘
```

Nothing runs until you run `/barnraise:run`, and nothing ever touches your own branch or working
tree. Barnraise only writes to `barnraise/*` branches and to `.barnraise/` in your repo.

---

## 1. Requirements

- **Claude Code**, logged in however you normally use it. Barnraise uses your existing Claude Code
  session and never asks for an API key.
- **git**, and a repository with at least one commit.
- **python3** (3.10 or newer) on your `PATH`. Barnraise's server and hooks run on the Python standard
  library alone, so nothing is installed.
- Whatever your project itself needs (Node, Rust, Go, …), since workers run its tests.

## 2. Install

From GitHub:

```sh
claude plugin marketplace add saikirankalidindi/barnraise
claude plugin install barnraise@barnraise
```

Once it's listed in the Claude plugin directory, you can also install it from there.

To try a local copy for one session without installing:

```sh
claude --plugin-dir /path/to/barnraise/plugin
```

Check that it loaded: in Claude Code, type `/barnraise:` and you should see `init`, `plan`, `run`,
`status`, `pause`, `cancel`, and `replan`. 

Update with `claude plugin update barnraise@barnraise`, and remove with
`claude plugin uninstall barnraise@barnraise`.

## 3. Quick start

In the root of your repository (commit or stash your changes first):

```text
/barnraise:init
/barnraise:plan A FastAPI task manager with SQLite, a Typer CLI client, and pytest tests
```

Read the plan Barnraise shows you. Change anything you like (section 5), then:

```text
/barnraise:run
```

Leave the session open while it runs. You'll see one short progress line each time something
changes. When it finishes, review the result:

```sh
git log --first-parent barnraise/integration
git diff main..barnraise/integration
git merge barnraise/integration        # when you're happy
```

## 4. Setting up a repo: `/barnraise:init`

```text
/barnraise:init
/barnraise:init checks: pnpm test, pnpm lint; agents: 4; budget: 20
```

This does four things:

1. Creates the **integration branch** `barnraise/integration` from your current commit. Every
   finished task is merged there. Your current branch is not changed.
2. Records the **project checks**: the commands that mean "green" for the whole project. Barnraise
   suggests them from your repo (`package.json` scripts, `pyproject.toml`, `Cargo.toml`, `go.mod`,
   `Makefile`) and asks you to confirm. In a brand-new repo you can leave them empty, and the plan
   will define them.
3. Records the **agent limit** (how many workers run at once, 1–8, default 3), an optional
   **budget** in USD, and the **worker model** (`inherit` by default, or `sonnet`, `opus`, `haiku`).
4. Adds two lines to `.gitignore` so that everything under `.barnraise/` is ignored except
   `.barnraise/plan.yaml`, which you can commit.

Run it again at any time to change the agent limit, budget, or worker model.

## 5. Planning: `/barnraise:plan`

```text
/barnraise:plan <what you want built>
```

If your prompt leaves an architecture-level choice open (language, storage, auth, deployment
target), Barnraise asks up to three questions first. Then a planner reads your repo and writes a
plan, and Barnraise shows it to you as a table:

| Key | Title | After | Consumes | Owns |
| --- | --- | --- | --- | --- |
| T0 | Scaffold project and dependencies | – | – | `pyproject.toml`, `uv.lock`, `src/app/__init__.py` |
| T1 | Tasks API | T0 | – | `src/app/api/**`, `tests/test_api.py` |
| T2 | CLI client | T0 | `tasks-api` (from T1) | `src/app/cli.py`, `tests/test_cli.py` |
| T3 | Wire up and end-to-end tests | T1, T2 | – | `src/app/main.py`, `tests/test_e2e.py` |

It also shows the **critical path** (the longest chain of tasks that must happen one after another)
and the **estimated maximum speedup** over doing the tasks one by one. Expect something like 1.3–2×
for a typical app: parallelism is limited by how much of the work depends on other work.

### How to read a plan

- **T0** is always the foundation: scaffolding, tooling, and every dependency. Only T0 owns shared
  files such as `package.json`, `pyproject.toml`, and lockfiles.
- **owns**: the files a task may change, as repo-relative globs. Tasks that can run at the same time
  never own overlapping paths, so their merges don't collide.
- **after**: the task starts only when these tasks are **merged**.
- **consumes**: the task starts as soon as these **contracts** are **published**, which can be long
  before their producer finishes. This is where the parallelism comes from.
- **contracts**: interfaces one task publishes for others: an OpenAPI fragment, shared types, a
  database schema, a CLI spec. Each has exactly one producer, and producers publish them early.
- **acceptance**: commands that must pass in the task's worktree for the task to count as done.
- **checks** / **setup**: the project-wide checks, and the commands that prepare a fresh checkout
  (for example `uv sync` or `pnpm install`).
- **size**: S, M, or L. Used only for the critical-path estimate.

### Changing the plan

Either ask in the chat ("merge T2 and T3", "use Postgres instead") and Barnraise edits the plan for
you, or edit `.barnraise/plan.yaml` yourself and run:

```text
/barnraise:plan --sync
```

Barnraise re-validates the file and tells you exactly what's wrong, with line numbers:

```text
plan.yaml line 48: T3 depends on T7, which is not in the plan
plan.yaml line 61: T2 owns 'src/**' and T1 owns 'src/api/**'; they can run in parallel, so their owned paths must not overlap (or add an 'after' dependency between them)
```

A plan is rejected if it has a dependency cycle, a contract nobody produces (or two tasks
producing the same contract), a task with no acceptance checks, overlapping owned paths between
tasks that can run at the same time, a path outside the repo, or a shared file owned by a task
other than T0.

### plan.yaml reference

```yaml
prompt: A task manager with an API and a CLI
checks:            # project-wide "green", run around every merge
- uv run pytest
setup:             # prepares a fresh worktree before checks run
- uv sync
contracts:
- name: tasks-api            # lowercase slug
  kind: openapi              # openapi | types | schema | cli | other
  description: CRUD for /tasks with request and response schemas
tasks:
- key: T1                    # T0, T1, T2, …
  title: Tasks API
  description: |             # the worker's brief: be specific
    Implement the tasks-api contract in src/app/api/ using FastAPI …
  acceptance:                # run from the worktree root
  - uv run pytest tests/test_api.py
  owns:
  - src/app/api/**
  - tests/test_api.py
  after: [T0]                # completion dependencies (optional)
  produces: [tasks-api]      # contracts this task publishes (optional)
  consumes: []               # contracts this task needs (optional)
  size: M                    # S | M | L (default M)
```

Comments you add to `plan.yaml` are kept when you sync. If the planner later replaces the plan while
you have unsynced edits, your file is saved as `plan.yaml.bak` first.

## 6. Running: `/barnraise:run`

```text
/barnraise:run
```

Running this command is your approval of the current plan. Barnraise then:

1. Starts up to *agent limit* workers on the tasks that are ready, each in its own worktree at
   `.barnraise/worktrees/<task>` on branch `barnraise/<task>`, created from the integration branch.
2. Gives each worker a written brief: what to build, the files it owns, the contracts it produces
   and consumes (with their current content), and what "done" means.
3. Starts dependent tasks as soon as what they need exists: a published contract for `consumes`, a
   merge for `after`.
4. Verifies and merges each finished task (section 7), then starts whatever became ready.
5. Stops when every task is merged and every project check passes, or when it can't go on (a task
   failed, the budget ran out, or you paused or cancelled).

Your session acts as the coordinator. Leave it open and don't give it other work until the run
finishes. You'll see lines like:

```text
T0 done · T1, T2 running · 2 pending · $0.84 spent
```

### Permissions

Workers use Bash, Write, Edit, and Barnraise's own tools in the background, so set up permissions
before you start a run, so that workers aren't stopped waiting on approvals:

- start Claude Code with `claude --permission-mode acceptEdits` and allow the commands your project
  needs, or
- add allow rules to `.claude/settings.json` (or `.claude/settings.local.json`):

  ```json
  {
    "permissions": {
      "allow": [
        "mcp__plugin_barnraise_barnraise__*",
        "Edit",
        "Write",
        "Bash(uv:*)",
        "Bash(git:*)",
        "Bash(pnpm:*)"
      ]
    }
  }
  ```

Barnraise's hooks add their own limits on workers, whatever your permission mode (section 10).

### Questions from workers

A worker that can't continue without a decision from you (for example "Postgres or SQLite?") stops
and asks. The coordinator shows you the question. Your answer, even "use your judgement", is passed
to the task, which restarts from where it left off with the answer in its brief.

### Other commands while running

| Command | What it does |
| --- | --- |
| `/barnraise:status` | Tasks by status, what pending tasks wait for, contracts, messages, spend, failures. Read-only; safe in another session |
| `/barnraise:pause` | Running workers finish their current tasks; no new tasks start. `/barnraise:run` resumes |
| `/barnraise:cancel` | Running workers are told to stop. Their branches and worktrees are kept, and `/barnraise:run` restarts them from there |
| `/barnraise:replan <change>` | Re-plan the tasks that haven't started. Done and running tasks stay exactly as they are. Barnraise shows the change and asks before it takes effect |

## 7. How verification and merging work

When a worker says it's done, Barnraise checks, in order:

1. **No uncommitted changes** in the worktree.
2. **Every contract the task produces has been published.**
3. **The branch only changes files the task owns** (plus small edits to shared manifests and
   lockfiles, for example to add a dependency).
4. **The task's acceptance commands pass** in its worktree, after the setup commands.
5. **The merge into the integration branch is clean.** If it conflicts, the merge is abandoned and
   the worker is told to merge the integration branch into its own branch, resolve, and try again.
6. **No project check that passed before the merge fails after it.** If one does, the merge is
   undone and the worker gets the output.

Problems 1–3 are sent back to the worker to fix and don't count against it. Failing checks (4 and 6)
count as an attempt. After 3 attempts the task is marked **Failed**, its output is kept, and tasks
that depend on it don't start. If a branch keeps conflicting, it fails the same way.

Step 6 compares against the state before the merge, not against an absolute result. Early in a run
the full test suite often can't pass yet (for example, there are no tests until later tasks add
them), and that's fine. Nothing that passed may start failing, though. Once every task is merged,
**every project check must pass** on the integration branch. If one doesn't, the run stops with the
output instead of reporting success, and you can add a fix task with `/barnraise:replan`.

## 8. Reviewing and landing the result

Barnraise never merges into your branch. When the run is done:

```sh
git log --first-parent barnraise/integration   # one merge commit per task
git diff main..barnraise/integration           # everything that changed
git checkout barnraise/integration && <run your tests>   # or check it out in a worktree
git checkout main && git merge barnraise/integration
```

Each task's own commits stay on `barnraise/<task>` if you want to look at them individually.
Delete the branches afterwards with `git branch -D $(git branch --list 'barnraise/*' | tr -d ' ')`.

## 9. Cost and budget

Barnraise estimates each worker's spend from its transcript (token counts multiplied by list prices
per model family) and shows the total in `/barnraise:status`. Set a cap with
`/barnraise:init budget: 20`. When spend reaches it, no new tasks start, running ones finish, and
the run stops with "budget". On a Claude subscription the figure is notional: usage counts against
your plan's limits rather than being billed. The coordinator's and planner's own usage isn't
included.

To get a sense of scale, in Barnraise's own evaluation, planning a small app took 30–60 seconds, and
each worker on a small task used roughly 70k tokens.

## 10. Safety and limits

- **Workers are confined by hooks.** A worker's Write and Edit calls must stay inside its worktree
  and its owned paths, `cd` outside the worktree is refused, and `git push` is refused. Merges
  additionally reject branches that change files the task doesn't own.
- **Bash is not sandboxed.** A worker's shell commands can still reach anything your user account
  can (for example `echo x > ../file`). Run Barnraise only on code and in environments you would
  let Claude Code work on directly.
- **Shared files** (manifests, lockfiles) can be edited by any task when it needs a dependency.
  Collisions there show up as merge conflicts, which the worker resolves.
- **Speedup is bounded by the plan's shape.** Long chains of dependent tasks limit what parallel
  workers can do. Barnraise shows the critical path up front so this is visible.
- **One run per repository at a time.**
- **Claude only.** Barnraise drives Claude Code's own subagents.

## 11. If something goes wrong

**The session closed or crashed mid-run.** Open Claude Code in the repo again and run
`/barnraise:run`. Tasks whose worker has been silent for 5 minutes go back to pending, keeping their
branch and worktree, and are restarted from the existing work. If Barnraise says some workers "may
still be running" and you know the old session is gone, run `/barnraise:run --recover` to restart
them immediately. Otherwise wait 5 minutes and run `/barnraise:run` again. Don't use `--recover`
while another session is still running Barnraise in the same repo, or tasks will run twice.

**A task failed.** `/barnraise:status` shows its last failure output. Fix it by hand on its
`barnraise/<task>` branch, or describe the fix with `/barnraise:replan`, then `/barnraise:run`.

**"Every task is merged, but these project checks fail."** The parts work alone but not together.
Use `/barnraise:replan add a task that fixes: <paste the failing output>`, then `/barnraise:run`.

**"Cannot check out barnraise/integration".** You have the integration branch checked out in your
main working tree. Switch to another branch.

**A worker is stuck repeating the same error.** It gets 3 attempts. To intervene sooner,
`/barnraise:cancel`, fix the branch by hand, and `/barnraise:run`.

**The plan won't validate.** `/barnraise:plan --sync` names each problem and its line. The most
common fixes are giving two parallel tasks disjoint `owns`, or adding an `after` edge between them.

**Hooks seem not to work.** Check that `python3 --version` is 3.10 or newer, and look in
`.barnraise/hook-errors.log`.

## 12. Files Barnraise creates

| Path | What it is | Commit it? |
| --- | --- | --- |
| `.barnraise/plan.yaml` | The current plan, human-editable | Yes, if you like |
| `.barnraise/state.db` | Project config, plan versions, task state, contracts, messages, spend | No (gitignored) |
| `.barnraise/events.jsonl` | Append-only log of every state change | No |
| `.barnraise/worktrees/` | One worktree per running task, plus `_integration` | No |
| `barnraise/integration` | Branch with every merged task | Merge it when ready |
| `barnraise/<task>` | Each task's own branch | Delete after merging |

To remove Barnraise from a repo entirely:

```sh
git worktree list | grep .barnraise/worktrees | awk '{print $1}' | xargs -r -n1 git worktree remove --force
rm -rf .barnraise
git branch -D $(git branch --list 'barnraise/*' | tr -d ' ')
```
