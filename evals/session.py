"""Drive a real Claude Code session with the Barnraise plugin loaded (used by the evals).

Spends plan usage. Sessions run with bypassPermissions in throwaway repos under /tmp;
Barnraise's own hooks still apply to workers.
"""

import asyncio
import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
DEFAULTS_REPLY = (
    "Use sensible defaults for anything open and do not ask me more questions. Proceed."
)


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def new_repo(prefix: str, files: dict[str, str] | None = None) -> Path:
    repo = Path(tempfile.mkdtemp(prefix=f"bp-{prefix}-"))
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.email", "eval@example.com", cwd=repo)
    sh("git", "config", "user.name", "eval", cwd=repo)
    for rel, text in {"README.md": f"# {prefix}\n", **(files or {})}.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "initial commit", cwd=repo)
    return repo


@dataclass
class Transcript:
    texts: list[str] = field(default_factory=list)
    tools: list[tuple[float, str, dict[str, Any]]] = field(default_factory=list)
    cost_usd: float = 0.0
    turns: int = 0
    plugin_errors: Any = None


async def run_session(
    repo: Path,
    prompts: list[str],
    *,
    model: str = "sonnet",
    timeout_s: float = 3600,
    reply: Callable[[str], str | None] = lambda text: DEFAULTS_REPLY,
    max_replies: int = 3,
    verbose: bool = True,
) -> Transcript:
    """Send each prompt in turn, answering questions the session asks with `reply`."""
    t = Transcript()
    t0 = time.monotonic()
    opts = ClaudeAgentOptions(
        cli_path=shutil.which("claude"),
        model=model,
        cwd=str(repo),
        plugins=[{"type": "local", "path": str(PLUGIN)}],
        setting_sources=[],
        permission_mode="bypassPermissions",
        max_turns=400,
    )

    def log(msg: str) -> None:
        if verbose:
            print(f"[{time.monotonic() - t0:7.1f}s] {msg}", flush=True)

    async with ClaudeSDKClient(options=opts) as client:
        for prompt in prompts:
            log(f"USER: {prompt[:200]}")
            await client.query(prompt)
            replies = 0
            while True:
                last_text = ""
                async for msg in client.receive_response():
                    if isinstance(msg, SystemMessage) and msg.subtype == "init":
                        t.plugin_errors = msg.data.get("plugin_errors")
                        if t.plugin_errors:
                            log(f"PLUGIN ERRORS: {t.plugin_errors}")
                    elif isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                        for b in msg.content:
                            if isinstance(b, TextBlock) and b.text.strip():
                                t.texts.append(b.text)
                                last_text = b.text
                                log(f"CLAUDE: {b.text[:600]}")
                            elif isinstance(b, ToolUseBlock):
                                t.tools.append((time.monotonic() - t0, b.name, dict(b.input)))
                                brief = {k: str(v)[:80] for k, v in b.input.items() if k != "prompt"}
                                log(f"TOOL {b.name} {json.dumps(brief)[:300]}")
                    elif isinstance(msg, ResultMessage):
                        t.cost_usd += msg.total_cost_usd or 0.0
                        t.turns += 1
                        log(f"turn ended ({msg.subtype}); session cost so far ${t.cost_usd:.3f}")
                if time.monotonic() - t0 > timeout_s:
                    log("TIMEOUT")
                    return t
                # The turn ended. If Claude asked something, answer and keep going.
                answer = reply(last_text) if last_text.rstrip().endswith("?") else None
                if answer is None or replies >= max_replies:
                    break
                replies += 1
                log(f"USER (auto-reply): {answer}")
                await client.query(answer)
    return t


def run(coro: Any) -> Any:
    return asyncio.run(coro)
