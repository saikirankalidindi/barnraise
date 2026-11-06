# M0 spike results

Nov 5, 2026. Claude Code 2.1.312, Agent SDK 0.2.174, MCP Python SDK 2.3.0, Haiku for driver and workers.
Harness: `spikes/run_spikes.py` (scenarios A, B, C), run against throwaway repos in `/tmp` where
`barnraise/integration` is one commit ahead of `main`. Total notional cost across all runs: about $0.15.

| Spike | Result | Evidence |
| --- | --- | --- |
| S-1 Worktree base | **Pass** with Barnraise-made worktrees | Scenario A, C |
| S-2 Identity via hooks | **Pass** | Scenario A |
| S-3 Stop gate | **Pass** | Scenario A |
| S-4 Delivery to a running worker | **Pass** | Scenario A |
| S-5 Parallelism | **Pass**: 6 of 6 concurrent | Scenario B |
| Install from a marketplace | **Pass** (one fix needed) | Install check |

## S-1 Worktree base

Three `barnraise:worker` subagents were each given a worktree that Barnraise created with
`git worktree add -b barnraise/<key> .barnraise/worktrees/<key> barnraise/integration`.

- Every worktree had the integration-only file, so its base was the integration branch.
- Workers used absolute paths and `cd <worktree> &&` as the agent definition told them. T1 and T2
  committed on `barnraise/T1` and `barnraise/T2`. The main checkout stayed clean.
- T1's deliberate `Write` to `<repo>/OUTSIDE.txt` was denied by the PreToolUse hook, and the worker
  carried on.

Baseline (scenario C): an `isolation: "worktree"` subagent got `.claude/worktrees/agent-<id>` on branch
`worktree-agent-<id>`. Here it branched from the session's HEAD, but the docs say it branches from the
default branch. Either way Barnraise can't choose the base, so **the PRD default stands: Barnraise
creates the worktrees (B-13).**

## S-2 Identity

The PreToolUse hook on `mcp__plugin_barnraise_barnraise__claim_task` recorded `agent_id → task` for all
three workers. The same `agent_id` appeared on every later hook in that worker (Write, PostToolUse,
SubagentStop), and `agent_type` was `barnraise:worker`. Hooks fire inside plugin subagents even
though plugin agents can't declare hooks themselves.

## S-3 Stop gate

T2 was told not to call `complete_task`. It stopped at 8.0 s. SubagentStop returned
`{"decision": "block", "reason": "...call complete_task..."}`, the worker obeyed the hook over its brief,
called `complete_task`, and stopped for good at 13.9 s. One block was enough. The hook caps blocks at 3
to avoid loops.

## S-4 Delivery

A message written to T3's inbox after it claimed the task came back through PostToolUse
`additionalContext` on its next tool call. T3 quoted it verbatim in its `complete_task` summary
("contract api v2 published, codeword PINEAPPLE"). B-19 works as designed.

## S-5 Parallelism

Six workers each ran `sleep 20`. They started 0.1–1.7 s apart, their sleeps overlapped completely
(windows 3.5–23.7 s to 6.3–26.4 s), so 6 of 6 ran concurrently. Time from spawn to first command was
3.5–6.3 s. The real limit is above 6; it wasn't probed further. Rate limits are the likelier ceiling
on a subscription, so the agent limit default stays 3, with an upper bound of 8.

## Install

`claude plugin marketplace add <dir>` then `claude plugin install barnraise@barnraise` worked against a
throwaway `CLAUDE_CONFIG_DIR`. The installed copy's MCP server, started exactly as `.mcp.json` says
(`uv run --frozen --project ${CLAUDE_PLUGIN_ROOT}/server`, venv in `${CLAUDE_PLUGIN_DATA}`), answered
`status()` with about a 1.9 s cold start.

**Fix needed:** a local-path install copied the whole folder, including `server/.venv` and tool caches
(144 MB). Releases install from git, which carries only tracked files, and `.gitignore` now excludes
those directories.

## Findings that change the design

1. **The driver is woken by completion notifications.** After spawning background workers, the driver's
   turn ends within seconds. Claude Code starts a new driver turn as workers finish, sometimes several
   at once. So `/barnraise:run` is event-driven: on every wake, call `next_actions()` and do what it
   says. No polling, no sleeping. This makes PRD B-12 simpler.
2. **Bash is not confined.** The hook confines `Write`/`Edit`, but a worker's Bash can still write outside
   its worktree (`echo x > ../file`). B-23's "Bash `cd` targets" check catches mistakes, not misuse.
   The integrator's checks catch the damage that matters (changes outside owned paths on the task
   branch). Out-of-tree writes are documented as a known limit.
3. **Workers follow hook instructions over their brief** (S-3). That's good for enforcement, and it means
   hook reasons must be written as precise instructions.
4. **The session also loads the user's other plugins and MCP connectors.** Workers inherit them. That's
   acceptable because it's the user's own session, but worker briefs should say to use only the tools
   the task needs.
5. **Workers add Claude Code's default commit trailer** (`Co-Authored-By`). The integrator should keep
   task commits as they are and write its own merge commits.
6. **`.barnraise/worktrees/` lives inside the repo,** so `/barnraise:init` must gitignore everything in
   `.barnraise/` except `plan.yaml` (B-1, wording tightened).

No spike failed, so no design change blocks M1.
