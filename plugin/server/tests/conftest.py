import subprocess
from pathlib import Path
from typing import Any

import pytest

from barnraise.plan import Plan


def sample(**overrides: Any) -> dict[str, Any]:
    """A valid task-manager plan: T0 foundation, API and UI in parallel, e2e last."""
    data: dict[str, Any] = {
        "prompt": "A task manager with a REST API and a web UI",
        "checks": ["pnpm test"],
        "contracts": [
            {"name": "db-schema", "kind": "schema", "description": "Tables for tasks"},
            {"name": "tasks-api", "kind": "openapi", "description": "CRUD for /tasks"},
        ],
        "tasks": [
            {
                "key": "T0",
                "title": "Scaffold and database",
                "description": "Set up the workspace and the tasks table",
                "acceptance": ["pnpm test db"],
                "owns": ["package.json", "pnpm-lock.yaml", "src/db/**"],
                "produces": ["db-schema"],
                "size": "S",
            },
            {
                "key": "T1",
                "title": "Tasks API",
                "description": "CRUD endpoints",
                "acceptance": ["pnpm test api"],
                "owns": ["src/api/**"],
                "consumes": ["db-schema"],
                "produces": ["tasks-api"],
            },
            {
                "key": "T2",
                "title": "Tasks UI",
                "description": "List and edit tasks",
                "acceptance": ["pnpm test ui"],
                "owns": ["src/ui/**"],
                "consumes": ["tasks-api"],
                "size": "L",
            },
            {
                "key": "T3",
                "title": "End-to-end tests",
                "description": "Browser tests",
                "acceptance": ["pnpm e2e"],
                "owns": ["e2e/**"],
                "after": ["T1", "T2"],
                "size": "S",
            },
        ],
    }
    data.update(overrides)
    return data


def plan(**overrides: Any) -> Plan:
    return Plan.model_validate(sample(**overrides))


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    (root / "README.md").write_text("hi\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root.resolve()
