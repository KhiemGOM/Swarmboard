"""Ensure the configured shared hub is running; do not create an agent session."""
import argparse
from pathlib import Path
from board.runtime import ensure_hub

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--workspace", default=str(Path.cwd()), help="Workspace to coordinate; defaults to your current directory")
parser.add_argument("--database", help="State database; defaults to WORKSPACE/.board/hub.sqlite")
parser.add_argument("--port", type=int, default=4317, help="Hub port; use a distinct port for a second workspace")
args = parser.parse_args()
workspace = Path(args.workspace).resolve()
database = Path(args.database).resolve() if args.database else workspace / ".board/hub.sqlite"
result = ensure_hub(f"http://127.0.0.1:{args.port}", workspace, database, autostart=True)
print("Agent Board is ready for", result["workspace"])
print("In Claude Code use /board; in Codex invoke the board skill with $board.")
