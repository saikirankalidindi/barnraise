"""Running acceptance checks and project checks (PRD B-20, B-21)."""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

CHECK_TIMEOUT_S = float(os.environ.get("BARNRAISE_CHECK_TIMEOUT_S", "1200"))
TAIL_LINES = 120
TAIL_CHARS = 8000


@dataclass(frozen=True)
class CheckResult:
    command: str
    ok: bool
    exit_code: int | None
    duration_s: float
    output: str  # tail of combined stdout and stderr


def tail(text: str) -> str:
    lines = text.splitlines()[-TAIL_LINES:]
    out = "\n".join(lines)
    return out[-TAIL_CHARS:]


def run(commands: list[str], cwd: Path, *, stop_on_failure: bool = True) -> list[CheckResult]:
    """Run commands in order with a shell, by default stopping at the first failure."""
    results = []
    for cmd in commands:
        started = time.monotonic()
        try:
            # The command inherits the environment as usual; Barnraise never reads or copies
            # it. CI=1 keeps test runners non-interactive.
            res = subprocess.run(
                f"export CI=1 BARNRAISE=1\n{cmd}",
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=CHECK_TIMEOUT_S,
                start_new_session=True,
            )
            code: int | None = res.returncode
            output = tail((res.stdout or "") + (res.stderr or ""))
        except subprocess.TimeoutExpired as exc:
            code = None
            raw = exc.stdout or b""
            text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
            output = tail(text) + f"\n[timed out after {CHECK_TIMEOUT_S:.0f}s]"
        results.append(
            CheckResult(cmd, code == 0, code, round(time.monotonic() - started, 2), output)
        )
        if code != 0 and stop_on_failure:
            break
    return results


def project_state(setup: list[str], commands: list[str], cwd: Path) -> tuple[dict[str, bool], str]:
    """Run setup (stopping if it fails), then every project check. Returns which checks
    pass and a printable summary."""
    prepared = run(setup, cwd) if setup else []
    if prepared and not all(r.ok for r in prepared):
        return {c: False for c in commands}, summary(prepared)
    results = run(commands, cwd, stop_on_failure=False)
    return {r.command: r.ok for r in results}, summary(prepared + results)


def summary(results: list[CheckResult]) -> str:
    lines = []
    for r in results:
        status = (
            "passed"
            if r.ok
            else ("timed out" if r.exit_code is None else f"failed ({r.exit_code})")
        )
        lines.append(f"$ {r.command}  -> {status} in {r.duration_s}s")
        if not r.ok and r.output:
            lines.append(r.output)
    return "\n".join(lines)
