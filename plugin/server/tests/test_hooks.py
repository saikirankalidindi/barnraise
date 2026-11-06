"""Hooks (PRD section 4) with recorded-style inputs, plus the launcher as Claude Code runs it."""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from barnraise import hooks
from barnraise.engine import Engine
from tests.test_engine import Worker, make_plan, spawns, start, task

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
CLAIM = "mcp__plugin_barnraise_barnraise__claim_task"


def pre(tool: str, args: dict[str, Any], agent: str | None = "agent-1") -> dict[str, Any]:
    return {
        "hook_event_name": "PreToolUse",
        "agent_id": agent,
        "tool_name": tool,
        "tool_input": args,
    }


def decision(out: dict[str, Any] | None) -> str | None:
    return None if out is None else out["hookSpecificOutput"]["permissionDecision"]


class Running(NamedTuple):
    engine: Engine
    worker: Worker
    spawned: dict[str, dict[str, Any]]


@pytest.fixture
def running(repo: Path) -> Running:
    engine = start(
        repo,
        make_plan(
            task("T0", owns=["src/api/**", "t0/**"]), task("T1", owns=["src/ui/**", "t1/**"])
        ),
    )
    started = spawns(engine)
    w = Worker(engine, started["T0"])
    # Claude Code fires PreToolUse for the worker's claim_task call first.
    assert hooks.handle(pre(CLAIM, {"key": "T0", "token": w.token}), repo) is None
    assert engine.s.tasks()["T0"]["agent_id"] == "agent-1"
    return Running(engine, w, started)


def test_claim_with_a_wrong_token_maps_nothing(repo: Path, running: Running) -> None:
    engine = running.engine
    hooks.handle(pre(CLAIM, {"key": "T1", "token": "forged"}, agent="agent-2"), repo)
    assert engine.s.tasks()["T1"]["agent_id"] is None


def test_file_writes_are_confined(repo: Path, running: Running) -> None:
    w = running.worker
    wt = w.wt
    allowed = [
        str(wt / "src/api/routes.ts"),
        "src/api/x/y.ts",  # relative to the worktree
        str(wt / "package.json"),  # shared file
    ]
    for path in allowed:
        assert decision(hooks.handle(pre("Write", {"file_path": path}), repo)) is None, path
    outside = hooks.handle(pre("Write", {"file_path": str(repo / "src/api/routes.ts")}), repo)
    assert decision(outside) == "deny"
    assert "outside your worktree" in outside["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
    unowned = hooks.handle(pre("Edit", {"file_path": str(wt / "src/ui/app.tsx")}), repo)
    assert decision(unowned) == "deny"
    reason = unowned["hookSpecificOutput"]["permissionDecisionReason"]  # type: ignore[index]
    assert "src/ui/app.tsx is not in the paths task T0 owns (src/api/**, t0/**)" in reason
    # The main session (no agent_id) and unknown agents are never restricted.
    assert hooks.handle(pre("Write", {"file_path": "/etc/x"}, agent=None), repo) is None
    assert hooks.handle(pre("Write", {"file_path": "/etc/x"}, agent="stranger"), repo) is None


def test_bash_cd_and_push(repo: Path, running: Running) -> None:
    w = running.worker
    ok = [f"cd {w.wt} && pnpm test", "cd src/api && ls", "git commit -m 'x'", "echo cd /tmp"]
    for cmd in ok:
        assert decision(hooks.handle(pre("Bash", {"command": cmd}), repo)) is None, cmd
    for cmd in [f"cd {repo} && git status", "cd .. && ls", "ls; cd /tmp", "git push origin HEAD"]:
        assert decision(hooks.handle(pre("Bash", {"command": cmd}), repo)) == "deny", cmd


def test_messages_are_delivered_once_with_a_heartbeat(repo: Path, running: Running) -> None:
    engine, _, spawned = running
    t1 = Worker(engine, spawned["T1"])
    engine.post_message("T1", t1.token, "notice", "The API returns ISO dates", to="T0")
    post = {"hook_event_name": "PostToolUse", "agent_id": "agent-1", "tool_name": "Bash"}
    out = hooks.handle(post, repo)
    assert out is not None
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "from T1 (notice): The API returns ISO dates" in context
    assert hooks.handle(post, repo) is None  # delivered once
    assert engine.s.tasks()["T0"]["heartbeat"] is not None


def test_stop_gate_then_release(repo: Path, running: Running, tmp_path: Path) -> None:
    engine = running.engine
    transcript = tmp_path / "agent.jsonl"
    entry = {
        "type": "assistant",
        "message": {
            "id": "m1",
            "model": "claude-sonnet-5-5",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 2000,
                "cache_read_input_tokens": 10000,
            },
        },
    }
    transcript.write_text(json.dumps(entry) + "\n" + json.dumps(entry) + "\n")  # duplicate line
    stop = {
        "hook_event_name": "SubagentStop",
        "agent_id": "agent-1",
        "agent_transcript_path": str(transcript),
    }
    for _ in range(hooks.MAX_STOP_BLOCKS):
        out = hooks.handle(stop, repo)
        assert out is not None and out["decision"] == "block"
        assert "Call complete_task with key T0" in out["reason"]
    assert hooks.handle(stop, repo) is None  # gives up and releases the task
    row = engine.s.tasks()["T0"]
    assert (row["status"], row["attempts"], row["agent_id"]) == ("pending", 1, None)
    # (1000*3 + 10000*0.3 + 2000*15) / 1e6, counted once.
    assert engine.s.spent_usd() == pytest.approx(0.036)


def test_a_finished_worker_may_stop(repo: Path, running: Running) -> None:
    w = running.worker
    assert w.finish()["done"] is True
    assert hooks.handle({"hook_event_name": "SubagentStop", "agent_id": "agent-1"}, repo) is None


def test_launcher_end_to_end(repo: Path, running: Running, tmp_path: Path) -> None:
    w = running.worker
    script = PLUGIN_ROOT / "hooks" / "barnraise_hook.py"
    payload = json.dumps(pre("Write", {"file_path": str(repo / "README.md")}))
    env = {"CLAUDE_PROJECT_DIR": str(repo), "PATH": "/usr/bin:/bin"}
    res = subprocess.run(
        [sys.executable, str(script)], input=payload, capture_output=True, text=True, env=env
    )
    assert res.returncode == 0
    assert json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Projects without Barnraise state: silent no-op. Garbage input: still exit 0.
    other = tmp_path / "plain"
    other.mkdir()
    for stdin in (payload, "not json"):
        res = subprocess.run(
            [sys.executable, str(script)], input=stdin, capture_output=True, text=True,
            env={**env, "CLAUDE_PROJECT_DIR": str(other)},
        )  # fmt: skip
        assert (res.returncode, res.stdout) == (0, "")
    del w
