"""Execution (PRD 3.3-3.6): assigning tasks, the worker tools, verification, and merging.

The driver (main Claude Code session) calls start_run, then loops on next_actions and
wait_for_change. Workers call claim_task, publish_contract, post_message,
report_blocker, and complete_task, authenticating with the task token from their
brief. All state lives in the Store; the scheduler decides, the engine applies.
"""

import asyncio
import fcntl
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from barnraise import checks, gitops
from barnraise.globs import overlap
from barnraise.plan import Issue, Plan, Task
from barnraise.scheduler import RUNNING, Inputs, decide, key_order, waiting_on
from barnraise.store import Store
from barnraise.validate import SHARED_FILES, analyse, edges, validate

MAX_ATTEMPTS = 3  # the first try plus two retries (PRD B-20)
MAX_CONFLICTS = 3
MESSAGE_CAP = 5  # messages a task may post (PRD B-18)
STALE_S = 300  # a claimed task with no tool call for this long is presumed dead
UNCLAIMED_S = 600  # an assigned task whose worker never claimed it
SIGNIFICANT = (
    "task.done",
    "task.failed",
    "task.blocked",
    "task.released",
    "task.cancelled",
    "contract.published",
    "run.paused",
    "run.cancelled",
    "run.resumed",
)
DRIVER_INSTRUCTION = (
    "Carry out these actions exactly. Start every 'spawn' as a background "
    "barnraise:worker subagent with the given description and prompt, all in one message. "
    "Then, if there is a 'wait' action, call wait_for_change and afterwards next_actions "
    "again. Never do task work yourself."
)

_thread_lock = threading.Lock()


class EngineError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "errors": [{"code": self.code, "message": self.message}]}


def matches_owned(path: str, owns: list[str]) -> bool:
    return any(overlap(g, path) for g in owns) or any(overlap(f, path) for f in SHARED_FILES)


class Engine:
    def __init__(self, store: Store) -> None:
        self.s = store

    # --- Helpers --------------------------------------------------------------

    @property
    def root(self) -> Path:
        return self.s.root

    def integration_branch(self) -> str:
        return str(self.s.config().get("integration_branch") or "barnraise/integration")

    def integration_worktree(self) -> Path:
        return self.s.worktrees / gitops.INTEGRATION_WORKTREE

    def plan(self) -> tuple[int, Plan]:
        approved = self.s.approved_plan()
        if approved is None:
            raise EngineError("not_running", "no approved plan; run /barnraise:run")
        return approved

    def task(self, plan: Plan, key: str) -> Task:
        for t in plan.tasks:
            if t.key == key:
                return t
        raise EngineError("unknown_task", f"{key} is not in the plan")

    def _auth(self, conn: sqlite3.Connection, key: str, token: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM task_state WHERE key = ?", (key,)).fetchone()
        if row is None or not token or row["token"] != token:
            raise EngineError("bad_token", f"unknown task {key} or wrong token for it")
        return dict(row)

    @contextmanager
    def _integration_lock(self) -> Iterator[None]:
        """One merge at a time, across threads and processes (PRD 5, git safety)."""
        self.s.dir.mkdir(parents=True, exist_ok=True)
        with _thread_lock, (self.s.dir / "integrate.lock").open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def ensure_integration_worktree(self) -> Path:
        wt = self.integration_worktree()
        branch = self.integration_branch()
        if not (wt / ".git").exists():
            try:
                gitops.ensure_worktree(self.root, wt, branch, branch)
            except gitops.GitError as exc:
                raise EngineError(
                    "integration_checkout",
                    f"cannot check out {branch} in {wt}: {exc}. If it is checked out in your "
                    "main working tree, switch that to another branch.",
                ) from None
        return wt

    # --- Plan approval and replanning (PRD B-4, B-7) --------------------------

    def frozen_issues(self, plan: Plan) -> list[Issue]:
        """Tasks that have started cannot change or disappear in a new plan version."""
        approved = self.s.approved_plan()
        if approved is None:
            return []
        old = {t.key: t for t in approved[1].tasks}
        new = {t.key: (i, t) for i, t in enumerate(plan.tasks)}
        issues = []
        for key, row in sorted(self.s.tasks().items(), key=lambda kv: key_order(kv[0])):
            if row["status"] == "pending":
                continue
            if key not in new:
                issues.append(
                    Issue(
                        "frozen_task", f"{key} is {row['status']} and cannot be removed", ("tasks",)
                    )
                )
            elif key in old and new[key][1] != old[key]:
                issues.append(
                    Issue(
                        "frozen_task",
                        f"{key} is {row['status']}; only pending tasks can change",
                        ("tasks", new[key][0]),
                    )
                )
        return issues

    def approve(self, version: int | None = None) -> dict[str, Any]:
        latest = self.s.latest_plan()
        if latest is None:
            raise EngineError("no_plan", "no plan yet; run /barnraise:plan")
        version = version or latest[0]
        plan = self.s.plan(version)
        if plan is None:
            raise EngineError("no_plan", f"plan version {version} does not exist")
        issues = validate(plan) + self.frozen_issues(plan)
        if issues:
            raise EngineError("invalid_plan", "; ".join(i.message for i in issues))
        keys = {t.key for t in plan.tasks}
        with self.s.tx() as c:
            for k in keys:
                c.execute("INSERT OR IGNORE INTO task_state (key) VALUES (?)", (k,))
            for row in c.execute("SELECT key, status FROM task_state").fetchall():
                if row["key"] not in keys and row["status"] == "pending":
                    c.execute("DELETE FROM task_state WHERE key = ?", (row["key"],))
            self.s.set_config(c, approved_version=version)
            self.s.log("plan.approved", conn=c, version=version)
        return {"ok": True, "approved_version": version}

    # --- Driver ---------------------------------------------------------------

    def start_run(self, force_recover: bool = False) -> dict[str, Any]:
        """Approve the latest plan if needed, recover dead tasks, and start (B-4, B-26)."""
        if not self.s.initialized:
            raise EngineError("not_initialized", "run /barnraise:init first")
        latest = self.s.latest_plan()
        if latest is None:
            raise EngineError("no_plan", "no plan yet; run /barnraise:plan")
        approved = self.s.config().get("approved_version")
        if approved is None or latest[0] > int(approved):
            self.approve(latest[0])
        version, plan = self.plan()
        self.ensure_integration_worktree()
        now = time.time()
        recovered, live = [], []
        with self.s.tx() as c:
            for key, row in self.s.tasks(c).items():
                if row["status"] in RUNNING:
                    last = row["heartbeat"] or row["claimed_at"] or row["assigned_at"] or 0
                    if force_recover or now - last > STALE_S:
                        self._release(c, key, reason="recovered after restart")
                        recovered.append(key)
                    else:
                        live.append(key)
                elif row["status"] == "cancelled":
                    self.s.update_task(c, key, status="pending")
                    recovered.append(key)
            self.s.set_config(c, run_status="running")
            self.s.log("run.started", conn=c, version=version, recovered=recovered)
        config = self.s.config()
        return {
            "ok": True,
            "plan_version": version,
            "tasks": len(plan.tasks),
            "agent_limit": config.get("agent_limit"),
            "budget_usd": config.get("budget_usd"),
            "integration_branch": self.integration_branch(),
            "recovered": sorted(recovered, key=key_order),
            "possibly_still_running": sorted(live, key=key_order),
            "analysis": analyse(plan),
        }

    def _release(self, c: sqlite3.Connection, key: str, reason: str) -> None:
        self.s.update_task(
            c, key, status="pending", token=None, agent_id=None, stop_blocks=0, heartbeat=None
        )
        self.s.log("task.released", conn=c, task=key, reason=reason)

    def inputs(self, plan: Plan) -> Inputs:
        config = self.s.config()
        tasks = self.s.tasks()
        return Inputs(
            plan=plan,
            status={k: v["status"] for k, v in tasks.items()},
            published=frozenset(self.s.contracts()),
            agent_limit=int(config.get("agent_limit") or 3),
            run_status=str(config.get("run_status") or "stopped"),
            spent_usd=self.s.spent_usd(),
            budget_usd=config.get("budget_usd"),
        )

    def next_actions(self) -> dict[str, Any]:
        version, plan = self.plan()
        self._housekeeping()
        # Taken before deciding, so wait_for_change(cursor) cannot miss what happens next.
        cursor = self._significant_seq()
        config = self.s.config()
        worker_model = config.get("worker_model")
        actions = []
        for a in decide(self.inputs(plan)):
            kind = a["action"]
            if kind == "spawn":
                task = self.task(plan, a["task"])
                spawn: dict[str, Any] = {
                    "action": "spawn",
                    "task": task.key,
                    "subagent_type": "barnraise:worker",
                    "description": f"Barnraise {task.key}: {task.title}"[:80],
                    "run_in_background": True,
                    "prompt": self._assign(plan, task),
                }
                if worker_model:
                    spawn["model"] = worker_model
                actions.append(spawn)
            elif kind == "ask_user":
                row = self.s.tasks()[a["task"]]
                actions.append(
                    {
                        "action": "ask_user",
                        "task": a["task"],
                        "question": row["blocker"],
                        "then": f"call answer_blocker(key='{a['task']}', answer=<the user's answer>)",
                    }
                )
            elif kind == "done":
                actions.append(self._finish(plan))
                if actions[-1]["action"] == "stopped":
                    break
            elif kind == "stopped":
                actions.append({**a, "message": self._stopped_message(a)})
            elif kind == "wait":
                actions.append(
                    {**a, "cursor": cursor, "then": f"call wait_for_change(cursor={cursor})"}
                )
            else:
                actions.append(a)
        return {
            "ok": True,
            "plan_version": version,
            "actions": actions,
            "progress": self.progress(plan),
            "instruction": DRIVER_INSTRUCTION,
        }

    def _housekeeping(self) -> None:
        """Release tasks whose worker never claimed them, and remove merged worktrees."""
        now = time.time()
        with self.s.tx() as c:
            for key, row in self.s.tasks(c).items():
                if row["status"] == "assigned" and now - (row["assigned_at"] or now) > UNCLAIMED_S:
                    self._release(c, key, reason="worker never claimed the task")
        for _key, row in self.s.tasks().items():
            if row["status"] == "done" and row["worktree"] and Path(row["worktree"]).exists():
                gitops.remove_worktree(self.root, Path(row["worktree"]))

    def progress(self, plan: Plan) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for row in self.s.tasks().values():
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        out: dict[str, Any] = {"tasks": counts, "spent_usd": round(self.s.spent_usd(), 4)}
        budget = self.s.config().get("budget_usd")
        if budget:
            out["budget_usd"] = budget
        return out

    def _stopped_message(self, a: dict[str, Any]) -> str:
        reason = a.get("reason")
        if reason == "paused":
            return "The run is paused. Resume with /barnraise:run."
        if reason == "cancelled":
            return "The run was cancelled. Worktrees are kept; resume with /barnraise:run."
        if reason == "budget":
            return (
                "The budget is used up, so no new tasks are started. Raise it with /barnraise:init."
            )
        if reason == "failed":
            return (
                f"Task(s) {', '.join(a['failed'])} failed after {MAX_ATTEMPTS} attempts"
                + (f"; {', '.join(a['stuck'])} cannot start without them" if a.get("stuck") else "")
                + ". See /barnraise:status for the output, then fix the plan with "
                "/barnraise:replan or the code by hand, and run /barnraise:run again."
            )
        return f"Nothing can start ({a.get('pending')}). Check the plan with /barnraise:status."

    def _finish(self, plan: Plan) -> dict[str, Any]:
        """Every task is merged: the project checks must now all pass at the tip."""
        branch = self.integration_branch()
        with self._integration_lock():
            int_wt = self.ensure_integration_worktree()
            results, report = self._project_checks(int_wt, plan)
        failing = sorted(cmd for cmd, ok in results.items() if not ok)
        if failing:
            return {
                "action": "stopped",
                "reason": "final_checks",
                "failing": failing,
                "output": report[-6000:],
                "message": f"Every task is merged, but these project checks fail on {branch}: "
                f"{', '.join(failing)}. Add a task that fixes them with /barnraise:replan "
                "(pass it the output), then /barnraise:run.",
            }
        base = self.s.config().get("base_commit", "")
        log = gitops.out(self.root, "log", "--oneline", "--first-parent", f"{base}..{branch}")
        if self.s.config().get("run_status") != "done":
            with self.s.tx() as c:
                self.s.set_config(c, run_status="done")
                self.s.log("run.done", conn=c)
        gitops.remove_worktree(self.root, self.integration_worktree())
        return {
            "action": "done",
            "integration_branch": branch,
            "checks": sorted(results),
            "merges": log.splitlines(),
            "message": f"All {len(plan.tasks)} tasks are merged into {branch} and every project "
            f"check passes. Review with `git log --first-parent {branch}` and "
            f"`git diff {base[:12]}..{branch}`, then merge it into your branch when you are happy.",
        }

    async def wait_for_change(self, timeout_s: float, cursor: int | None = None) -> dict[str, Any]:
        """Return once a task finishes, blocks, or is released, a contract is published,
        or the run is paused or cancelled, since `cursor` (from next_actions); or after
        `timeout_s`."""
        start = self._significant_seq() if cursor is None else cursor
        deadline = time.monotonic() + max(1.0, min(timeout_s, 600.0))
        while True:
            seq = self._significant_seq()
            if seq > start:
                events = [
                    e for e in self.s.events() if e["seq"] > start and e["type"] in SIGNIFICANT
                ]
                return {"changed": True, "events": events[-20:], "then": "call next_actions"}
            if time.monotonic() >= deadline:
                return {"changed": False, "then": "call next_actions"}
            await asyncio.sleep(1.0)

    def _significant_seq(self) -> int:
        marks = ",".join("?" * len(SIGNIFICANT))
        with self.s.read() as c:
            row = c.execute(
                f"SELECT COALESCE(MAX(seq), 0) FROM event WHERE type IN ({marks})", SIGNIFICANT
            ).fetchone()
        return int(row[0])

    def control(self, action: str) -> dict[str, Any]:
        """pause | resume | cancel (PRD B-6)."""
        if action not in ("pause", "resume", "cancel"):
            raise EngineError("bad_action", "action must be pause, resume, or cancel")
        self.plan()  # requires an approved plan
        stopped = []
        with self.s.tx() as c:
            if action == "pause":
                self.s.set_config(c, run_status="paused")
                self.s.log("run.paused", conn=c)
            elif action == "resume":
                self.s.set_config(c, run_status="running")
                self.s.log("run.resumed", conn=c)
            else:
                for key, row in self.s.tasks(c).items():
                    if row["status"] in RUNNING:
                        self.s.update_task(c, key, status="cancelled", token=None)
                        self._send(
                            c,
                            "barnraise",
                            "control",
                            "The run was cancelled. Stop now: make no further changes and do "
                            "not call complete_task.",
                            [key],
                        )
                        self.s.log("task.cancelled", conn=c, task=key)
                        stopped.append(key)
                self.s.set_config(c, run_status="cancelled")
                self.s.log("run.cancelled", conn=c, stopped=stopped)
        return {"ok": True, "run_status": self.s.config()["run_status"], "stopped": stopped}

    def answer_blocker(self, key: str, answer: str) -> dict[str, Any]:
        with self.s.tx() as c:
            row = c.execute("SELECT * FROM task_state WHERE key = ?", (key,)).fetchone()
            if row is None or row["status"] != "blocked":
                raise EngineError("not_blocked", f"{key} is not waiting for an answer")
            self._send(
                c,
                "user",
                "answer",
                f"You asked: {row['blocker']}\nThe user answered: {answer}",
                [key],
            )
            self.s.update_task(c, key, status="pending", blocker=None, token=None, agent_id=None)
            self.s.log("task.released", conn=c, task=key, reason="blocker answered")
        return {"ok": True, "message": f"{key} will restart with the answer."}

    # --- Assigning (PRD B-13, B-14) -------------------------------------------

    def _assign(self, plan: Plan, task: Task) -> str:
        wt = self.s.worktrees / task.key
        branch = f"barnraise/{task.key}"
        gitops.ensure_worktree(self.root, wt, branch, self.integration_branch())
        token = secrets.token_hex(8)
        with self.s.tx() as c:
            self.s.update_task(
                c,
                task.key,
                status="assigned",
                token=token,
                branch=branch,
                worktree=str(wt),
                assigned_at=time.time(),
                claimed_at=None,
                heartbeat=None,
                agent_id=None,
                stop_blocks=0,
            )
            self.s.log("task.assigned", conn=c, task=task.key, branch=branch)
        return self.brief(plan, task, token, wt, branch)

    def brief(self, plan: Plan, task: Task, token: str, wt: Path, branch: str) -> str:
        rows = self.s.tasks()
        row = rows.get(task.key, {})
        contracts = self.s.contracts()
        producer = {c: t.key for t in plan.tasks for c in t.produces}
        declared = {c.name: c for c in plan.contracts}
        lines = [
            f"You are the Barnraise worker for task {task.key}: {task.title}.",
            "",
            f"Task key: {task.key}",
            f"Token: {token}",
            f"Worktree: {wt}",
            f"Branch: {branch} (started from {self.integration_branch()})",
            "",
            "## What to build",
            task.description,
            "",
            "## Done means",
            "These commands must pass when run from the worktree root:",
            *[f"- `{a}`" for a in task.acceptance],
            "After your branch is merged, these project checks must pass too:",
            *[f"- `{c}`" for c in plan.checks],
        ]
        if plan.setup:
            lines += ["Prepare a fresh checkout with:", *[f"- `{c}`" for c in plan.setup]]
        lines += [
            "",
            "## Files you own",
            *[f"- `{g}`" for g in task.owns],
            "You may also make minimal edits to shared files (package manifests and lockfiles) "
            "when you need a dependency. Do not change any other file: other workers own them, "
            "and Barnraise refuses to merge a branch that does.",
        ]
        if task.produces:
            lines += ["", "## Contracts you publish"]
            for name in task.produces:
                c = declared.get(name)
                lines.append(
                    f"- `{name}` ({c.kind if c else 'other'}): {c.description if c else ''}"
                )
            lines.append(
                "Publish each one with publish_contract as soon as the interface is settled, "
                "before you implement it: tasks that depend on it start when you do. Publish "
                "again if it changes."
            )
        if task.consumes:
            lines += ["", "## Contracts you use"]
            for name in task.consumes:
                pub = contracts.get(name)
                if pub:
                    lines += [
                        f"### `{name}` v{pub['version']} (from {pub['producer']})",
                        "```",
                        pub["content"],
                        "```",
                    ]
                else:
                    lines.append(f"- `{name}` from {producer.get(name)}: not published yet")
            lines.append("If a newer version is published while you work, Barnraise tells you.")
        merged = [t for t in plan.tasks if rows.get(t.key, {}).get("status") == "done"]
        if merged:
            lines += ["", "## Already merged into the integration branch"]
            lines += [f"- {t.key} {t.title}: {rows[t.key].get('summary') or ''}" for t in merged]
        inbox = self._inbox(task.key, mark=False)
        if inbox:
            lines += ["", "## Messages for you"]
            lines += [f"- from {m['from']} ({m['kind']}): {m['body']}" for m in inbox]
        if row.get("last_failure"):
            lines += [
                "",
                "## Previous attempt",
                f"This task was tried before (attempt {row.get('attempts', 0)} of {MAX_ATTEMPTS}). "
                "Your branch already has that work. The last failure was:",
                "```",
                str(row["last_failure"])[-4000:],
                "```",
            ]
        lines += [
            "",
            "## Rules",
            f"1. Call claim_task with key {task.key} and your token before anything else.",
            f"2. Work only inside {wt}. Use absolute paths under it for file tools, and start "
            f"every Bash command with `cd {wt} && `.",
            f"3. Commit your work on {branch}. Never push, switch branches, or touch the main "
            "checkout or another worktree.",
            "4. To ask or tell a dependent or producer task something, use post_message. If you "
            "cannot continue without a decision from the user, call report_blocker and stop.",
            "5. When you are done, call complete_task with your key, token, and a short "
            "summary. It runs the checks and merges your branch. If it reports a failure, fix "
            "it and call complete_task again. Stop only when it says you are done or that the "
            "task failed.",
        ]
        return "\n".join(lines)

    # --- Worker tools ---------------------------------------------------------

    def claim(self, key: str, token: str) -> dict[str, Any]:
        with self.s.tx() as c:
            row = self._auth(c, key, token)
            if row["status"] not in RUNNING:
                raise EngineError("not_assigned", f"{key} is {row['status']}, not assigned to you")
            now = time.time()
            self.s.update_task(c, key, status="in_progress", claimed_at=now, heartbeat=now)
            self.s.log("task.claimed", conn=c, task=key)
        return {"ok": True, "worktree": row["worktree"], "branch": row["branch"]}

    def publish_contract(self, key: str, token: str, name: str, content: str) -> dict[str, Any]:
        _, plan = self.plan()
        task = self.task(plan, key)
        if name not in task.produces:
            raise EngineError(
                "not_producer",
                f"{key} does not produce {name}; it produces {', '.join(task.produces) or 'nothing'}",
            )
        if not content.strip():
            raise EngineError("empty_contract", "contract content is empty")
        kind = next((c.kind for c in plan.contracts if c.name == name), "other")
        consumers = [t.key for t in plan.tasks if name in t.consumes]
        with self.s.tx() as c:
            self._auth(c, key, token)
            row = c.execute(
                "SELECT COALESCE(MAX(version), 0) FROM contract_version WHERE name = ?", (name,)
            ).fetchone()
            version = int(row[0]) + 1
            c.execute(
                "INSERT INTO contract_version VALUES (?, ?, ?, ?, ?, ?)",
                (name, version, kind, content, key, time.time()),
            )
            running = [
                k for k, r in self.s.tasks(c).items() if k in consumers and r["status"] in RUNNING
            ]
            if running and version > 1:
                self._send(
                    c,
                    key,
                    "contract",
                    f"Contract {name} v{version} was published by {key}. Call "
                    f"read_contract('{name}') and adapt your work to it.",
                    running,
                )
            self.s.log("contract.published", conn=c, name=name, version=version, producer=key)
        return {
            "ok": True,
            "name": name,
            "version": version,
            "notified": running if version > 1 else [],
        }

    def read_contract(self, name: str, version: int | None = None) -> dict[str, Any]:
        with self.s.read() as c:
            if version is None:
                row = c.execute(
                    "SELECT * FROM contract_version WHERE name = ? ORDER BY version DESC LIMIT 1",
                    (name,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM contract_version WHERE name = ? AND version = ?", (name, version)
                ).fetchone()
        if row is None:
            raise EngineError("no_contract", f"contract {name} has not been published")
        return {"ok": True, **dict(row)}

    def post_message(
        self, key: str, token: str, kind: str, body: str, to: str | None = None
    ) -> dict[str, Any]:
        if kind not in ("question", "notice"):
            raise EngineError(
                "bad_kind", "kind must be question or notice (use report_blocker to block)"
            )
        _, plan = self.plan()
        keys = {t.key for t in plan.tasks}
        if to is not None:
            if to not in keys or to == key:
                raise EngineError("bad_recipient", f"{to} is not another task in the plan")
            recipients = [to]
        else:
            related = {b for a, b, _ in edges(plan) if a == key} | {
                a for a, b, _ in edges(plan) if b == key
            }
            recipients = sorted(related, key=key_order)
            if not recipients:
                raise EngineError(
                    "no_recipients", f"{key} has no related tasks; name one with `to`"
                )
        with self.s.tx() as c:
            self._auth(c, key, token)
            sent = c.execute("SELECT COUNT(*) FROM message WHERE from_task = ?", (key,)).fetchone()[
                0
            ]
            if sent >= MESSAGE_CAP:
                raise EngineError("message_cap", f"{key} has used its {MESSAGE_CAP} messages")
            mid = self._send(c, key, kind, body, recipients)
            self.s.log("message.posted", conn=c, id=mid, sender=key, to=recipients, kind=kind)
        return {"ok": True, "id": mid, "to": recipients, "remaining": MESSAGE_CAP - sent - 1}

    def read_messages(self, key: str, token: str) -> dict[str, Any]:
        with self.s.tx() as c:
            self._auth(c, key, token)
        return {"ok": True, "messages": self._inbox(key, mark=True, include_read=True)}

    def report_blocker(self, key: str, token: str, question: str) -> dict[str, Any]:
        with self.s.tx() as c:
            row = self._auth(c, key, token)
            if row["status"] not in RUNNING:
                raise EngineError("not_active", f"{key} is {row['status']}")
            self.s.update_task(c, key, status="blocked", blocker=question, token=None)
            self.s.log("task.blocked", conn=c, task=key, question=question)
        return {
            "ok": True,
            "message": "Stop now. Barnraise will ask the user and restart this task with the answer.",
        }

    def complete(self, key: str, token: str, summary: str) -> dict[str, Any]:
        """Verify, then merge under the integration lock (PRD B-20, B-21)."""
        _, plan = self.plan()
        task = self.task(plan, key)
        with self.s.tx() as c:
            row = self._auth(c, key, token)
            if row["status"] != "in_progress":
                raise EngineError(
                    "not_in_progress", f"{key} is {row['status']}; call claim_task first"
                )
        wt = Path(row["worktree"])
        integration = self.integration_branch()

        # Problems the worker fixes without spending an attempt.
        dirty = gitops.dirty_files(wt)
        if dirty:
            return self._retry_free(
                "uncommitted",
                "Commit (or discard) these changes, then call complete_task again:\n"
                + "\n".join(f"- {f}" for f in dirty[:50]),
            )
        published = self.s.contracts()
        missing = [n for n in task.produces if n not in published]
        if missing:
            return self._retry_free(
                "unpublished_contract",
                f"Publish {', '.join(missing)} with publish_contract first.",
            )
        outside = [
            f for f in gitops.changed_files(wt, integration) if not matches_owned(f, task.owns)
        ]
        if outside:
            return self._retry_free(
                "outside_owned_paths",
                "Your branch changes files you do not own:\n"
                + "\n".join(f"- {f}" for f in outside[:50])
                + f"\nRestore them (`git checkout {integration} -- <file>`, or `git rm` new "
                "ones), commit, and call complete_task again. If another task must change "
                "them, tell it with post_message.",
            )

        results = checks.run([*plan.setup, *task.acceptance], wt)
        if not all(r.ok for r in results):
            return self._failed_attempt(key, "acceptance checks failed", checks.summary(results))

        with self._integration_lock():
            int_wt = self.ensure_integration_worktree()
            before, _ = self._project_checks(int_wt, plan)
            ok, pre, conflicts = gitops.merge(
                int_wt, row["branch"], f"Barnraise: merge {key} ({task.title})"
            )
            if not ok:
                return self._conflict(key, conflicts, integration)
            after, report = self._project_checks(int_wt, plan)
            # Only regressions block a merge: early tasks cannot make the whole suite pass
            # (no tests exist yet), but nothing that passed may start failing.
            broken = [cmd for cmd, passed in before.items() if passed and not after.get(cmd)]
            if broken:
                gitops.reset_to(int_wt, pre)
                self.s.log("merge.result", task=key, outcome="checks_failed", broken=broken)
                return self._failed_attempt(
                    key,
                    f"project checks that passed on {integration} fail after merging your "
                    f"branch ({', '.join(broken)}); the merge was undone. Merge {integration} "
                    "into your branch, reproduce, and fix",
                    report,
                )
            merge_commit = gitops.out(int_wt, "rev-parse", "HEAD")
            with self.s.tx() as c:
                self.s.update_task(
                    c,
                    key,
                    status="done",
                    summary=summary,
                    merge_commit=merge_commit,
                    finished_at=time.time(),
                    token=None,
                )
                self.s.log(
                    "merge.result",
                    conn=c,
                    task=key,
                    outcome="merged",
                    commit=merge_commit,
                    checks_passing=sorted(k for k, v in after.items() if v),
                    checks_failing=sorted(k for k, v in after.items() if not v),
                )
                self.s.log("task.done", conn=c, task=key)
        return {
            "ok": True,
            "done": True,
            "message": f"Merged into {integration}. You are done; stop now.",
        }

    def _project_checks(self, int_wt: Path, plan: Plan) -> tuple[dict[str, bool], str]:
        """Which project checks pass at the integration worktree's HEAD (cached per commit)."""
        sha = gitops.out(int_wt, "rev-parse", "HEAD")
        digest = hashlib.sha256(json.dumps([plan.setup, plan.checks]).encode()).hexdigest()[:16]
        cache_id = f"{sha}:{digest}"
        with self.s.read() as c:
            row = c.execute(
                "SELECT results, summary FROM check_cache WHERE id = ?", (cache_id,)
            ).fetchone()
        if row is not None:
            return dict(json.loads(row[0])), str(row[1])
        results, report = checks.project_state(plan.setup, plan.checks, int_wt)
        with self.s.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO check_cache VALUES (?, ?, ?)",
                (cache_id, json.dumps(results), report),
            )
        return results, report

    def _retry_free(self, code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "done": False, "code": code, "message": message}

    def _failed_attempt(self, key: str, what: str, output: str) -> dict[str, Any]:
        with self.s.tx() as c:
            row = c.execute("SELECT attempts FROM task_state WHERE key = ?", (key,)).fetchone()
            attempts = int(row[0]) + 1
            failure = f"{what}:\n{output}"
            if attempts >= MAX_ATTEMPTS:
                self.s.update_task(
                    c,
                    key,
                    status="failed",
                    attempts=attempts,
                    last_failure=failure,
                    finished_at=time.time(),
                    token=None,
                )
                self.s.log("task.failed", conn=c, task=key, attempts=attempts)
                return {
                    "ok": False,
                    "done": True,
                    "code": "task_failed",
                    "message": f"{what}. That was attempt {attempts} of {MAX_ATTEMPTS}, so the task "
                    f"is marked failed. Stop now.\n\n{output}",
                }
            self.s.update_task(c, key, attempts=attempts, last_failure=failure)
            self.s.log("task.attempt_failed", conn=c, task=key, attempts=attempts)
        return {
            "ok": False,
            "done": False,
            "code": "checks_failed",
            "message": f"{what}. Fix it, commit, and call complete_task again (attempt "
            f"{attempts} of {MAX_ATTEMPTS} used).\n\n{output}",
        }

    def _conflict(self, key: str, files: list[str], integration: str) -> dict[str, Any]:
        with self.s.tx() as c:
            row = c.execute("SELECT conflicts FROM task_state WHERE key = ?", (key,)).fetchone()
            conflicts = int(row[0]) + 1
            self.s.update_task(c, key, conflicts=conflicts)
            self.s.log("merge.result", conn=c, task=key, outcome="conflict", files=files)
        if conflicts > MAX_CONFLICTS:
            return self._failed_attempt(key, "the branch keeps conflicting", "\n".join(files))
        return {
            "ok": False,
            "done": False,
            "code": "conflict",
            "message": f"Your branch conflicts with {integration} in:\n"
            + "\n".join(f"- {f}" for f in files)
            + f"\nRun `git merge {integration}` in your worktree, resolve the conflicts "
            "(keep both sides' intent), commit, and call complete_task again.",
        }

    # --- Messages -------------------------------------------------------------

    def _send(
        self, c: sqlite3.Connection, sender: str, kind: str, body: str, recipients: list[str]
    ) -> int:
        cur = c.execute(
            "INSERT INTO message (from_task, kind, body, created_at) VALUES (?, ?, ?, ?)",
            (sender, kind, body, time.time()),
        )
        mid = int(cur.lastrowid or 0)
        c.executemany(
            "INSERT OR IGNORE INTO delivery (message_id, task) VALUES (?, ?)",
            [(mid, r) for r in recipients],
        )
        return mid

    def _inbox(self, key: str, *, mark: bool, include_read: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.id, m.from_task, m.kind, m.body, d.delivered_at FROM delivery d "
            "JOIN message m ON m.id = d.message_id WHERE d.task = ?"
        )
        if not include_read:
            sql += " AND d.delivered_at IS NULL"
        with self.s.tx() as c:
            rows = c.execute(sql + " ORDER BY m.id", (key,)).fetchall()
            if mark:
                c.execute(
                    "UPDATE delivery SET delivered_at = ? WHERE task = ? AND delivered_at IS NULL",
                    (time.time(), key),
                )
        return [
            {"id": r[0], "from": r[1], "kind": r[2], "body": r[3], "read": r[4] is not None}
            for r in rows
        ]

    # --- Status (PRD B-5) -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        approved = self.s.approved_plan()
        if approved is None:
            return {}
        version, plan = approved
        rows = self.s.tasks()
        published = frozenset(self.s.contracts())
        status_map = {k: r["status"] for k, r in rows.items()}
        spend = self.s.spend_by_task()
        tasks = []
        for t in sorted(plan.tasks, key=lambda t: key_order(t.key)):
            r = rows.get(t.key, {})
            item: dict[str, Any] = {
                "key": t.key,
                "title": t.title,
                "status": r.get("status", "pending"),
            }
            if item["status"] == "pending":
                item["waiting_on"] = waiting_on(plan, status_map, published, t.key)
            if r.get("attempts"):
                item["attempts"] = r["attempts"]
            if t.key in spend:
                item["spent_usd"] = round(spend[t.key], 4)
            if item["status"] in ("failed",) and r.get("last_failure"):
                item["last_failure"] = str(r["last_failure"])[-3000:]
            if item["status"] == "blocked":
                item["question"] = r.get("blocker")
            if item["status"] == "done":
                item["summary"] = r.get("summary")
            tasks.append(item)
        contracts = [
            {"name": n, "version": c["version"], "producer": c["producer"]}
            for n, c in sorted(self.s.contracts().items())
        ]
        with self.s.read() as c:
            unread = c.execute(
                "SELECT COUNT(*) FROM delivery WHERE delivered_at IS NULL"
            ).fetchone()[0]
        config = self.s.config()
        return {
            "run_status": config.get("run_status", "not started"),
            "approved_version": version,
            "integration_branch": self.integration_branch(),
            "tasks": tasks,
            "contracts": contracts,
            "unread_messages": unread,
            "spent_usd": round(self.s.spent_usd(), 4),
            "budget_usd": config.get("budget_usd"),
        }
