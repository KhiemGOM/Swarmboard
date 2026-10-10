"""Absolute-path entry point usable from any harness working directory."""
import sys

if len(sys.argv) < 2 or sys.argv[1] not in ("server", "mcp", "cli"):
    sys.exit("Usage: python run_board.py {server|mcp|cli} [options]")
mode = sys.argv.pop(1)
if mode == "server":
    from board.server import main
elif mode == "mcp":
    from board.mcp import main
else:
    from board.cli import main
main()
