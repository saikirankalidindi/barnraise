"""M0 spike MCP server: the worker handshake stubs (claim_task / complete_task) used by
spikes/run_spikes.py. Not part of the plugin; the harness adds it to a temporary copy."""

import json
import os
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(name="spike")
STATE = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()) / ".barnraise" / "m0-state.json"


def _load() -> dict[str, Any]:
    return json.loads(STATE.read_text()) if STATE.exists() else {"tasks": {}}


def _save(data: dict[str, Any]) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STATE)


@mcp.tool()
def claim_task(key: str, token: str) -> dict[str, Any]:
    """Start working on a task. Call this first, with the key and token from your brief."""
    data = _load()
    task = data["tasks"].get(key)
    if task is None or task.get("token") != token:
        return {"ok": False, "error": f"unknown task {key} or wrong token"}
    task.update(status="in_progress", claimed_at=time.time())
    _save(data)
    return {"ok": True, "worktree": task.get("worktree")}


@mcp.tool()
def complete_task(key: str, summary: str) -> dict[str, Any]:
    """Finish a task. Call this last, with a one-paragraph summary of what you did."""
    data = _load()
    task = data["tasks"].get(key)
    if task is None or task.get("status") != "in_progress":
        return {"ok": False, "error": f"task {key} is not in progress; call claim_task first"}
    task.update(status="done", summary=summary, completed_at=time.time())
    _save(data)
    return {"ok": True}


if __name__ == "__main__":
    mcp.run("stdio")
