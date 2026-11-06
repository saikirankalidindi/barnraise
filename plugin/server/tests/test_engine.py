"""Execution (M2, M3) on real temporary git repos, with a scripted stand-in for workers."""

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from barnraise import server
from barnraise.engine import MAX_ATTEMPTS, Engine, EngineError
from barnraise.plan import Plan
from barnraise.store import Store
from tests.conftest import git


def task(key: str, **kw: Any) -> dict[str, Any]:
    out = {
        "key": key,
        "title": f"Task {key}",
        "description": f"Build {key}",
        "acceptance": [f"test -f {key.lower()}/done.txt"],
        "owns": [f"{key.lower()}/**"],
    }
    out.update(kw)
    return out


def make_plan(
    *tasks: dict[str, Any],
    contracts: list[dict[str, Any]] | None = None,
    checks: list[str] | None = None,
) -> Plan:
    return Plan.from_dict(
        {
            "prompt": "test project",
            "checks": checks or ["true"],
            "contracts": contracts or [],
            "tasks": list(tasks),
        }
    )


def start(repo: Path, plan: Plan, **init: Any) -> Engine:
    assert server.init_project(checks=[], **init)["ok"]
    proposed = server.propose_plan(plan)
    assert proposed["ok"], proposed
    engine = Engine(Store(repo))
    assert engine.start_run()["ok"]
    return engine


def spawns(engine: Engine) -> dict[str, dict[str, Any]]:
    return {a["task"]: a for a in engine.next_actions()["actions"] if a["action"] == "spawn"}


class Worker:
    """Does what a barnraise:worker would, through the same engine calls."""

    def __init__(self, engine: Engine, spawn: dict[str, Any]) -> None:
        self.engine = engine
        self.key = spawn["task"]
        self.prompt: str = spawn["prompt"]
        self.token = re.search(r"^Token: (\w+)$", self.prompt, re.M).group(1)  # type: ignore[union-attr]
        self.wt = Path(re.search(r"^Worktree: (.+)$", self.prompt, re.M).group(1))  # type: ignore[union-attr]
        assert engine.claim(self.key, self.token)["ok"]

    def write(self, rel: str, text: str = "x\n", commit: bool = True) -> None:
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if commit:
            git(self.wt, "add", "-A")
            git(self.wt, "commit", "-q", "-m", f"{self.key}: {rel}")

    def finish(self) -> dict[str, Any]:
        self.write(f"{self.key.lower()}/done.txt", f"{self.key}\n")
        return self.complete()

    def complete(self) -> dict[str, Any]:
        return self.engine.complete(self.key, self.token, f"{self.key} finished")


def statuses(engine: Engine) -> dict[str, str]:
    return {k: v["status"] for k, v in engine.s.tasks().items()}


def test_agent_limit_is_never_exceeded(repo: Path) -> None:
    engine = start(repo, make_plan(*(task(f"T{i}") for i in range(5))), agent_limit=3)
    first = spawns(engine)
    assert list(first) == ["T0", "T1", "T2"]
    again = engine.next_actions()["actions"]
    assert [a["action"] for a in again] == ["wait"]  # no free slot
    workers = {k: Worker(engine, s) for k, s in first.items()}
    assert workers["T1"].finish()["done"] is True
    assert list(spawns(engine)) == ["T3"]
    running = [s for s in statuses(engine).values() if s in ("assigned", "in_progress")]
    assert len(running) == 3


def test_parallel_disjoint_tasks_both_merge(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1")))
    workers = [Worker(engine, s) for s in spawns(engine).values()]
    assert [w.finish()["done"] for w in workers] == [True, True]
    branch = "barnraise/integration"
    files = git(repo, "ls-tree", "-r", "--name-only", branch).splitlines()
    assert {"t0/done.txt", "t1/done.txt"} <= set(files)
    merges = git(repo, "log", "--merges", "--oneline", branch).splitlines()
    assert len(merges) == 2
    actions = engine.next_actions()["actions"]
    assert actions[0]["action"] == "done" and len(actions[0]["merges"]) == 2
    assert engine.s.config()["run_status"] == "done"
    # The user's checkout only gained the gitignore entry and the committed-by-design plan.
    status = git(repo, "status", "--short", "--untracked-files=all").splitlines()
    assert status == ["?? .barnraise/plan.yaml", "?? .gitignore"]
    assert git(repo, "branch", "--show-current") == "main"


def test_failing_checks_retry_twice_then_fail(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0", acceptance=["test -f t0/never.txt"])))
    w = Worker(engine, spawns(engine)["T0"])
    w.write("t0/a.txt")
    results = [w.complete() for _ in range(MAX_ATTEMPTS)]
    assert [r["code"] for r in results] == ["checks_failed", "checks_failed", "task_failed"]
    assert "test -f t0/never.txt  -> failed (1)" in results[0]["message"]
    assert results[-1]["done"] is True
    stopped = engine.next_actions()["actions"]
    assert stopped == [{**stopped[0], "action": "stopped", "reason": "failed", "failed": ["T0"]}]
    status = engine.status()["tasks"][0]
    assert status["status"] == "failed" and status["attempts"] == 3
    assert "never.txt" in status["last_failure"]


def test_project_checks_failing_undoes_the_merge(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), checks=["test ! -f t0/bad.txt"]))
    before = git(repo, "rev-parse", "barnraise/integration")
    w = Worker(engine, spawns(engine)["T0"])
    w.write("t0/bad.txt")
    out = w.finish()
    assert out["code"] == "checks_failed"
    assert "project checks that passed on barnraise/integration fail" in out["message"]
    assert git(repo, "rev-parse", "barnraise/integration") == before
    git(w.wt, "rm", "-q", "t0/bad.txt")
    git(w.wt, "commit", "-q", "-m", "fix")
    assert w.complete()["done"] is True
    assert engine.s.tasks()["T0"]["attempts"] == 1


def test_checks_failing_before_a_merge_do_not_block_it(repo: Path) -> None:
    # Like `pytest` with no tests yet: the suite can only pass once T1 has merged.
    engine = start(repo, make_plan(task("T0"), task("T1"), checks=["test -f t1/done.txt"]))
    t0, t1 = (Worker(engine, s) for s in spawns(engine).values())
    assert t0.finish()["done"] is True  # still failing, but not a regression
    assert t1.finish()["done"] is True  # now passing
    merged = [e for e in engine.s.events("merge.result") if e["outcome"] == "merged"]
    assert merged[0]["checks_failing"] == ["test -f t1/done.txt"]
    assert merged[1]["checks_passing"] == ["test -f t1/done.txt"]
    assert engine.next_actions()["actions"][0]["action"] == "done"


def test_final_checks_must_pass(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), checks=["test -f nobody/makes/this"]))
    assert Worker(engine, spawns(engine)["T0"]).finish()["done"] is True
    [stopped] = engine.next_actions()["actions"]
    assert stopped["action"] == "stopped" and stopped["reason"] == "final_checks"
    assert stopped["failing"] == ["test -f nobody/makes/this"]
    assert engine.s.config()["run_status"] == "running"  # not done


def test_conflicts_go_back_to_the_worker(repo: Path) -> None:
    # Both tasks add a dependency to package.json (a shared file any task may touch).
    (repo / "package.json").write_text('{\n  "name": "x"\n}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "pkg")
    engine = start(repo, make_plan(task("T0", owns=["t0/**"]), task("T1")))
    a, b = (Worker(engine, s) for s in spawns(engine).values())
    a.write("package.json", '{\n  "name": "x",\n  "a": 1\n}\n')
    b.write("package.json", '{\n  "name": "x",\n  "b": 1\n}\n')
    assert a.finish()["done"] is True
    out = b.finish()
    assert out["code"] == "conflict" and "package.json" in out["message"]
    res = subprocess.run(["git", "merge", "barnraise/integration"], cwd=b.wt, capture_output=True)
    assert res.returncode != 0  # the worker sees the same conflict and resolves it
    b.write("package.json", '{\n  "name": "x",\n  "a": 1,\n  "b": 1\n}\n')
    assert b.complete()["done"] is True


def test_retry_free_problems(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1")))
    w = Worker(engine, spawns(engine)["T0"])
    w.write("t0/done.txt", commit=False)
    assert w.complete()["code"] == "uncommitted"
    git(w.wt, "add", "-A")
    git(w.wt, "commit", "-q", "-m", "x")
    w.write("t1/stolen.txt")
    out = w.complete()
    assert out["code"] == "outside_owned_paths" and "t1/stolen.txt" in out["message"]
    git(w.wt, "rm", "-q", "t1/stolen.txt")
    git(w.wt, "commit", "-q", "-m", "undo")
    assert w.complete()["done"] is True
    assert engine.s.tasks()["T0"]["attempts"] == 0


def test_contract_edges_start_dependents_early(repo: Path) -> None:
    plan = make_plan(
        task("T0", produces=["api"]),
        task("T1", consumes=["api"]),
        task("T2", after=["T1"]),
        contracts=[{"name": "api", "kind": "openapi", "description": "the API"}],
    )
    engine = start(repo, plan)
    last_spawn = spawns(engine)
    assert list(last_spawn) == ["T0"]
    assert list(spawns(engine)) == []  # T0 already assigned; T1 waits for the contract
    producer = Worker(engine, last_spawn["T0"])
    out = engine.publish_contract("T0", producer.token, "api", "GET /tasks -> [Task]")
    assert out == {"ok": True, "name": "api", "version": 1, "notified": []}
    early = spawns(engine)
    assert list(early) == ["T1"]  # starts while T0 is still in progress
    assert "GET /tasks -> [Task]" in early["T1"]["prompt"]
    assert statuses(engine)["T0"] == "in_progress"
    consumer = Worker(engine, early["T1"])

    # v2 reaches the running consumer.
    v2 = engine.publish_contract("T0", producer.token, "api", "GET /tasks?done=true -> [Task]")
    assert v2["version"] == 2 and v2["notified"] == ["T1"]
    inbox = engine._inbox("T1", mark=False)
    assert "Contract api v2 was published" in inbox[0]["body"]

    # T2 has a completion edge, so it waits for T1's merge, then starts from it.
    assert spawns(engine) == {}
    assert consumer.finish()["done"] is True
    t2 = spawns(engine)["T2"]
    wt = Path(re.search(r"^Worktree: (.+)$", t2["prompt"], re.M).group(1))  # type: ignore[union-attr]
    assert (wt / "t1" / "done.txt").exists()
    assert producer.finish()["done"] is True


def test_producer_must_publish_before_completing(repo: Path) -> None:
    plan = make_plan(
        task("T0", produces=["api"]),
        task("T1", consumes=["api"]),
        contracts=[{"name": "api", "kind": "openapi", "description": "the API"}],
    )
    engine = start(repo, plan)
    w = Worker(engine, spawns(engine)["T0"])
    assert w.finish()["code"] == "unpublished_contract"
    with pytest.raises(EngineError, match="does not produce other"):
        engine.publish_contract("T0", w.token, "other", "x")
    with pytest.raises(EngineError, match="wrong token"):
        engine.publish_contract("T0", "forged", "api", "x")


def test_recovery_after_a_crash_reuses_the_branch(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0")))
    w = Worker(engine, spawns(engine)["T0"])
    w.write("t0/partial.txt")
    # The session dies: no tool calls for a long time.
    with engine.s.tx() as c:
        engine.s.update_task(c, "T0", heartbeat=time.time() - 3600)
    out = engine.start_run()
    assert out["recovered"] == ["T0"]
    assert statuses(engine)["T0"] == "pending"
    w2 = Worker(engine, spawns(engine)["T0"])
    assert (w2.wt / "t0" / "partial.txt").exists()
    with pytest.raises(EngineError, match="wrong token"):
        engine.claim("T0", w.token)  # the dead worker's token no longer works
    assert w2.finish()["done"] is True


def test_recent_workers_are_not_recovered(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0")))
    Worker(engine, spawns(engine)["T0"])
    out = engine.start_run()
    assert out["recovered"] == [] and out["possibly_still_running"] == ["T0"]


def test_pause_resume_cancel(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1"), task("T2")), agent_limit=1)
    w = Worker(engine, spawns(engine)["T0"])
    engine.control("pause")
    assert w.finish()["done"] is True
    assert engine.next_actions()["actions"][0]["reason"] == "paused"
    engine.control("resume")
    w1 = Worker(engine, spawns(engine)["T1"])
    out = engine.control("cancel")
    assert out["stopped"] == ["T1"]
    assert "cancelled" in engine._inbox("T1", mark=False)[0]["body"]
    with pytest.raises(EngineError, match="wrong token"):
        w1.complete()
    assert engine.next_actions()["actions"][0]["reason"] == "cancelled"
    assert engine.start_run()["recovered"] == ["T1"]
    assert list(spawns(engine)) == ["T1"]


def test_budget_stops_new_assignments(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1")), agent_limit=1, budget_usd=1.0)
    w = Worker(engine, spawns(engine)["T0"])
    with engine.s.tx() as c:
        c.execute("INSERT INTO agent_spend VALUES ('a1', 'T0', 1.5, 10, 10, 0)")
    assert w.finish()["done"] is True
    actions = engine.next_actions()["actions"]
    assert actions[0]["reason"] == "budget" and "budget" in actions[0]["message"]
    assert engine.status()["spent_usd"] == 1.5


def test_blocker_asks_the_user_and_restarts(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0")))
    w = Worker(engine, spawns(engine)["T0"])
    assert engine.report_blocker("T0", w.token, "Postgres or SQLite?")["ok"]
    [ask] = engine.next_actions()["actions"]
    assert ask["action"] == "ask_user" and ask["question"] == "Postgres or SQLite?"
    engine.answer_blocker("T0", "SQLite")
    again = spawns(engine)["T0"]
    assert "The user answered: SQLite" in again["prompt"]


def test_messages(repo: Path) -> None:
    plan = make_plan(task("T0"), task("T1", after=["T0"]), task("T2"))
    engine = start(repo, plan)
    w = Worker(engine, spawns(engine)["T0"])
    out = engine.post_message("T0", w.token, "notice", "Use UTC everywhere")
    assert out["to"] == ["T1"]  # its dependents
    assert engine.post_message("T0", w.token, "question", "hi", to="T2")["to"] == ["T2"]
    for i in range(3):
        engine.post_message("T0", w.token, "notice", f"n{i}")
    with pytest.raises(EngineError, match="5 messages"):
        engine.post_message("T0", w.token, "notice", "one too many")
    assert w.finish()["done"] is True
    t1 = spawns(engine)["T1"]
    assert "Use UTC everywhere" in t1["prompt"]


def test_started_tasks_are_frozen(repo: Path) -> None:
    plan = make_plan(task("T0"), task("T1"))
    engine = start(repo, plan, agent_limit=1)
    Worker(engine, spawns(engine)["T0"])
    changed = make_plan(task("T0", title="Renamed"), task("T1", title="Also renamed"))
    out = server.propose_plan(changed)
    assert out["ok"] is False
    assert [e["message"] for e in out["errors"]] == [
        "T0 is in_progress; only pending tasks can change"
    ]
    ok = server.propose_plan(make_plan(task("T0"), task("T1", title="Also renamed"), task("T2")))
    assert ok["ok"] is True
    assert engine.start_run()["plan_version"] == ok["version"]
    assert set(engine.s.tasks()) == {"T0", "T1", "T2"}


def test_unclaimed_assignments_are_released(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0")))
    spawns(engine)
    with engine.s.tx() as c:
        engine.s.update_task(c, "T0", assigned_at=time.time() - 3600)
    assert list(spawns(engine)) == ["T0"]  # released and reassigned with a new token


async def test_wait_for_change_returns_on_events(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1")))
    w = Worker(engine, spawns(engine)["T0"])
    quiet = await engine.wait_for_change(1.5)
    assert quiet["changed"] is False

    timer = threading.Timer(0.5, w.finish)
    timer.start()
    started = time.monotonic()
    out = await engine.wait_for_change(10)
    timer.join()
    assert out["changed"] is True and time.monotonic() - started < 5
    assert any(e["type"] == "task.done" and e["task"] == "T0" for e in out["events"])


async def test_wait_does_not_miss_changes_after_next_actions(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0"), task("T1")))
    w = Worker(engine, spawns(engine)["T0"])
    [wait] = [a for a in engine.next_actions()["actions"] if a["action"] == "wait"]
    w.finish()  # happens before the driver gets round to waiting
    started = time.monotonic()
    out = await engine.wait_for_change(30, cursor=wait["cursor"])
    assert out["changed"] is True and time.monotonic() - started < 1


def test_events_jsonl_mirrors_the_log(repo: Path) -> None:
    engine = start(repo, make_plan(task("T0")))
    assert Worker(engine, spawns(engine)["T0"]).finish()["done"]
    lines = (repo / ".barnraise" / "events.jsonl").read_text().splitlines()
    types = [json.loads(line)["type"] for line in lines]
    assert types[0] == "project.initialized"
    assert {"plan.saved", "plan.approved", "run.started", "task.assigned", "task.claimed",
            "merge.result", "task.done"} <= set(types)  # fmt: skip
    seqs = [json.loads(line)["seq"] for line in lines]
    assert seqs == sorted(seqs)
