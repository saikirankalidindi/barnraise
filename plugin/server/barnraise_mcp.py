"""Start Barnraise's MCP server: `python3 ${CLAUDE_PLUGIN_ROOT}/server/barnraise_mcp.py`.

Standard library only; the code is in src/barnraise/ (PyYAML is vendored as source).
"""

import sys
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036 - the user's python3 may be older
    sys.exit("Barnraise needs Python 3.10 or newer (python3 is " + sys.version.split()[0] + ")")

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from barnraise.server import main

main()
