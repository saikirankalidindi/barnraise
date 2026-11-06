---
name: cancel
description: Cancel the Barnraise run. Running workers are told to stop; their worktrees and branches are kept so /barnraise:run can resume.
disable-model-invocation: true
---

Confirm with the user first, in one short question, unless they already said to cancel without asking. Then call the barnraise `control` tool with action `cancel` and report which tasks were stopped. Their work stays on their `barnraise/<task>` branches and worktrees; `/barnraise:run` restarts them from there.
