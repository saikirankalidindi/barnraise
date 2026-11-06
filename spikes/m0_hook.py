"""M0 spike hook (stdlib only; installed only by spikes/run_spikes.py). Logs every event and implements the behaviour the
spikes test: claim mapping (S-2), path confinement (S-1), stop gate (S-3), and
message delivery (S-4). Replaced by the real hooks in M2/M3.
"""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
DIR = ROOT / ".barnraise"
MAX_STOP_BLOCKS = 3


def read_json(path: Path, default: dict) -> dict:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def log(entry: dict) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    with (DIR / "m0-hooks.jsonl").open("a") as f:
        f.write(json.dumps(entry) + "\n")


def respond(out: dict) -> None:
    log({"t": time.time(), "response": out})
    print(json.dumps(out))


def main() -> None:
    data = json.load(sys.stdin)
    event = data.get("hook_event_name")
    agent = data.get("agent_id")
    tool = data.get("tool_name", "")
    args = data.get("tool_input") or {}
    log(
        {
            "t": time.time(),
            "event": event,
            "agent_id": agent,
            "agent_type": data.get("agent_type"),
            "tool": tool,
            "tool_use_id": data.get("tool_use_id"),
            "input": {k: str(v)[:200] for k, v in args.items()} if isinstance(args, dict) else None,
            "cwd": data.get("cwd"),
            "keys": sorted(data),
        }
    )
    agents = read_json(DIR / "m0-agents.json", {})
    tasks = read_json(DIR / "m0-state.json", {"tasks": {}})["tasks"]
    mine = agents.get(agent or "")

    if event == "PreToolUse":
        if tool.endswith("__claim_task") and agent:
            agents[agent] = {"task": args.get("key"), "stop_blocks": 0}
            write_json(DIR / "m0-agents.json", agents)
        elif tool in ("Write", "Edit") and mine:
            worktree = tasks.get(mine["task"], {}).get("worktree")
            target = Path(str(args.get("file_path", ""))).resolve()
            if worktree and not target.is_relative_to(Path(worktree).resolve()):
                respond(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": f"Barnraise: {target} is outside your worktree {worktree}",
                        }
                    }
                )
    elif event == "PostToolUse" and mine:
        inbox = DIR / "m0-inbox" / f"{mine['task']}.txt"
        if inbox.exists():
            text = inbox.read_text()
            inbox.rename(inbox.with_suffix(".delivered"))
            respond(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": f"Barnraise message for {mine['task']}: {text}",
                    }
                }
            )
    elif event == "SubagentStop" and mine:
        done = tasks.get(mine["task"], {}).get("status") == "done"
        if not done and mine["stop_blocks"] < MAX_STOP_BLOCKS:
            mine["stop_blocks"] += 1
            write_json(DIR / "m0-agents.json", agents)
            respond(
                {
                    "decision": "block",
                    "reason": f"Barnraise: task {mine['task']} is not complete. Call the barnraise "
                    "complete_task tool with your summary now, then stop.",
                }
            )


if __name__ == "__main__":
    main()
