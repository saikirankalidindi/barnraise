"""Launcher for Barnraise's hooks (stdlib only; logic in server/src/barnraise/hooks.py)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "src"))

from barnraise.hooks import main  # noqa: E402

main()
