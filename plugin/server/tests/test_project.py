import json
from pathlib import Path

import pytest

from barnraise.project import ProjectError, detect_checks, init_project
from tests.conftest import git


def test_init_creates_branch_gitignore_and_config(repo: Path) -> None:
    out = init_project(repo, ["pnpm test"])
    assert out["created_branch"] is True
    assert git(repo, "rev-parse", "barnraise/integration") == git(repo, "rev-parse", "HEAD")
    assert git(repo, "branch", "--show-current") == "main"  # the checkout is untouched
    gitignore = (repo / ".gitignore").read_text()
    assert ".barnraise/*" in gitignore and "!.barnraise/plan.yaml" in gitignore
    assert out["config"]["checks"] == ["pnpm test"] and out["config"]["agent_limit"] == 3

    again = init_project(repo, ["pnpm test", "pnpm lint"], agent_limit=5)
    assert again["created_branch"] is False and again["gitignore_changed"] is False
    assert again["config"]["agent_limit"] == 5
    assert (repo / ".gitignore").read_text() == gitignore


def test_init_refuses_bad_repos(tmp_path: Path, repo: Path) -> None:
    with pytest.raises(ProjectError, match="not a git repository"):
        init_project(tmp_path, [])
    (repo / "sub").mkdir()
    with pytest.raises(ProjectError, match="repository root"):
        init_project(repo / "sub", [])
    with pytest.raises(ProjectError, match="agent_limit"):
        init_project(repo, [], agent_limit=9)
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    with pytest.raises(ProjectError, match="no commits"):
        init_project(empty, [])


def test_detect_checks(tmp_path: Path) -> None:
    assert detect_checks(tmp_path) == []
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "lint": "x"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    assert detect_checks(tmp_path) == ["pnpm run lint", "pnpm test"]
    (tmp_path / "pyproject.toml").write_text('[dependency-groups]\ndev = ["pytest"]\n')
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "go.mod").write_text("module x\n")
    assert detect_checks(tmp_path)[2:] == ["uv run pytest", "go test ./..."]
