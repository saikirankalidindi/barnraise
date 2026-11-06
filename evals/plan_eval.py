"""M1 acceptance: plan the 5 reference prompts and score the plans (PRD section 6, M1).

    uv run --project ../plugin/server --with claude-agent-sdk python plan_eval.py [model]
"""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin" / "server" / "src"))

from barnraise.store import Store  # noqa: E402
from barnraise.validate import analyse, validate  # noqa: E402
from session import new_repo, run_session  # noqa: E402

HERE = Path(__file__).resolve().parent
MODEL = sys.argv[1] if len(sys.argv) > 1 else "sonnet"


async def one(name: str, prompt: str) -> dict[str, object]:
    repo = new_repo(name)
    started = time.monotonic()
    t = await run_session(
        repo,
        ["/barnraise:init checks: none yet (new project)", f"/barnraise:plan {prompt}"],
        model=MODEL,
        timeout_s=900,
        verbose=False,
    )
    store = Store(repo)
    latest = store.latest_plan()
    rejected = len(store.events("plan.rejected"))
    result: dict[str, object] = {
        "prompt": name,
        "repo": str(repo),
        "seconds": round(time.monotonic() - started),
        "cost_usd": round(t.cost_usd, 3),
        "proposals_rejected": rejected,
    }
    if latest is None:
        return {**result, "valid": False, "parallel_pairs": 0}
    _, plan = latest
    issues = validate(plan)
    a = analyse(plan) if not issues else {}
    out_dir = HERE / "results" / "plans"
    out_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(store.plan_file, out_dir / f"{name}.yaml")
    return {
        **result,
        "valid": not issues,
        "tasks": len(plan.tasks),
        "contracts": len(plan.contracts),
        "parallel_pairs": a.get("parallel_pairs", 0),
        "peak_parallel": a.get("peak_parallel", 0),
        "max_speedup": a.get("max_speedup"),
        "critical_path": a.get("critical_path"),
    }


async def main() -> None:
    prompts = {p.stem: p.read_text().strip() for p in sorted((HERE / "prompts").glob("*.md"))}
    results = await asyncio.gather(*(one(n, p) for n, p in prompts.items()))
    passed = [r for r in results if r["valid"] and int(r["parallel_pairs"]) >= 1]  # type: ignore[call-overload]
    summary = {"model": MODEL, "passed": len(passed), "of": len(results), "results": results}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "plan_eval.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


asyncio.run(main())
