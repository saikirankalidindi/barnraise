"""M4 acceptance: build the task-manager prompt with Barnraise and with a single agent, then
verify both independently and compare wall-clock time (PRD section 6, M4).

    uv run --project ../plugin/server --with claude-agent-sdk python e2e.py [model] [agent_limit]
"""

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "server" / "src"))

from barnraise.store import Store  # noqa: E402
from session import new_repo, run, run_session, sh  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"
AGENTS = sys.argv[2] if len(sys.argv) > 2 else "3"
PROMPT = (HERE / "prompts" / "task-manager.md").read_text().strip()


def verify(repo: Path, ref: str) -> dict[str, Any]:
    """Check out `ref` fresh and run the project's tests there."""
    wt = Path(tempfile.mkdtemp(prefix="bp-verify-")) / "wt"
    sh("git", "worktree", "add", "-q", "--detach", str(wt), ref, cwd=repo)
    sync = subprocess.run("uv sync -q", shell=True, cwd=wt, capture_output=True, text=True)
    tests = subprocess.run(
        "uv run pytest -q 2>&1 | tail -3", shell=True, cwd=wt, capture_output=True, text=True
    )
    status = subprocess.run("uv run pytest -q", shell=True, cwd=wt, capture_output=True, text=True)
    files = sh("git", "ls-tree", "-r", "--name-only", ref, cwd=repo).splitlines()
    return {
        "ref": ref,
        "uv_sync_ok": sync.returncode == 0,
        "pytest_ok": status.returncode == 0,
        "pytest_tail": tests.stdout.strip(),
        "files": len(files),
        "python_files": len([f for f in files if f.endswith(".py")]),
    }


def barnraise_run() -> dict[str, Any]:
    repo = new_repo("e2e-barnraise")
    print("barnraise repo:", repo, flush=True)
    phases = {}
    started = time.monotonic()
    cost = 0.0
    for name, prompt in [
        ("init", f"/barnraise:init checks: none yet (new project); agents: {AGENTS}"),
        ("plan", f"/barnraise:plan {PROMPT}"),
        ("run", "/barnraise:run"),
    ]:
        t0 = time.monotonic()
        t = run(run_session(repo, [prompt], model=MODEL, timeout_s=5400))
        phases[name] = round(time.monotonic() - t0)
        cost += t.cost_usd
    # The three phases ran as separate sessions; recovery (start_run) handles that fine.
    wall = round(time.monotonic() - started)
    store = Store(repo)
    tasks = store.tasks()
    events = store.events()
    merges = [e for e in events if e["type"] == "merge.result"]
    return {
        "repo": str(repo),
        "wall_s": wall,
        "phases_s": phases,
        "driver_cost_usd": round(cost, 3),
        "worker_spend_estimate_usd": round(store.spent_usd(), 3),
        "tasks": {k: (v["status"], v["attempts"]) for k, v in tasks.items()},
        "merge_outcomes": [(e["task"], e["outcome"]) for e in merges],
        "run_status": store.config().get("run_status"),
        "verify": verify(repo, "barnraise/integration"),
    }


def single_agent() -> dict[str, Any]:
    repo = new_repo("e2e-single")
    print("single-agent repo:", repo, flush=True)
    started = time.monotonic()
    t = run(
        run_session(
            repo,
            [
                "Build this project in this repository, then make sure `uv sync` and "
                "`uv run pytest` pass and commit everything. Use sensible defaults and do not "
                f"ask me questions.\n\n{PROMPT}"
            ],
            model=MODEL,
            timeout_s=5400,
        )
    )
    return {
        "repo": str(repo),
        "wall_s": round(time.monotonic() - started),
        "cost_usd": round(t.cost_usd, 3),
        "verify": verify(repo, "HEAD"),
    }


if __name__ == "__main__":
    which = sys.argv[3] if len(sys.argv) > 3 else "both"
    out: dict[str, Any] = {"model": MODEL, "agent_limit": int(AGENTS)}
    if which in ("both", "barnraise"):
        out["barnraise"] = barnraise_run()
        print(json.dumps(out["barnraise"], indent=2), flush=True)
    if which in ("both", "single"):
        out["single_agent"] = single_agent()
        print(json.dumps(out["single_agent"], indent=2), flush=True)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / f"e2e-{which}.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
