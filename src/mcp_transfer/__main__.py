import os
import sys
from pathlib import Path

# pythonw.exe has no inherited console streams. Establish them before importing
# the MCP SDK, whose subprocess helpers capture sys.stderr in default arguments.
if sys.stderr is None:
    from .config import default_home

    home = default_home()
    if "--home" in sys.argv:
        index = sys.argv.index("--home")
        if index + 1 < len(sys.argv):
            home = Path(sys.argv[index + 1])
    home.mkdir(parents=True, exist_ok=True)
    sys.stderr = open(home / "agent.log", "a", encoding="utf-8", buffering=1)
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")

from .cli import main  # noqa: E402

main()
