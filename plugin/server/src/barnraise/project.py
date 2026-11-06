"""/barnraise:init (PRD B-1): integration branch, gitignore, project checks."""

import json
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from barnraise.store import STATE_DIR, Store

INTEGRATION_BRANCH = "barnraise/integration"
GITIGNORE_LINES = [f"{STATE_DIR}/*", f"!{STATE_DIR}/plan.yaml"]
DEFAULT_AGENT_LIMIT = 3


class ProjectError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if res.returncode != 0:
        raise ProjectError(res.stderr.strip() or f"git {' '.join(args)} failed")
    return res.stdout.strip()


def detect_checks(root: Path) -> list[str]:
    """Best guess at the commands that define green for this repo."""
    checks: list[str] = []
    pkg = root / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text()).get("scripts") or {}
        except json.JSONDecodeError:
            scripts = {}
        runner = (
            "pnpm"
            if (root / "pnpm-lock.yaml").exists()
            else "yarn"
            if (root / "yarn.lock").exists()
            else "bun"
            if (root / "bun.lock").exists() or (root / "bun.lockb").exists()
            else "npm"
        )
        for name in ("lint", "typecheck", "test"):
            if name in scripts:
                checks.append(f"{runner} test" if name == "test" else f"{runner} run {name}")
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text()
            tomllib.loads(text)
        except (tomllib.TOMLDecodeError, OSError):
            text = ""
        if "pytest" in text or (root / "tests").is_dir():
            checks.append("uv run pytest" if (root / "uv.lock").exists() else "pytest")
    if (root / "Cargo.toml").exists():
        checks.append("cargo test")
    if (root / "go.mod").exists():
        checks.append("go test ./...")
    makefile = root / "Makefile"
    if not checks and makefile.exists() and "\ntest:" in "\n" + makefile.read_text():
        checks.append("make test")
    return checks


def ensure_gitignore(root: Path) -> bool:
    """Ignore Barnraise's state except plan.yaml. Returns True if the file changed."""
    path = root / ".gitignore"
    text = path.read_text() if path.exists() else ""
    lines = text.splitlines()
    missing = [line for line in GITIGNORE_LINES if line not in lines]
    if not missing:
        return False
    block = "\n# Barnraise state (plan.yaml is committed)\n" + "\n".join(missing) + "\n"
    path.write_text(text + ("" if text.endswith("\n") or not text else "\n") + block.lstrip("\n"))
    return True


def init_project(
    root: Path,
    checks: list[str],
    *,
    agent_limit: int = DEFAULT_AGENT_LIMIT,
    budget_usd: float | None = None,
    integration_branch: str = INTEGRATION_BRANCH,
) -> dict[str, Any]:
    """Create .barnraise/ and the integration branch. Safe to run again (updates config)."""
    if not 1 <= agent_limit <= 8:
        raise ProjectError("agent_limit must be between 1 and 8")
    try:
        top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
    except ProjectError:
        raise ProjectError(f"{root} is not a git repository; run `git init` first") from None
    if top != root.resolve():
        raise ProjectError(f"run Barnraise from the repository root ({top}), not {root}")
    try:
        head = git(root, "rev-parse", "HEAD")
    except ProjectError:
        raise ProjectError("the repository has no commits yet; make an initial commit") from None

    created_branch = False
    try:
        git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{integration_branch}")
    except ProjectError:
        git(root, "branch", integration_branch, head)
        created_branch = True

    store = Store(root)
    store.set_config(
        integration_branch=integration_branch,
        base_commit=head,
        checks=[c for c in (s.strip() for s in checks) if c],
        agent_limit=agent_limit,
        budget_usd=budget_usd,
    )
    gitignore_changed = ensure_gitignore(root)
    store.log("project.initialized", branch=integration_branch, created_branch=created_branch)
    return {
        "integration_branch": integration_branch,
        "created_branch": created_branch,
        "base_commit": head,
        "gitignore_changed": gitignore_changed,
        "config": store.config(),
    }
