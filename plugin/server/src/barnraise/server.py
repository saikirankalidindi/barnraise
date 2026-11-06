"""The `barnraise` MCP server (PRD 4.1).

Driver tools (main session): status, init_project, get_plan, sync_plan, approve_plan,
start_run, next_actions, wait_for_change, answer_blocker, control.
Planner tool: propose_plan.
Worker tools (authenticated with the task token from the brief): claim_task,
publish_contract, read_contract, post_message, read_messages, report_blocker,
complete_task.
"""

import hashlib
import shutil
from collections.abc import Callable
from typing import Any, Literal

from barnraise import __version__
from barnraise import plan as planmod
from barnraise.engine import Engine, EngineError
from barnraise.mcp_stdio import StdioServer
from barnraise.plan import Issue, Plan
from barnraise.project import ProjectError, detect_checks
from barnraise.project import init_project as do_init
from barnraise.store import Store, project_dir
from barnraise.validate import analyse, validate

mcp = StdioServer(
    name="barnraise",
    version=__version__,
    instructions="Barnraise turns a project prompt into a validated, contract-first plan of "
    "parallel tasks, runs them on barnraise:worker subagents in their own git worktrees, and "
    "merges each verified task into one integration branch. Use the /barnraise:* skills; "
    "workers use only the tools their brief names.",
)


def _store() -> Store:
    return Store(project_dir())


def _engine() -> Engine:
    return Engine(_store())


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _unsynced(store: Store) -> bool:
    """plan.yaml was edited since Barnraise last wrote or synced it."""
    if not store.plan_file.exists():
        return False
    return _sha(store.plan_file.read_text()) != store.config().get("plan_file_sha")


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "errors": [{"code": code, "message": message}]}


def _not_initialized() -> dict[str, Any]:
    return _fail("not_initialized", "run /barnraise:init first")


def _errors(issues: list[Issue]) -> dict[str, Any]:
    return {"ok": False, "errors": [i.to_dict() for i in issues]}


def _guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return fn()
    except EngineError as exc:
        return exc.to_dict()


# --- Setup and planning -------------------------------------------------------


@mcp.tool()
def status() -> dict[str, Any]:
    """Barnraise's view of this project: config, plan, task progress, contracts, spend. Read-only."""
    store = _store()
    out: dict[str, Any] = {
        "version": __version__,
        "project_dir": str(store.root),
        "initialized": store.initialized,
    }
    config = store.config()
    if config:
        out["config"] = {k: v for k, v in config.items() if k != "plan_file_sha"}
    if not config.get("checks"):
        out["suggested_checks"] = detect_checks(store.root)
    latest = store.latest_plan()
    if latest:
        version, plan = latest
        issues = validate(plan)
        out["plan"] = {
            "version": version,
            "file": str(store.plan_file),
            "tasks": len(plan.tasks),
            "valid": not issues,
            "unsynced_edits": _unsynced(store),
        }
        if not issues:
            out["plan"]["analysis"] = analyse(plan)
    run = Engine(store).status()
    if run:
        out["run"] = run
    return out


@mcp.tool()
def init_project(
    checks: list[str],
    agent_limit: int = 3,
    budget_usd: float | None = None,
    worker_model: Literal["inherit", "sonnet", "opus", "haiku"] = "inherit",
) -> dict[str, Any]:
    """Set up Barnraise in this repo: .barnraise/, the integration branch, and the project
    checks that define green (may be empty for a new project; the plan then supplies them).
    Safe to run again to change the agent limit, budget, or worker model."""
    try:
        result = do_init(project_dir(), checks, agent_limit=agent_limit, budget_usd=budget_usd)
    except ProjectError as exc:
        return _fail("init_failed", str(exc))
    _store().set_config(worker_model=None if worker_model == "inherit" else worker_model)
    return {"ok": True, **result}


def _plan_issues(store: Store, plan: Plan) -> list[Issue]:
    return validate(plan) + Engine(store).frozen_issues(plan)


@mcp.tool()
def propose_plan(plan: Plan) -> dict[str, Any]:
    """Validate and store a plan (planner only). On errors nothing is stored: fix every
    listed problem and call this again with the whole corrected plan."""
    store = _store()
    if not store.initialized:
        return _not_initialized()
    issues = _plan_issues(store, plan)
    if issues:
        store.log("plan.rejected", codes=sorted({i.code for i in issues}), count=len(issues))
        return _errors(issues)
    backup = None
    if _unsynced(store):
        backup = store.plan_file.with_name("plan.yaml.bak")
        shutil.copyfile(store.plan_file, backup)
    version = store.save_plan(plan, source="planner")
    text = planmod.to_yaml(plan)
    store.plan_file.write_text(text)
    store.set_config(plan_file_sha=_sha(text))
    out: dict[str, Any] = {
        "ok": True,
        "version": version,
        "plan_file": str(store.plan_file),
        "analysis": analyse(plan),
    }
    if backup:
        out["note"] = f"plan.yaml had unsynced edits; they were saved to {backup}"
    return out


@mcp.tool()
def get_plan() -> dict[str, Any]:
    """The current plan as plan.yaml text, with validation problems and analysis."""
    store = _store()
    latest = store.latest_plan()
    if latest is None:
        return _fail("no_plan", "no plan yet; run /barnraise:plan")
    version, plan = latest
    issues = validate(plan)
    out: dict[str, Any] = {
        "ok": True,
        "version": version,
        "approved_version": store.config().get("approved_version"),
        "plan_file": str(store.plan_file),
        "yaml": planmod.to_yaml(plan),
        "unsynced_edits": _unsynced(store),
        "errors": [i.to_dict() for i in issues],
    }
    if not issues:
        out["analysis"] = analyse(plan)
    return out


@mcp.tool()
def sync_plan() -> dict[str, Any]:
    """Re-read .barnraise/plan.yaml after it was edited, validate it, and store it as the
    new plan version. Errors carry the line number in plan.yaml."""
    store = _store()
    if not store.initialized:
        return _not_initialized()
    if not store.plan_file.exists():
        return _fail("no_plan_file", f"{store.plan_file} does not exist")
    text = store.plan_file.read_text()
    plan, issues = planmod.from_yaml(text)
    if plan is None:
        return _errors(issues)
    lines = planmod.line_index(text)
    issues = [planmod.with_line(i, lines) for i in _plan_issues(store, plan)]
    if issues:
        return _errors(issues)
    latest = store.latest_plan()
    store.set_config(plan_file_sha=_sha(text))
    if latest and latest[1] == plan:
        return {"ok": True, "changed": False, "version": latest[0]}
    version = store.save_plan(plan, source="sync")
    return {"ok": True, "changed": True, "version": version, "analysis": analyse(plan)}


@mcp.tool()
def approve_plan(version: int | None = None) -> dict[str, Any]:
    """Approve a plan version (default: the latest) for execution. Only after the user
    agreed to it, e.g. after /barnraise:replan."""
    return _guard(lambda: _engine().approve(version))


# --- Driver -------------------------------------------------------------------


@mcp.tool()
def start_run(force_recover: bool = False) -> dict[str, Any]:
    """Start or resume execution (/barnraise:run). Approves the latest plan if it is newer
    than the approved one. Tasks whose worker went quiet for 5 minutes are restarted;
    force_recover restarts every in-progress task (use only if no workers are running)."""
    return _guard(lambda: _engine().start_run(force_recover))


@mcp.tool()
def next_actions() -> dict[str, Any]:
    """What the driver must do now: spawn workers, ask the user, wait, or report done or
    stopped. Deterministic: the same state always gives the same actions."""
    return _guard(lambda: _engine().next_actions())


@mcp.tool()
async def wait_for_change(cursor: int | None = None, timeout_s: float = 240) -> dict[str, Any]:
    """Block until a task finishes, blocks, or is released, a contract is published, or the
    run is paused or cancelled after `cursor` (pass the cursor from next_actions' wait
    action), or until the timeout. Then call next_actions."""
    try:
        return await _engine().wait_for_change(timeout_s, cursor)
    except EngineError as exc:
        return exc.to_dict()


@mcp.tool()
def answer_blocker(key: str, answer: str) -> dict[str, Any]:
    """Give a blocked task the user's answer; it restarts with it."""
    return _guard(lambda: _engine().answer_blocker(key, answer))


@mcp.tool()
def control(action: Literal["pause", "resume", "cancel"]) -> dict[str, Any]:
    """Pause (finish running tasks, start no new ones), resume, or cancel (stop every
    running worker; worktrees are kept)."""
    return _guard(lambda: _engine().control(action))


# --- Workers ------------------------------------------------------------------


@mcp.tool()
def claim_task(key: str, token: str) -> dict[str, Any]:
    """Worker: start your task. Call this first, with the key and token from your brief."""
    return _guard(lambda: _engine().claim(key, token))


@mcp.tool()
def publish_contract(key: str, token: str, name: str, content: str) -> dict[str, Any]:
    """Worker: publish (a new version of) a contract your task produces. Dependents that
    only need the interface start as soon as it is published."""
    return _guard(lambda: _engine().publish_contract(key, token, name, content))


@mcp.tool()
def read_contract(name: str, version: int | None = None) -> dict[str, Any]:
    """Read the latest (or a given) version of a published contract."""
    return _guard(lambda: _engine().read_contract(name, version))


@mcp.tool()
def post_message(
    key: str,
    token: str,
    kind: Literal["question", "notice"],
    body: str,
    to: str | None = None,
) -> dict[str, Any]:
    """Worker: send a short message to another task (`to`), or to every task your task
    depends on or that depends on it. At most 5 per task."""
    return _guard(lambda: _engine().post_message(key, token, kind, body, to))


@mcp.tool()
def read_messages(key: str, token: str) -> dict[str, Any]:
    """Worker: every message sent to your task."""
    return _guard(lambda: _engine().read_messages(key, token))


@mcp.tool()
def report_blocker(key: str, token: str, question: str) -> dict[str, Any]:
    """Worker: you cannot continue without a decision from the user. Ask one clear
    question, then stop; the task restarts with the answer."""
    return _guard(lambda: _engine().report_blocker(key, token, question))


@mcp.tool()
def complete_task(key: str, token: str, summary: str) -> dict[str, Any]:
    """Worker: finish your task. Runs the acceptance checks in your worktree, merges your
    branch into the integration branch, and runs the project checks. Read the result: if
    it is not done, fix what it says and call it again."""
    return _guard(lambda: _engine().complete(key, token, summary))


def main() -> None:
    mcp.serve()
