"""M0 spikes S-1 to S-5 (PRD section 6). Drives a real Claude Code session with the
plugin loaded, in a throwaway git repo, and prints the evidence.

    uv run --project plugin/server --with claude-agent-sdk python spikes/run_spikes.py [A|B|C]

Spends plan usage (Haiku). The repo is created under /tmp and left for inspection.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
from claude_agent_sdk.types import AssistantMessage, ResultMessage, SystemMessage, TextBlock

SOURCE = Path(__file__).resolve().parents[1] / "plugin"
SPIKES = Path(__file__).resolve().parent
MODEL = "haiku"


def spike_plugin() -> Path:
    """A temporary copy of the plugin with the M0 spike hooks and stub tools added."""
    root = Path(tempfile.mkdtemp(prefix="barnraise-plugin-")) / "barnraise"
    shutil.copytree(SOURCE, root, ignore=shutil.ignore_patterns(".venv", ".*_cache", "__pycache__", ".git"))
    hook = f'python3 "{SPIKES / "m0_hook.py"}"'
    entry = [{"matcher": "*", "hooks": [{"type": "command", "command": hook}]}]
    (root / "hooks").mkdir(exist_ok=True)
    (root / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
        "PreToolUse": entry, "PostToolUse": entry,
        "SubagentStart": [{"hooks": entry[0]["hooks"]}], "SubagentStop": [{"hooks": entry[0]["hooks"]}],
    }}))
    mcp_config = json.loads((root / ".mcp.json").read_text())
    mcp_config["mcpServers"]["spike"] = {
        "command": "uv",
        "args": ["run", "--quiet", "--project", str(SOURCE / "server"), "python", str(SPIKES / "m0_server.py")],
    }
    (root / ".mcp.json").write_text(json.dumps(mcp_config))
    return root


PLUGIN = spike_plugin()


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="barnraise-spike-"))
    sh("git", "init", "-q", "-b", "main", cwd=repo)
    sh("git", "config", "user.email", "spike@example.com", cwd=repo)
    sh("git", "config", "user.name", "spike", cwd=repo)
    (repo / "README.md").write_text("spike repo\n")
    (repo / ".gitignore").write_text(".barnraise/\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "init", cwd=repo)
    # The integration branch is ahead of main, so we can tell which base a worktree used.
    sh("git", "checkout", "-q", "-b", "barnraise/integration", cwd=repo)
    (repo / "INTEGRATION.md").write_text("only on the integration branch\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "integration marker", cwd=repo)
    sh("git", "checkout", "-q", "main", cwd=repo)
    return repo


def add_task(repo: Path, key: str) -> Path:
    wt = repo / ".barnraise" / "worktrees" / key
    sh("git", "worktree", "add", "-q", "-b", f"barnraise/{key}", str(wt), "barnraise/integration", cwd=repo)
    state_file = repo / ".barnraise" / "m0-state.json"
    state = json.loads(state_file.read_text()) if state_file.exists() else {"tasks": {}}
    state["tasks"][key] = {"token": f"tok-{key}", "status": "ready", "worktree": str(wt)}
    state_file.write_text(json.dumps(state, indent=2))
    return wt


def hook_log(repo: Path) -> list[dict[str, Any]]:
    path = repo / ".barnraise" / "m0-hooks.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def options(repo: Path) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        cli_path=shutil.which("claude"),
        model=MODEL,
        cwd=str(repo),
        plugins=[{"type": "local", "path": str(PLUGIN)}],
        setting_sources=[],
        strict_mcp_config=False,
        # Throwaway repo under /tmp. Plugin hooks still run, and their denials apply.
        permission_mode="bypassPermissions",
        max_turns=60,
    )


async def drive(repo: Path, prompt: str, until: Any, timeout_s: float = 420.0) -> list[str]:
    """Send the driver prompt, then keep reading (background workers report back in
    later turns) until `until(hook_log)` is true or the timeout passes."""
    texts: list[str] = []
    t0 = time.monotonic()
    async with ClaudeSDKClient(options=options(repo)) as client:
        await client.query(prompt)

        async def read() -> None:
            async for msg in client.receive_messages():
                if isinstance(msg, SystemMessage) and msg.subtype == "init":
                    d = msg.data
                    print("init: plugins", [p.get("name") for p in d.get("plugins", [])],
                          "errors", d.get("plugin_errors"),
                          "mcp", [(s.get("name"), s.get("status")) for s in d.get("mcp_servers", [])],
                          "agents", [a for a in d.get("agents", []) if "barnraise" in a])
                elif isinstance(msg, AssistantMessage) and msg.parent_tool_use_id is None:
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            texts.append(b.text)
                            print(f"[{time.monotonic() - t0:5.1f}s] driver: {b.text[:300]}")
                elif isinstance(msg, ResultMessage):
                    print(f"[{time.monotonic() - t0:5.1f}s] turn ended ({msg.subtype}, cost ${msg.total_cost_usd or 0:.4f})")

        reader = asyncio.create_task(read())
        while time.monotonic() - t0 < timeout_s and not until(hook_log(repo)):
            await asyncio.sleep(1)
        await asyncio.sleep(3)  # let the final turn land
        reader.cancel()
        print(f"stopped after {time.monotonic() - t0:.0f}s; condition met: {until(hook_log(repo))}")
    return texts


def stops(log: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in log if e.get("event") == "SubagentStop"]


def summarise_agents(repo: Path) -> None:
    log = hook_log(repo)
    agents = json.loads((repo / ".barnraise" / "m0-agents.json").read_text()) if (repo / ".barnraise" / "m0-agents.json").exists() else {}
    print("\nclaim mapping (S-2):", json.dumps(agents))
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for e in log:
        if e.get("agent_id"):
            by_agent.setdefault(e["agent_id"], []).append(e)
    t0 = min((e["t"] for e in log), default=0)
    for agent, evs in by_agent.items():
        start = next((e["t"] - t0 for e in evs if e.get("event") == "SubagentStart"), None)
        stop = [round(e["t"] - t0, 1) for e in evs if e.get("event") == "SubagentStop"]
        tools = [e["tool"] for e in evs if e.get("event") == "PreToolUse"]
        print(f"  agent {agent[:12]} type={evs[0].get('agent_type')} start={start and round(start, 1)} stops={stop}")
        print(f"    tools: {tools}")
    responses = [e["response"] for e in log if "response" in e]
    print("hook responses:", json.dumps(responses, indent=1)[:2500])


async def scenario_a() -> None:
    """S-1 to S-5 with three workers in Barnraise-created worktrees."""
    repo = make_repo()
    wts = {k: add_task(repo, k) for k in ("T1", "T2", "T3")}
    print("repo:", repo)
    brief = {
        "T1": f"Task T1, token tok-T1, worktree {wts['T1']}. Create notes/t1.txt in the worktree "
        f"containing 'hello from T1', then commit it in the worktree (git add + git commit -m T1). "
        f"Then, as a guard test, try once to write the file {repo / 'OUTSIDE.txt'} with any content; "
        "if that is refused, note it and continue. Then run the bash command `sleep 15`. Then complete the task.",
        "T2": f"Task T2, token tok-T2, worktree {wts['T2']}. Create notes/t2.txt in the worktree containing "
        "'hello from T2' and commit it in the worktree. For this test, do NOT call complete_task yourself: "
        "just stop once the commit is made, unless Barnraise later tells you otherwise.",
        "T3": f"Task T3, token tok-T3, worktree {wts['T3']}. Run the bash command `sleep 8` three times, as three "
        "separate Bash tool calls. Then complete the task; in the summary, quote verbatim every message from "
        "Barnraise you received while working (or say 'no messages').",
    }
    prompt = (
        "Start three barnraise:worker subagents now, in the background and in parallel (all three in one "
        "message), each with exactly one of the briefs below as its prompt. Do not do any of the work "
        "yourself. When all three have finished, reply DONE followed by one line per worker with its result.\n\n"
        + "\n\n".join(f"Brief {k}:\n{v}" for k, v in brief.items())
    )

    async def deliver_message() -> None:
        # S-4: once T3 has claimed its task, drop a message into its inbox.
        agents_file = repo / ".barnraise" / "m0-agents.json"
        for _ in range(300):
            if agents_file.exists() and "T3" in agents_file.read_text():
                inbox = repo / ".barnraise" / "m0-inbox"
                inbox.mkdir(parents=True, exist_ok=True)
                (inbox / "T3.txt").write_text("contract api v2 published, codeword PINEAPPLE")
                print("delivered message to T3's inbox")
                return
            await asyncio.sleep(1)

    msg_task = asyncio.create_task(deliver_message())
    texts = await drive(repo, prompt, lambda log: len({e["agent_id"] for e in stops(log)}) >= 3 and _all_done(repo))
    msg_task.cancel()
    summarise_agents(repo)
    state = json.loads((repo / ".barnraise" / "m0-state.json").read_text())
    print("\ntask state:", json.dumps({k: (v["status"], v.get("summary", "")[:300]) for k, v in state["tasks"].items()}, indent=1))
    for k, wt in wts.items():
        print(f"{k}: branch commits {sh('git', 'log', '--oneline', 'barnraise/integration..HEAD', cwd=wt)!r}; "
              f"has INTEGRATION.md={(wt / 'INTEGRATION.md').exists()}; files={sorted(p.name for p in (wt / 'notes').glob('*')) if (wt / 'notes').exists() else []}")
    print("OUTSIDE.txt written in main checkout:", (repo / "OUTSIDE.txt").exists())
    print("main checkout status:", sh("git", "status", "--short", cwd=repo) or "clean")
    print("delivered inbox:", [p.name for p in (repo / ".barnraise" / "m0-inbox").glob("*")] if (repo / ".barnraise" / "m0-inbox").exists() else None)
    print("driver final:", texts[-1][:800] if texts else None)


def _all_done(repo: Path) -> bool:
    state = json.loads((repo / ".barnraise" / "m0-state.json").read_text())
    return all(t["status"] == "done" for t in state["tasks"].values())


async def scenario_b(n: int = 6) -> None:
    """S-5: how many background workers really run at once."""
    repo = make_repo()
    wts = {f"P{i}": add_task(repo, f"P{i}") for i in range(1, n + 1)}
    briefs = "\n\n".join(
        f"Brief {k}:\nTask {k}, token tok-{k}, worktree {wt}. Run the bash command `sleep 20` once, then complete the task."
        for k, wt in wts.items()
    )
    prompt = (
        f"Start {n} barnraise:worker subagents now, in the background and in parallel (all in one message), "
        "each with exactly one brief below as its prompt. Do not do the work yourself. Reply DONE when all have finished.\n\n"
        + briefs
    )
    await drive(repo, prompt, lambda log: _all_done(repo), timeout_s=300)
    log = hook_log(repo)
    t0 = min(e["t"] for e in log)
    sleeps = []
    for e in log:
        if e.get("event") == "PreToolUse" and e.get("tool") == "Bash" and "sleep 20" in str(e.get("input")):
            end = next((x["t"] for x in log if x.get("event") == "PostToolUse" and x.get("tool_use_id") == e["tool_use_id"]), None)
            sleeps.append((round(e["t"] - t0, 1), end and round(end - t0, 1)))
    print("sleep 20 windows (start, end):", sorted(sleeps))
    points = sorted([(s, 1) for s, _ in sleeps] + [(t, -1) for _, t in sleeps if t])
    peak = cur = 0
    for _, d in points:
        cur += d
        peak = max(peak, cur)
    print(f"peak concurrent sleeps: {peak} of {n}")
    starts = sorted(round(e["t"] - t0, 1) for e in log if e.get("event") == "SubagentStart")
    print("SubagentStart times:", starts)
    print("task state:", {k: v["status"] for k, v in json.loads((repo / ".barnraise" / "m0-state.json").read_text())["tasks"].items()})


async def scenario_c() -> None:
    """S-1 baseline: where does `isolation: worktree` branch from?"""
    repo = make_repo()
    sh("git", "checkout", "-q", "barnraise/integration", cwd=repo)  # session HEAD on integration
    prompt = (
        "Use the Agent tool to start one general-purpose subagent with isolation set to \"worktree\" (not in the "
        "background; wait for it). Its prompt: 'Run this bash command and report its output verbatim: "
        "pwd; git rev-parse --abbrev-ref HEAD; git log --oneline -3; ls'. Then reply with exactly what it reported."
    )
    texts = await drive(repo, prompt, lambda log: bool(stops(log)), timeout_s=180)
    print("driver final:", texts[-1][:1200] if texts else None)
    print("worktrees:", sh("git", "worktree", "list", cwd=repo))


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "A"
    asyncio.run({"A": scenario_a, "B": scenario_b, "C": scenario_c}[which]())
