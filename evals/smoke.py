"""Smoke test: init, plan, and run a tiny Python project end to end with real Claude."""

import sys
import time

from session import new_repo, run, run_session, sh

repo = new_repo("smoke")
print("repo:", repo)
prompt = (
    "A tiny Python package `calc` (src layout, managed with uv, pytest for tests) with two "
    "independent modules: calc/add.py with add(a, b) and calc/mul.py with mul(a, b), each with "
    "its own test file. Keep it minimal."
)
start = time.monotonic()
t = run(
    run_session(
        repo,
        ["/barnraise:init checks: none yet (new project); agents: 3", f"/barnraise:plan {prompt}", "/barnraise:run"],
        model=sys.argv[1] if len(sys.argv) > 1 else "sonnet",
        timeout_s=1800,
    )
)
print(f"\nwall {time.monotonic() - start:.0f}s, driver cost ${t.cost_usd:.3f}")
print("tools used by driver:", sorted({n for _, n, _ in t.tools}))
print(sh("git", "log", "--oneline", "--graph", "barnraise/integration", cwd=repo))
print(sh("git", "ls-tree", "-r", "--name-only", "barnraise/integration", cwd=repo))
print(open(repo / ".barnraise" / "events.jsonl").read()[-3000:])
