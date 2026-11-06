---
name: status
description: "Show Barnraise's status for this project: plan, tasks by status, running workers, contracts, messages, spend, and failures. Read-only."
---

Call the barnraise `status` tool and summarise it without changing anything:

- Not initialised: say so and suggest `/barnraise:init`.
- A plan but no run: the plan version, task count, validity, critical path, and whether `plan.yaml` has unsynced edits.
- A run: `run_status`, then a table of tasks (key, title, status, attempts, what pending tasks wait for), published contracts with versions, unread messages, and spend against the budget. For failed tasks show `last_failure`; for blocked tasks show the question.
