"""The scheduler (PRD B-11): a pure function from state to next actions.

Same plan, task states, published contracts, limits, and run status always give the
same actions, in task-key order. It never touches git or the database; the engine
applies its decisions.
"""

import re
from dataclasses import dataclass
from typing import Any

from barnraise.plan import Plan

RUNNING = frozenset({"assigned", "in_progress"})
FINISHED = frozenset({"done", "failed"})


def key_order(key: str) -> tuple[int, str]:
    m = re.match(r"^T(\d+)(.*)$", key)
    return (int(m.group(1)), m.group(2)) if m else (1 << 30, key)


@dataclass(frozen=True)
class Inputs:
    plan: Plan
    status: dict[str, str]  # task key -> stored status
    published: frozenset[str]  # contracts with at least one version
    agent_limit: int
    run_status: str  # running | paused | cancelled
    spent_usd: float = 0.0
    budget_usd: float | None = None


def waiting_on(
    plan: Plan, status: dict[str, str], published: frozenset[str], key: str
) -> list[str]:
    """What a task still waits for: unmerged tasks and unpublished contracts."""
    task = next(t for t in plan.tasks if t.key == key)
    out = [f"{d} (merge)" for d in task.after if status.get(d) != "done"]
    out += [f"{c} (contract)" for c in task.consumes if c not in published]
    return out


def ready(i: Inputs) -> list[str]:
    keys = [
        t.key
        for t in i.plan.tasks
        if i.status.get(t.key, "pending") == "pending"
        and not waiting_on(i.plan, i.status, i.published, t.key)
    ]
    return sorted(keys, key=key_order)


def stuck(i: Inputs) -> list[str]:
    """Pending tasks that can never start because something they need failed."""
    failed = {k for k, s in i.status.items() if s == "failed"}
    producer = {c: t.key for t in i.plan.tasks for c in t.produces}
    blocked_by_failure: set[str] = set()
    changed = True
    while changed:
        changed = False
        for t in i.plan.tasks:
            if t.key in blocked_by_failure or i.status.get(t.key, "pending") != "pending":
                continue
            deps = set(t.after) | {
                producer[c] for c in t.consumes if c in producer and c not in i.published
            }
            if deps & (failed | blocked_by_failure):
                blocked_by_failure.add(t.key)
                changed = True
    return sorted(blocked_by_failure, key=key_order)


def decide(i: Inputs) -> list[dict[str, Any]]:
    keys = [t.key for t in i.plan.tasks]
    status = {k: i.status.get(k, "pending") for k in keys}
    running = sorted((k for k in keys if status[k] in RUNNING), key=key_order)
    blocked = sorted((k for k in keys if status[k] == "blocked"), key=key_order)

    if all(status[k] == "done" for k in keys):
        return [{"action": "done"}]

    actions: list[dict[str, Any]] = [{"action": "ask_user", "task": k} for k in blocked]
    over_budget = i.budget_usd is not None and i.spent_usd >= i.budget_usd

    if i.run_status == "running" and not over_budget:
        free = max(0, i.agent_limit - len(running))
        actions += [{"action": "spawn", "task": k} for k in ready(i)[:free]]

    spawning = any(a["action"] == "spawn" for a in actions)
    if running or spawning:
        actions.append({"action": "wait", "running": running})
        return actions
    if blocked:
        return actions  # only the user can unblock these

    if i.run_status != "running":
        return [{"action": "stopped", "reason": i.run_status}]
    if over_budget:
        return [{"action": "stopped", "reason": "budget"}]
    failed = sorted((k for k in keys if status[k] == "failed"), key=key_order)
    if failed:
        return [{"action": "stopped", "reason": "failed", "failed": failed, "stuck": stuck(i)}]
    # Nothing running, nothing ready, nothing failed: waiting on something that cannot
    # happen (a contract no running task will publish). Validation should prevent it.
    pending = sorted((k for k in keys if status[k] == "pending"), key=key_order)
    return [{"action": "stopped", "reason": "deadlock", "pending": pending}]
