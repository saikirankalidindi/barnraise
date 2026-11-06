---
name: replan
description: Re-plan the not-yet-started part of a Barnraise run (finished and running tasks stay as they are).
argument-hint: "<what should change>"
disable-model-invocation: true
---

What the user wants changed: $ARGUMENTS

1. Call `status` and `get_plan`. Note which tasks are done, running, blocked, or failed: those are frozen and must appear unchanged in the new plan. Only `pending` tasks may change, be removed, or be added to.
2. Start the `barnraise:planner` subagent in the foreground with: the current plan YAML, the status of every task, the user's requested change, and the rule above. It calls `propose_plan`, which rejects any change to a frozen task.
3. Show the user what changed (tasks added, removed, or edited, and the new critical path) and ask them to confirm.
4. If they confirm, call `approve_plan`, then tell them to run `/barnraise:run` (or, if a run is active in this session, continue its loop with `next_actions`). If they decline, say the previous approved plan is still in force.
