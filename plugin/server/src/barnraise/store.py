"""Project state in <repo>/.barnraise/state.db (PRD 4.2).

Plans are stored whole, one row per version, because they are proposed and synced
whole. Runtime state is per task. The database is shared with the hook scripts
(barnraise.hooks), which run in their own processes, so every connection uses WAL and
a busy timeout, and read-modify-write sequences use `tx()` (BEGIN IMMEDIATE).
"""

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from barnraise.plan import Plan

STATE_DIR = ".barnraise"

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_version (
    version INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    source TEXT NOT NULL,          -- planner | sync
    plan TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    t REAL NOT NULL,
    type TEXT NOT NULL,
    payload TEXT NOT NULL
);
-- Runtime state of each task of the approved plan (M2).
-- status: pending | assigned | in_progress | blocked | done | failed | cancelled
CREATE TABLE IF NOT EXISTS task_state (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    conflicts INTEGER NOT NULL DEFAULT 0,
    branch TEXT,
    worktree TEXT,
    token TEXT,
    agent_id TEXT,
    assigned_at REAL,
    claimed_at REAL,
    heartbeat REAL,
    finished_at REAL,
    summary TEXT,
    last_failure TEXT,
    merge_commit TEXT,
    blocker TEXT,
    stop_blocks INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS contract_version (
    name TEXT NOT NULL,
    version INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    producer TEXT NOT NULL,
    published_at REAL NOT NULL,
    PRIMARY KEY (name, version)
);
CREATE TABLE IF NOT EXISTS message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_task TEXT NOT NULL,       -- a task key, "user", or "barnraise"
    kind TEXT NOT NULL,            -- question | notice | answer | contract | control
    body TEXT NOT NULL,
    created_at REAL NOT NULL
);
-- One row per recipient: a task's inbox.
CREATE TABLE IF NOT EXISTS delivery (
    message_id INTEGER NOT NULL,
    task TEXT NOT NULL,
    delivered_at REAL,
    PRIMARY KEY (message_id, task)
);
-- Project check results per integration commit and check set (regression baseline).
CREATE TABLE IF NOT EXISTS check_cache (
    id TEXT PRIMARY KEY,           -- <commit sha>:<hash of setup + checks>
    results TEXT NOT NULL,         -- {command: passed}
    summary TEXT NOT NULL
);
-- Estimated spend per worker subagent, from its transcript (written by the hooks).
CREATE TABLE IF NOT EXISTS agent_spend (
    agent_id TEXT PRIMARY KEY,
    task TEXT,
    usd REAL NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    updated_at REAL NOT NULL
);
"""


def project_dir() -> Path:
    """The project root Claude Code passes to plugin servers and hooks."""
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()


def connect(db_file: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_file, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / STATE_DIR
        self.plan_file = self.dir / "plan.yaml"
        self.db_file = self.dir / "state.db"
        self.events_file = self.dir / "events.jsonl"
        self.worktrees = self.dir / "worktrees"

    @property
    def initialized(self) -> bool:
        return self.db_file.exists()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """A write transaction that other processes (the hooks) cannot interleave with."""
        self.dir.mkdir(parents=True, exist_ok=True)
        conn = connect(self.db_file)
        try:
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_file)
        try:
            conn.executescript(SCHEMA)
            yield conn
        finally:
            conn.close()

    # --- Config ---------------------------------------------------------------

    def config(self) -> dict[str, Any]:
        if not self.initialized:
            return {}
        with self.read() as c:
            return {r[0]: json.loads(r[1]) for r in c.execute("SELECT key, value FROM config")}

    def set_config(self, conn: sqlite3.Connection | None = None, **values: Any) -> None:
        rows = [(k, json.dumps(v)) for k, v in values.items()]
        sql = (
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        if conn is not None:
            conn.executemany(sql, rows)
            return
        with self.tx() as c:
            c.executemany(sql, rows)

    # --- Plans ----------------------------------------------------------------

    def save_plan(self, plan: Plan, source: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO plan_version (created_at, source, plan) VALUES (?, ?, ?)",
                (time.time(), source, plan.to_json()),
            )
            version = int(cur.lastrowid or 0)
            self.log("plan.saved", conn=c, version=version, source=source, tasks=len(plan.tasks))
        return version

    def plan(self, version: int) -> Plan | None:
        with self.read() as c:
            row = c.execute(
                "SELECT plan FROM plan_version WHERE version = ?", (version,)
            ).fetchone()
        return Plan.from_json(row[0]) if row else None

    def latest_plan(self) -> tuple[int, Plan] | None:
        if not self.initialized:
            return None
        with self.read() as c:
            row = c.execute(
                "SELECT version, plan FROM plan_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
        return (int(row[0]), Plan.from_json(row[1])) if row else None

    def approved_plan(self) -> tuple[int, Plan] | None:
        version = self.config().get("approved_version")
        if version is None:
            return None
        plan = self.plan(int(version))
        return (int(version), plan) if plan else None

    # --- Tasks ----------------------------------------------------------------

    def tasks(self, conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
        if conn is not None:
            return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM task_state")}
        if not self.initialized:
            return {}
        with self.read() as c:
            return {r["key"]: dict(r) for r in c.execute("SELECT * FROM task_state")}

    def update_task(self, conn: sqlite3.Connection, key: str, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE task_state SET {cols} WHERE key = ?", (*fields.values(), key))

    # --- Contracts ------------------------------------------------------------

    def contracts(self, conn: sqlite3.Connection | None = None) -> dict[str, dict[str, Any]]:
        """Latest version of every published contract."""
        sql = (
            "SELECT c.* FROM contract_version c JOIN (SELECT name, MAX(version) v FROM "
            "contract_version GROUP BY name) m ON c.name = m.name AND c.version = m.v"
        )
        if conn is not None:
            return {r["name"]: dict(r) for r in conn.execute(sql)}
        if not self.initialized:
            return {}
        with self.read() as c:
            return {r["name"]: dict(r) for r in c.execute(sql)}

    # --- Spend ----------------------------------------------------------------

    def spent_usd(self) -> float:
        if not self.initialized:
            return 0.0
        with self.read() as c:
            row = c.execute("SELECT COALESCE(SUM(usd), 0) FROM agent_spend").fetchone()
        return float(row[0])

    def spend_by_task(self) -> dict[str, float]:
        if not self.initialized:
            return {}
        with self.read() as c:
            rows = c.execute(
                "SELECT task, SUM(usd) FROM agent_spend WHERE task IS NOT NULL GROUP BY task"
            )
            return {r[0]: float(r[1]) for r in rows}

    # --- Events (PRD B-25) ----------------------------------------------------

    def log(self, type_: str, conn: sqlite3.Connection | None = None, **payload: Any) -> int:
        """Append an event to state.db and to events.jsonl (for Agent Canvas and tooling)."""
        t = time.time()
        if conn is None:
            with self.tx() as c:
                return self.log(type_, conn=c, **payload)
        cur = conn.execute(
            "INSERT INTO event (t, type, payload) VALUES (?, ?, ?)",
            (t, type_, json.dumps(payload)),
        )
        seq = int(cur.lastrowid or 0)
        with self.events_file.open("a") as f:
            f.write(json.dumps({"seq": seq, "t": round(t, 3), "type": type_, **payload}) + "\n")
        return seq

    def events(self, type_: str | None = None) -> list[dict[str, Any]]:
        if not self.initialized:
            return []
        query = "SELECT seq, t, type, payload FROM event"
        args: tuple[str, ...] = ()
        if type_:
            query += " WHERE type = ?"
            args = (type_,)
        with self.read() as c:
            rows = c.execute(query + " ORDER BY seq", args).fetchall()
        return [{"seq": s, "t": t, "type": ty, **json.loads(p)} for s, t, ty, p in rows]

    def head_seq(self) -> int:
        if not self.initialized:
            return 0
        with self.read() as c:
            return int(c.execute("SELECT COALESCE(MAX(seq), 0) FROM event").fetchone()[0])
