"""Git operations for task worktrees and the integration branch (PRD B-13, B-21, B-22).

Barnraise never touches the user's checkout. Each task works in
.barnraise/worktrees/<key> on branch barnraise/<key>; merges happen in a dedicated
worktree of the integration branch, .barnraise/worktrees/_integration.
"""

import subprocess
from pathlib import Path

INTEGRATION_WORKTREE = "_integration"


class GitError(Exception):
    pass


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise GitError((res.stderr or res.stdout).strip() or f"git {' '.join(args)} failed")
    return res


def out(cwd: Path, *args: str) -> str:
    return git(cwd, *args).stdout.strip()


def branch_exists(root: Path, branch: str) -> bool:
    return (
        git(
            root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    )


def ensure_worktree(root: Path, path: Path, branch: str, base: str) -> bool:
    """Check out `branch` at `path`, creating the branch from `base` if needed. Returns
    True when the branch was created (a fresh start), False when it already existed."""
    git(root, "worktree", "prune")
    if (path / ".git").exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if branch_exists(root, branch):
        git(root, "worktree", "add", str(path), branch)
        return False
    git(root, "worktree", "add", "-b", branch, str(path), base)
    return True


def remove_worktree(root: Path, path: Path) -> None:
    if path.exists():
        git(root, "worktree", "remove", "--force", str(path), check=False)
    git(root, "worktree", "prune", check=False)


def dirty_files(wt: Path) -> list[str]:
    lines = out(wt, "status", "--porcelain", "--untracked-files=all").splitlines()
    return [line[3:] for line in lines if line.strip()]


def changed_files(wt: Path, base_branch: str) -> list[str]:
    """Files the task branch changed since it diverged from the integration branch."""
    base = out(wt, "merge-base", base_branch, "HEAD")
    names = out(wt, "diff", "--name-only", "--no-renames", f"{base}..HEAD")
    return [n for n in names.splitlines() if n]


def commits_ahead(wt: Path, base_branch: str) -> int:
    return int(out(wt, "rev-list", "--count", f"{base_branch}..HEAD") or 0)


def merge(int_wt: Path, branch: str, message: str) -> tuple[bool, str, list[str]]:
    """Merge `branch` into the integration worktree. Returns (ok, pre-merge sha,
    conflicted files). On conflict the merge is aborted and nothing changes."""
    pre = out(int_wt, "rev-parse", "HEAD")
    res = git(int_wt, "merge", "--no-ff", "-m", message, branch, check=False)
    if res.returncode == 0:
        return True, pre, []
    conflicts = out(int_wt, "diff", "--name-only", "--diff-filter=U").splitlines()
    git(int_wt, "merge", "--abort", check=False)
    git(int_wt, "reset", "--hard", pre, check=False)
    return False, pre, conflicts or [(res.stdout + res.stderr).strip()[-500:]]


def reset_to(int_wt: Path, sha: str) -> None:
    git(int_wt, "reset", "--hard", sha)
