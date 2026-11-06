"""Launcher for Barnraise's hooks (stdlib only; logic in server/src/barnraise/hooks.py)."""

import sys
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036 - the user's python3 may be older
    sys.exit(0)  # too old to run Barnraise; never block the user's tool call

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server" / "src"))

from barnraise.hooks import main  # noqa: E402

main()
