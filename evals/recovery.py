"""M2 acceptance: kill the Claude Code session mid-run, then resume with a new session.

    uv run --project ../plugin/server --with claude-agent-sdk python recovery.py [model]
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "server" / "src"))

from barnraise.store import Store  # noqa: E402
from e2e import verify  # noqa: E402
from session import new_repo, run_session, sh  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"
PROMPT = (
    "A small Python package `geo` (src layout, uv, pytest) with three independent modules, each "
    "with its own test file: geo/distance.py (haversine distance between two lat/lon points), "
    "geo/bearing.py (initial bearing between two points), and geo/midpoint.py (geographic "
    "midpoint of two points)."
)


def kill_tree_in(repo: Path) -> list[int]:
    """SIGKILL every process whose working directory is inside `repo` (the CLI, workers'
    shells, test runs), the way a crash would."""
    import os
    import signal

    killed = []
    root = str(repo.resolve())
    for pid_dir in Path("/proc").iterdir():
        if not pid_dir.name.isdigit() or int(pid_dir.name) == os.getpid():
            continue
        try:
            cwd = os.readlink(pid_dir / "cwd")
        except OSError:
            continue
        if cwd == root or cwd.startswith(root + "/"):
            try:
                os.kill(int(pid_dir.name), signal.SIGKILL)
                killed.append(int(pid_dir.name))
            except OSError:
                pass
    return killed


async def main() -> None:
    repo = new_repo("recovery")
    print("repo:", repo, flush=True)
    store = Store(repo)
    await run_session(repo, ["/barnraise:init checks: none yet; agents: 3", f"/barnraise:plan {PROMPT}"], model=MODEL)

    # Session 1: run until at least two workers are mid-task, then hard-kill the session.
    run1 = asyncio.create_task(run_session(repo, ["/barnraise:run"], model=MODEL, timeout_s=1800))
    killed_with = None
    for _ in range(600):
        await asyncio.sleep(1)
        tasks = store.tasks()
        in_progress = [k for k, v in tasks.items() if v["status"] == "in_progress"]
        if len(in_progress) >= 2:
            pids = kill_tree_in(repo)  # like a crash or closing the terminal: SIGKILL
            killed_with = {k: v["status"] for k, v in store.tasks().items()}
            print(f"SIGKILLed {len(pids)} processes", flush=True)
            run1.cancel()
            break
        if run1.done():
            break
    try:
        await run1
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(3)
    print("killed session 1 with task states:", killed_with, flush=True)
    print("task states 3 s after the kill:", {k: v["status"] for k, v in store.tasks().items()}, flush=True)
    commits_before = {
        k: sh("git", "rev-list", "--count", f"barnraise/integration..barnraise/{k}", cwd=repo)
        for k in (killed_with or {})
        if killed_with and killed_with[k] == "in_progress"
    }
    print("commits on in-progress branches at kill time:", commits_before, flush=True)

    # Session 2: the user knows session 1 is gone.
    started = time.monotonic()
    await run_session(repo, ["/barnraise:run --recover"], model=MODEL, timeout_s=1800)
    recovered = [e for e in store.events("run.started")][-1].get("recovered")
    result = {
        "killed_with": killed_with,
        "commits_on_branches_at_kill": commits_before,
        "recovered": recovered,
        "resume_wall_s": round(time.monotonic() - started),
        "final_tasks": {k: (v["status"], v["attempts"]) for k, v in store.tasks().items()},
        "run_status": store.config().get("run_status"),
        "verify": verify(repo, "barnraise/integration"),
    }
    out = Path(__file__).resolve().parent / "results" / "recovery.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


asyncio.run(main())
