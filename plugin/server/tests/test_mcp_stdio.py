"""The MCP server over real stdio, started the way Claude Code starts it."""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ENTRY = Path(__file__).resolve().parents[1] / "barnraise_mcp.py"


class Client:
    def __init__(self, project: Path) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(ENTRY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env={"CLAUDE_PROJECT_DIR": str(project), "PATH": "/usr/bin:/bin"},
        )
        self.next_id = 0

    def send(self, method: str, params: dict[str, Any] | None = None, notify: bool = False) -> int:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next_id += 1
            msg["id"] = self.next_id
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        return self.next_id

    def read(self) -> dict[str, Any]:
        assert self.proc.stdout is not None
        reply: dict[str, Any] = json.loads(self.proc.stdout.readline())
        return reply

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        self.send("tools/call", {"name": tool, "arguments": arguments})
        result = self.read()["result"]
        return json.loads(result["content"][0]["text"])

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def client(repo: Path) -> Any:
    c = Client(repo)
    yield c
    c.close()


def test_handshake_and_tools(client: Client, repo: Path) -> None:
    client.send(
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
    )
    init = client.read()["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["serverInfo"]["name"] == "barnraise" and "tools" in init["capabilities"]
    client.send("notifications/initialized", notify=True)  # no reply expected

    client.send("tools/list")
    tools = {t["name"]: t for t in client.read()["result"]["tools"]}
    assert len(tools) == 18
    plan_schema = tools["propose_plan"]["inputSchema"]["properties"]["plan"]
    assert plan_schema["properties"]["tasks"]["items"]["properties"]["key"]["pattern"] == r"^T\d+$"
    assert tools["control"]["inputSchema"]["properties"]["action"]["enum"] == [
        "pause",
        "resume",
        "cancel",
    ]
    assert tools["init_project"]["inputSchema"]["required"] == ["checks"]

    status = client.call("status", {})
    assert status["initialized"] is False and status["project_dir"] == str(repo)
    assert client.call("init_project", {"checks": ["true"]})["ok"] is True

    # A plan with the wrong shape comes back as listed errors, not a crash.
    bad = client.call(
        "propose_plan", {"plan": {"prompt": "x", "checks": [], "tasks": [{"key": "one"}]}}
    )
    assert bad["ok"] is False
    messages = {e["message"] for e in bad["errors"]}
    assert "'one' is not a valid key" in messages and "field required" in messages

    client.send("ping")
    assert client.read()["result"] == {}
    client.send("no/such/method")
    assert client.read()["error"]["code"] == -32601


def test_a_long_wait_does_not_block_other_calls(client: Client) -> None:
    client.send("initialize", {"protocolVersion": "2025-06-18"})
    client.read()
    client.call("init_project", {"checks": []})
    started = time.monotonic()
    wait_id = client.send("tools/call", {"name": "wait_for_change", "arguments": {"timeout_s": 3}})
    status_id = client.send("tools/call", {"name": "status", "arguments": {}})
    first = client.read()
    assert first["id"] == status_id and time.monotonic() - started < 2  # answered while waiting
    second = client.read()
    assert second["id"] == wait_id
