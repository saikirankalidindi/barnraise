"""Plugin hooks (PRD section 4): worker identity, confinement, delivery, stop gate, spend.

Runs as `python3 hooks/barnraise_hook.py` for every hook event in every session where
the plugin is enabled, so it is stdlib-only, exits fast when the project has no
Barnraise state, and never fails the user's tool call because of its own errors.

Workers are identified by the `agent_id` Claude Code puts on hook input (spike S-2):
the PreToolUse hook on claim_task records `agent_id -> task` when the token matches.
"""

import json
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from barnraise.globs import overlap

MAX_STOP_BLOCKS = 3
MAX_ATTEMPTS = 3
FILE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
SHARED_FILES = (
    "package.json", "package-lock.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "yarn.lock",
    "bun.lock", "bun.lockb", "pyproject.toml", "uv.lock", "poetry.lock", "requirements.txt",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "composer.json", "composer.lock",
)  # fmt: skip
# Estimated USD per million tokens (input, output), by model family. Override with the
# `prices` config key. Cache reads cost 10% of input, cache writes 125%.
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (5.0, 25.0)}
DEFAULT_PRICE = PRICES["opus"]
CD_RE = re.compile(r"(?:^|&&|;|\|\||\n)\s*(?:builtin\s+)?cd\s+(\"[^\"]*\"|'[^']*'|[^\s;&|]+)")
PUSH_RE = re.compile(r"\bgit\b[^\n;&|]*\bpush\b")


def connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def log_event(conn: sqlite3.Connection, root: Path, type_: str, **payload: Any) -> None:
    t = time.time()
    cur = conn.execute(
        "INSERT INTO event (t, type, payload) VALUES (?, ?, ?)", (t, type_, json.dumps(payload))
    )
    line = {"seq": cur.lastrowid, "t": round(t, 3), "type": type_, **payload}
    with (root / ".barnraise" / "events.jsonl").open("a") as f:
        f.write(json.dumps(line) + "\n")


def deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Barnraise: {reason}",
        }
    }


def owned_globs(conn: sqlite3.Connection, key: str) -> list[str] | None:
    row = conn.execute("SELECT value FROM config WHERE key = 'approved_version'").fetchone()
    if row is None:
        return None
    plan = conn.execute(
        "SELECT plan FROM plan_version WHERE version = ?", (json.loads(row[0]),)
    ).fetchone()
    if plan is None:
        return None
    for t in json.loads(plan[0])["tasks"]:
        if t["key"] == key:
            return list(t["owns"])
    return None


def inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def pre_tool_use(
    conn: sqlite3.Connection, root: Path, data: dict[str, Any]
) -> dict[str, Any] | None:
    agent = data.get("agent_id")
    tool = str(data.get("tool_name") or "")
    args = data.get("tool_input") or {}
    if tool.endswith("__claim_task"):
        if agent and isinstance(args, dict):
            conn.execute(
                "UPDATE task_state SET agent_id = ? WHERE key = ? AND token = ? AND token IS NOT NULL",
                (agent, args.get("key"), args.get("token")),
            )
        return None
    row = worker(conn, agent)
    if row is None or not row["worktree"]:
        return None
    wt = Path(row["worktree"])
    if tool in FILE_TOOLS and isinstance(args, dict):
        raw = str(args.get(FILE_TOOLS[tool]) or "")
        target = Path(raw) if Path(raw).is_absolute() else wt / raw
        if not inside(target, wt):
            return deny(f"{target} is outside your worktree {wt}. Work only inside it.")
        rel = target.resolve().relative_to(wt.resolve()).as_posix()
        owns = owned_globs(conn, row["key"])
        if owns is not None and not (
            any(overlap(g, rel) for g in owns) or any(overlap(f, rel) for f in SHARED_FILES)
        ):
            return deny(
                f"{rel} is not in the paths task {row['key']} owns ({', '.join(owns)}). Another "
                "task owns it; if it must change, tell that task with post_message."
            )
    if tool == "Bash" and isinstance(args, dict):
        command = str(args.get("command") or "")
        if PUSH_RE.search(command):
            return deny("workers never push. Commit locally; Barnraise merges your branch.")
        for m in CD_RE.finditer(command):
            target_raw = m.group(1).strip("\"'")
            if target_raw.startswith(("$", "~")) and target_raw != "~":
                continue  # variables cannot be resolved here
            target = Path(target_raw).expanduser()
            target = target if target.is_absolute() else wt / target
            if not inside(target, wt):
                return deny(f"`cd {target_raw}` leaves your worktree {wt}. Stay inside it.")
    return None


def worker(conn: sqlite3.Connection, agent: str | None) -> sqlite3.Row | None:
    if not agent:
        return None
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM task_state WHERE agent_id = ?", (agent,)
    ).fetchone()
    return row


def post_tool_use(
    conn: sqlite3.Connection, root: Path, data: dict[str, Any]
) -> dict[str, Any] | None:
    row = worker(conn, data.get("agent_id"))
    if row is None:
        return None
    now = time.time()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE task_state SET heartbeat = ? WHERE key = ?", (now, row["key"]))
        msgs = conn.execute(
            "SELECT m.id, m.from_task, m.kind, m.body FROM delivery d JOIN message m "
            "ON m.id = d.message_id WHERE d.task = ? AND d.delivered_at IS NULL ORDER BY m.id",
            (row["key"],),
        ).fetchall()
        if msgs:
            conn.execute(
                "UPDATE delivery SET delivered_at = ? WHERE task = ? AND delivered_at IS NULL",
                (now, row["key"]),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if not msgs:
        return None
    text = "\n".join(f"- from {m[1]} ({m[2]}): {m[3]}" for m in msgs)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"Barnraise messages for task {row['key']}:\n{text}",
        }
    }


def transcript_spend(path: Path, prices: dict[str, Any]) -> tuple[float, int, int]:
    """Estimated USD and tokens from a subagent transcript (deduplicated per message)."""
    usage: dict[str, tuple[str, dict[str, Any]]] = {}
    try:
        lines = path.expanduser().read_text().splitlines()
    except OSError:
        return 0.0, 0, 0
    for i, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            continue
        usage[str(msg.get("id") or i)] = (str(msg.get("model") or ""), msg["usage"])
    usd = 0.0
    tin = tout = 0
    table = {**PRICES, **{k: tuple(v) for k, v in prices.items()}}
    for model, u in usage.values():
        pin, pout = next((p for fam, p in table.items() if fam in model), DEFAULT_PRICE)
        fresh = int(u.get("input_tokens") or 0)
        read = int(u.get("cache_read_input_tokens") or 0)
        write = int(u.get("cache_creation_input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        tin += fresh + read + write
        tout += out
        usd += (fresh * pin + read * pin * 0.1 + write * pin * 1.25 + out * pout) / 1_000_000
    return usd, tin, tout


def subagent_stop(
    conn: sqlite3.Connection, root: Path, data: dict[str, Any]
) -> dict[str, Any] | None:
    agent = data.get("agent_id")
    row = worker(conn, agent)
    if row is None:
        return None
    key = row["key"]
    transcript = data.get("agent_transcript_path")
    if transcript:
        prices_row = conn.execute("SELECT value FROM config WHERE key = 'prices'").fetchone()
        usd, tin, tout = transcript_spend(
            Path(transcript), json.loads(prices_row[0]) if prices_row else {}
        )
        conn.execute(
            "INSERT INTO agent_spend VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(agent_id) DO UPDATE "
            "SET usd = excluded.usd, input_tokens = excluded.input_tokens, "
            "output_tokens = excluded.output_tokens, updated_at = excluded.updated_at",
            (agent, key, usd, tin, tout, time.time()),
        )
    if row["status"] not in ("assigned", "in_progress"):
        return None  # done, failed, blocked, or cancelled: the worker may stop
    if row["stop_blocks"] < MAX_STOP_BLOCKS:
        conn.execute("UPDATE task_state SET stop_blocks = stop_blocks + 1 WHERE key = ?", (key,))
        return {
            "decision": "block",
            "reason": f"Barnraise: task {key} is not finished. Call complete_task with key {key}, "
            "your token, and a summary. If it reports a failure, fix it and call it again. If you "
            "cannot go on without the user, call report_blocker instead.",
        }
    # The worker insists on stopping: count an attempt and release (or fail) the task.
    attempts = int(row["attempts"]) + 1
    status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
    conn.execute(
        "UPDATE task_state SET status = ?, attempts = ?, token = NULL, agent_id = NULL, "
        "stop_blocks = 0, last_failure = ? WHERE key = ?",
        (status, attempts, "the worker stopped without completing the task", key),
    )
    log_event(conn, root, "task.failed" if status == "failed" else "task.released", task=key,
              reason="worker stopped without completing")  # fmt: skip
    return None


HANDLERS = {"PreToolUse": pre_tool_use, "PostToolUse": post_tool_use, "SubagentStop": subagent_stop}


def handle(data: dict[str, Any], root: Path) -> dict[str, Any] | None:
    handler = HANDLERS.get(str(data.get("hook_event_name")))
    db = root / ".barnraise" / "state.db"
    if handler is None or not db.exists():
        return None
    conn = connect(db)
    try:
        return handler(conn, root, data)
    finally:
        conn.close()


def main() -> None:
    import os

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    try:
        data = json.load(sys.stdin)
        out = handle(data, root)
    except Exception:
        # Never break the user's session because of Barnraise's own bug.
        try:
            with (root / ".barnraise" / "hook-errors.log").open("a") as f:
                f.write(f"{time.ctime()}\n{traceback.format_exc()}\n")
        except OSError:
            pass
        return
    if out is not None:
        print(json.dumps(out))
