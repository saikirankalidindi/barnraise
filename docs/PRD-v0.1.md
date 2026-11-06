# Barnraise — PRD v0.1

Nov 5, 2026 · Saikiran Kalidindi

> Renamed from Blueprint to Barnraise on Nov 6, 2026, before publishing, to avoid clashing with existing names (see the naming brief). The design is unchanged.

Barnraise is a Claude Code plugin. It turns one project prompt into a contract-first plan of tasks, runs ready tasks in parallel on Claude Code subagents, each in its own git worktree, and merges each verified task into one integration branch.

It is the orchestration core of [Agent Canvas](../../AgentCanvas/docs/PRD-v0.1.md) (sections 3.5 and M4–M6 of that PRD), pulled out as a headless tool that runs inside the editor people already use. Agent Canvas is paused after its M3 and may later become a visual front end for Barnraise (B-25).

## Read first (for the implementer)

Build milestone by milestone (section 6) and stop after each for review. Treat every default below as decided unless the owner overrides it. M0 is a set of spikes. Several design choices rest on Claude Code behaviour that is only partly documented, and M0 confirms that behaviour before anything is built on it.

| Question | Why it matters | v0.1 default |
| --- | --- | --- |
| Plugin or standalone app? | Auth policy and install friction | **Claude Code plugin.** It runs inside the user's own Claude Code with whatever auth that uses, which Anthropic's policy allows. No separate app, no API key handling |
| Who decides what runs next? | An LLM driver drifts; a deterministic one is testable | **A deterministic MCP server** holds the plan, scheduler, contracts, and integrator. The main Claude session is a thin driver: it asks the server for the next action and carries it out |
| Where do workers run? | Parallel edits to one checkout corrupt each other | **One worktree per task**, on branch `barnraise/<task-key>`, created by Barnraise from the integration branch (not by `isolation: worktree`, which branches from the default branch; see M0 spike S-1) |
| How does the server know which worker is calling? | MCP servers do not learn the calling subagent | **Hooks.** Hook input carries `agent_id`. A PreToolUse hook on `claim_task` records `agent_id → task`, and later hooks use that mapping to enforce rules |
| How do agents "talk"? | Free-form chat is slow and drifts | **Contract-first** (same as Agent Canvas). Producers publish versioned contracts; dependents read them. Short typed messages (question, notice, blocker), capped per task |
| When does execution start? | A bad plan wastes every run | **Only after the user approves the plan** (`/barnraise:run`) |
| Where does state live? | Users expect project state with the project | **In the repo:** `.barnraise/plan.yaml` (committed) and `.barnraise/state.db` plus logs (gitignored) |
| Runtime for the MCP server | Plugin users must be able to run it | **Python 3.12 via `uv`**, matching Agent Canvas so planner, scheduler, and validator code can be reused. Revisit (Node) if `uv` turns out to be a real adoption barrier |
| Agent teams? | Claude Code has an experimental teams feature with a shared task list and mailboxes | **Not used in v0.1.** It is experimental, off by default, and interactive-only. Revisit once it is stable |

The two risks are the same as in Agent Canvas. Technically, parallel agents writing the same code is the hard part; worktrees, owned paths, and one integrator contain it. On the product side, plan quality decides everything downstream, so the plan is validated, shown, and approved before any worker starts.

## 1. Problem, goals, non-goals

A single coding agent builds an application serially. Tools exist that run several agents in parallel worktrees, but the user still splits the work by hand and resolves the collisions. Barnraise plans the split, starts dependents as soon as the interfaces they need are published, and only merges work that passes its checks.

Speedup is capped by the dependency graph. The realistic gain is a small multiple, so Barnraise reports the critical path and measures wall-clock time against a single agent (M4) rather than promising N×.

**Goals for v0.1**

1. Turn one prompt into a validated, editable plan (tasks, acceptance checks, owned paths, dependencies, contracts) with no code run yet.
2. Run ready tasks on up to N background subagents at once, each in an isolated worktree.
3. Let dependents start early on published contracts and receive typed messages, with no free-form chat.
4. Verify each task against its checks, retry on failure, and merge it into one integration branch.
5. Survive a closed or crashed Claude Code session: resume where it stopped, with no lost work.
6. Install with one command from a plugin marketplace.

**Non-goals for v0.1**

- A GUI (Agent Canvas may become one later).
- Providers other than Claude, or running outside Claude Code.
- Non-code projects; repositories without runnable checks.
- Deploying the result, cloud execution, multiple users.
- Automatic replanning mid-run (replan is user-triggered).

## 2. Glossary

Use these terms exactly, in code, commands, and output.

| Term | Meaning |
| --- | --- |
| Project | A git repo with a `.barnraise/` directory: root prompt, integration branch, agent limit, budget |
| Plan | Tasks, dependency edges, and contracts. Stored in `state.db`, exported to `.barnraise/plan.yaml` |
| Task | id key (T0, T1…), title, description, acceptance checks (shell commands), owned paths (globs), dependencies, contracts produced and consumed, status, attempts, summary |
| Dependency edge | `contract` (start once the producer publishes) or `completion` (start once the producer is merged) |
| Contract | A versioned interface a task publishes: OpenAPI fragment, shared types, DB schema, CLI spec |
| Message | A short typed note between tasks: question, notice, or blocker |
| Driver | The main Claude Code session running `/barnraise:run`. It does what the server tells it; it makes no scheduling decisions |
| Worker | A `barnraise:worker` subagent running one task in its worktree |
| Integrator | Server component that merges a verified task branch into the integration branch under a lock |
| Agent limit | Maximum workers running at once (1–8, default 3) |

## 3. Functional requirements

Ids are referenced by the acceptance criteria in section 6.

### 3.1 Commands (skills)

- **B-1** `/barnraise:init`: create `.barnraise/` and the integration branch (`barnraise/integration`, from the current `HEAD`). Ask for the checks that define "green" (default: detected test command). Gitignore everything under `.barnraise/` except `plan.yaml` (worktrees live in `.barnraise/worktrees/`).
- **B-2** `/barnraise:plan <prompt>`: run the planner subagent, validate, and print the plan with its critical path. Clarifying questions go to the user before a plan is proposed (at most 3).
- **B-3** Plan edits: the user edits `.barnraise/plan.yaml` directly or asks in chat; `/barnraise:plan --sync` re-reads the file and re-validates. Invalid edits are rejected with the exact error and line.
- **B-4** `/barnraise:run`: requires a valid, approved plan. Starts or resumes execution.
- **B-5** `/barnraise:status`: tasks by status, running workers, contracts, open messages, spend, critical path. Never mutates state.
- **B-6** `/barnraise:pause`, `/barnraise:cancel`: pause stops assigning new tasks; cancel also stops running workers and keeps their worktrees.
- **B-7** `/barnraise:replan`: replans the not-yet-started tasks, keeping done and in-progress ones fixed.

### 3.2 Planning and validation

- **B-8** The planner subagent may only call `propose_plan` (and read the repo). Its output is a typed plan, never free text.
- **B-9** Validation, server-side, blocks approval on: dependency cycles; a consumed contract no task produces; a task with no acceptance checks; overlapping owned paths between tasks that can run in parallel; owned paths outside the repo.
- **B-10** Shared files (package manifests, lockfiles) are owned by T0 in the plan, and the planner puts every dependency in T0. *Built:* other tasks may still make minimal edits to them (for example to add a dependency). Conflicts there come back to the worker as merge conflicts to resolve. Having the integrator apply `notice` requests was dropped: a worker that needs a dependency can't pass its own checks while it waits for someone else to add it.

### 3.3 Scheduling and workers

- **B-11** The scheduler is a pure function of (plan, task states, agent limit, contract versions). The same state always yields the same next actions, in task-key order.
- **B-12** `next_actions()` returns explicit instructions to the driver: `spawn` (with the full brief), `ask_user`, `wait`, `done`, or `stopped`. The driver starts the spawns as background subagents and, on `wait`, calls `wait_for_change(cursor)`. That call blocks server-side until a task finishes, blocks, or is released, a contract is published, or the run is paused, all measured from the cursor `next_actions` returned, so nothing is missed between the two calls. *Built:* this replaces relying on completion notifications, which never fire when a contract is published mid-task, so early starts would have waited for an unrelated worker to finish.
- **B-13** Each task gets a worktree at `.barnraise/worktrees/<key>` on branch `barnraise/<key>`, created from the integration branch at assignment time. Retries reuse the branch.
- **B-14** The worker brief contains: task description and acceptance checks, owned paths, worktree path, the contracts it consumes (latest versions), and a one-time task token used in `claim_task`.
- **B-15** Workers must call `claim_task(key, token)` first and `complete_task(key, token, summary)` last. Every worker tool takes the key and token from the brief, since the MCP server can't see which subagent calls it. Hooks identify the worker by `agent_id` (recorded on `claim_task`). A SubagentStop hook blocks a worker from stopping until `complete_task` succeeds or it reports a blocker. After 3 refusals it releases the task, which counts as an attempt.

### 3.4 Contracts and messages

- **B-16** `publish_contract(name, kind, content)` creates version N+1 and records the producer task. Dependents with a `contract` edge become ready once version 1 exists.
- **B-17** `read_contract(name, version?)` returns the latest (or a given) version.
- **B-18** `post_message(to?, kind, body)` delivers to a task or all its dependents. Each task may post at most 5 messages. A `blocker` pauses the sender's task until answered.
- **B-19** New contract versions and messages reach a running worker through a PostToolUse hook that adds them as context on its next tool call (spike S-4).

### 3.5 Verification and merge

- **B-20** `complete_task` runs the task's acceptance checks in its worktree. Failures go back to the worker with the output, up to 2 retries. After that the task is Failed, with the output kept.
- **B-21** The integrator merges a verified branch into the integration branch under one lock (in a dedicated worktree, `.barnraise/worktrees/_integration`), then runs the project-wide checks. Outcomes: merged, conflict (task back to the worker with the conflict), checks_failed (merge reverted, task back to the worker), all recorded with a log excerpt. *Built:* a merge is rejected only if a project check that passed before it fails after it (results are cached per commit). Absolute green after every merge is impossible early on: the M2 smoke test deadlocked because `pytest` exits 5 before any tests exist. When every task is merged, all project checks must pass, or the run stops with `final_checks` instead of `done`. Merging happens inside `complete_task`, so the worker that wrote the code fixes the failures while it still has the context. There is no separate driver `integrate` call.
- **B-22** All git operations on the integration branch go through the integrator. Worktrees are removed after merge or cancel, unless the user keeps them.

### 3.6 Safety, cost, recovery

- **B-23** A PreToolUse hook confines each worker's Write and Edit calls to its worktree and owned paths, and its Bash `cd` targets to its worktree. Main-session tools are not affected. Bash writes outside the worktree cannot be fully prevented (spike finding 2). The integrator rejects task branches that change files outside the task's owned paths.
- **B-24** Spend per task is read from the worker's transcript usage on SubagentStop. When spend reaches the budget, the scheduler stops assigning work and `/barnraise:status` says why. On a subscription login, spend is a notional figure (as in Agent Canvas ADR 0001).
- **B-25** Every state change is appended to `.barnraise/events.jsonl` with a monotonic seq, in a format Agent Canvas can render later.
- **B-26** Recovery: on `/barnraise:run` after a crash or a closed session, tasks that were In progress go back to Ready with their worktree and branch kept, and the next worker resumes from the existing branch.

## 4. Architecture

```
Claude Code session (driver)
 ├─ skills: /barnraise:init, plan, run, status, pause, cancel, replan
 ├─ subagents: barnraise:planner, barnraise:worker (background, many)
 ├─ hooks (plugin-level, fire in subagents with agent_id):
 │    PreToolUse  — claim mapping, path confinement
 │    PostToolUse — deliver contracts and messages to running workers
 │    SubagentStop — enforce complete_task, record spend
 └─ MCP server "barnraise" (stdio, Python via uv)
      planner validation · scheduler · contracts · messages · integrator
      state: <repo>/.barnraise/state.db, plan.yaml, events.jsonl, worktrees/
```

The MCP server is the only component that changes Barnraise's state or touches the integration branch. Hooks are small scripts that call the server's local control socket (or read `state.db` read-only) using `CLAUDE_PROJECT_DIR`. The driver skill is deliberately short. It loops on `next_actions()` and does exactly what that returns, so an LLM never makes a scheduling decision.

### 4.1 MCP tools

| Tool | Caller | Purpose |
| --- | --- | --- |
| `propose_plan(plan)` | planner | Validate and store a plan (B-8, B-9) |
| `get_plan()`, `status()` | driver, user | Read-only views (B-5) |
| `approve_plan()` | driver, after the user says so | Freeze the plan for execution |
| `next_actions()` | driver | Deterministic scheduling (B-11, B-12) |
| `claim_task(key, token)` | worker | Start a task; the hook records `agent_id → task` |
| `publish_contract`, `read_contract` | worker | B-16, B-17 |
| `post_message`, `read_messages` | worker | B-18 |
| `complete_task(key, summary)` | worker | Run checks, queue for merge (B-20) |
| `wait_for_change(cursor)` | driver | Block until something the scheduler cares about changes (B-12) |
| `answer_blocker`, `control`, `approve_plan`, `start_run` | driver | Blockers, pause/resume/cancel, approval, start and recovery |
| `report_blocker`, `read_messages` | worker | B-18 |

### 4.2 Data model

`project`, `task`, `task_edge`, `contract`, `message`, `attempt`, and `event`, following Agent Canvas PRD 4.2 (same fields where they overlap, so the two can share a schema). `plan.yaml` is the human-editable export of tasks, edges, and contracts.

### 4.3 Repo layout

```
.claude-plugin/plugin.json      manifest
.claude-plugin/marketplace.json single-plugin marketplace for installs
skills/<name>/SKILL.md          the commands in 3.1
agents/planner.md, worker.md    subagent definitions
hooks/hooks.json, hooks/*.py    the hooks in section 4
.mcp.json                       the barnraise MCP server
server/                         Python package (uv), tests
examples/                       the reference prompts and their scored plans
```

Plugin subagents cannot set `hooks`, `mcpServers`, or `permissionMode` in their frontmatter. Barnraise relies only on plugin-level hooks and the plugin's MCP server, which apply to all subagents.

## 5. Non-functional requirements

| Area | Requirement |
| --- | --- |
| Determinism | Scheduler and validator are pure and unit-tested. Same state, same next actions |
| Git safety | One lock for every integration-branch operation. Workers never touch the main checkout or another worktree |
| Durability | State is written before an action is returned. Killing the session at any point loses no committed work |
| Install | `/plugin marketplace add <repo>` then `/plugin install barnraise@<repo>` works with only `git` and `uv` installed |
| Footprint | No daemon. The MCP server starts with the session and exits with it |
| Observability | `events.jsonl` plus `status`. Every hook decision that blocks a tool is logged with the reason |
| Testing | Unit tests for validator, scheduler, integrator (on temp repos). Hook scripts are tested with recorded inputs. An e2e suite runs the reference prompts against real Claude Code, opt-in only, since it spends plan usage |
| Policy | Uses only the user's own Claude Code. Barnraise never handles credentials |

## 6. Milestones and acceptance criteria

1. **M0 Spikes and skeleton.** Done; results in [spikes.md](spikes.md). Plugin installs from a local marketplace; MCP server answers `status()`. Spikes, each with a recorded result in `docs/spikes.md`:
   - **S-1** Worktree base: can a worker run in a Barnraise-created worktree (B-13) reliably: file tools by absolute path, Bash with `cd`? Compare with `isolation: worktree` plus a `WorktreeCreate` hook or `worktree.baseRef`.
   - **S-2** Identity: PreToolUse on `claim_task` sees the worker's `agent_id` and input, and later hooks in that worker see the same `agent_id`.
   - **S-3** SubagentStop with `decision: block` keeps a worker going with our reason as its next instruction.
   - **S-4** PostToolUse `additionalContext` reaches a running background worker.
   - **S-5** Parallelism: N background workers run at once; measure the real limit and start-up cost.
   - [x] Every spike has a result. Any failed spike has an approved design change before M1 (none failed)
2. **M1 Planner and validation (no execution).** B-1, B-2, B-3, B-8, B-9, B-10, `plan.yaml` export.
   - [x] 4 of 5 reference prompts (task manager, blog, chat app, CLI tool, REST service) yield a valid plan with at least two tasks that can run in parallel (5 of 5, [evaluation](evaluation.md))
   - [x] Validation rejects a cycle, an unproduced contract, and overlapping owned paths, each with a precise error (`test_validate.py`)
   - [x] Edits to `plan.yaml` round-trip through `--sync` (`test_server.py`, with line numbers)
3. **M2 Run with completion edges.** B-4 to B-6, B-11 to B-15, B-20 to B-22, B-26.
   - [x] With agent limit 3, never more than 3 workers running (`test_agent_limit_is_never_exceeded`)
   - [x] Two parallel tasks with disjoint owned paths both merge cleanly (unit test and live runs)
   - [x] A task whose checks fail retries twice, then shows Failed with the output (`test_failing_checks_retry_twice_then_fail`)
   - [x] Closing the session mid-run and running `/barnraise:run` again resumes with no lost work (live SIGKILL test, [evaluation](evaluation.md))
4. **M3 Contracts, messages, confinement.** B-16 to B-19, B-23, `contract` edges, early start.
   - [x] In the task manager plan, dependents start before their producer is merged (live: T2, T3 started 23 s before T1 merged)
   - [x] Publishing contract v2 reaches an in-progress dependent (`test_contract_edges_start_dependents_early`, delivery hook test, spike S-4)
   - [x] A worker's write outside its owned paths is blocked with a clear reason (`test_file_writes_are_confined`, spike S-1)
5. **M4 Budget, replan, release.** B-7, B-24, B-25, marketplace publishing, README.
   - [x] Budget cap stops new assignments at 100% (`test_budget_stops_new_assignments`)
   - [x] End to end: the task manager prompt yields an integration branch with all checks green; wall-clock time recorded against a single-agent run (139 s against 54 s: slower on a small project, [evaluation](evaluation.md))
   - [x] A fresh machine with Claude Code, git, and uv installs and runs it from the README (checked with a throwaway `CLAUDE_CONFIG_DIR`; a separate physical machine was not tried)

## 7. Open questions

Each has a default so work is not blocked. The owner should confirm before M2.

- [x] **Planner quality** is the biggest unknown. Default: score the 5 reference prompts in M1 on validity, parallelism, and later on checks passing. Gate M2 on 4 of 5. *Result: 5 of 5 valid on the first attempt; plans are correct but narrow (est. speedup 1.1–1.5×).*
- [x] **Driver drift.** The main session may improvise instead of following `next_actions()`. Default: a strict, short skill plus server-side refusal of out-of-order calls. Measure in M2. *Result: in every live run the driver followed `next_actions` exactly. One drift (re-asking a blocker) was fixed in the skill.*
- [x] **Concurrency ceiling.** Background subagents share one session's rate limits. Default agent limit 3; measure in S-5 and M4. *Result: 6 of 6 concurrent in S-5; no rate limiting seen at 3.*
- [ ] **`uv` as a requirement.** Default: keep it. If it blocks adoption, port the server to Node.
- [ ] **Agent Canvas as front end.** Default: Barnraise only guarantees the `events.jsonl` and `plan.yaml` formats. The integration is designed after M4.
