"""Install only the board entry; preserve and back up other user connections."""
import json
import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, help="Shared directory to coordinate")
    parser.add_argument("--database", help="Persistent state outside the shared directory")
    parser.add_argument("--port", type=int, default=4317)
    parser.add_argument("--desktop-config", type=Path, help="Optional Claude Desktop config path")
    options = parser.parse_args()
    workspace = Path(options.workspace).resolve()
    if not workspace.is_dir():
        parser.error("Workspace must be an existing directory")
    root = Path(__file__).resolve().parent
    user = Path.home()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    codex_config = user / ".codex/config.toml"
    claude_config = user / ".claude.json"
    for config in (codex_config, claude_config):
        if config is not None and config.exists():
            backup = config.with_name(config.name + ".board-backup-" + stamp)
            shutil.copy2(config, backup)
            print("Backup:", backup)
    python = str(Path(sys.executable).resolve())
    key = hashlib.sha256(os.path.normcase(str(workspace)).encode()).hexdigest()[:16]
    database = Path(options.database).resolve() if options.database else root / ".board/workspaces" / key / "hub.sqlite"
    # All global connections select locally; project routes live below the workspace.
    code_args = [str(root / "run_board.py"), "mcp", "--autostart"]
    # Supported harness commands preserve unrelated server entries.
    codex = shutil.which("codex")
    claude = shutil.which("claude")
    if not codex or not claude:
        raise SystemExit("Both Codex and Claude Code must be installed")
    existing = codex_config.read_text(encoding="utf-8") if codex_config.exists() else ""
    if re.search(r"(?m)^\[mcp_servers\.board\]", existing):
        subprocess.run([codex, "mcp", "remove", "board"], check=True, capture_output=True)
    subprocess.run([codex, "mcp", "add", "board", "--", python, str(root / "run_board.py"), "mcp", "--autostart", "--name", "codex"],
                   check=True, capture_output=True)
    text = codex_config.read_text(encoding="utf-8")
    pattern = r"(?m)^\[mcp_servers\.board\]\r?$"
    text, count = re.subn(pattern, '[mcp_servers.board]\nstartup_timeout_sec = 15\ntool_timeout_sec = 15\ndefault_tools_approval_mode = "approve"', text)
    if count != 1:
        raise SystemExit("Expected exactly one board stanza; backups preserved")
    temporary = codex_config.with_name("config.toml.board-install.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, codex_config)
    claude_data = json.loads(claude_config.read_text(encoding="utf-8")) if claude_config.exists() else {}
    if "board" in claude_data.get("mcpServers", {}):
        subprocess.run([claude, "mcp", "remove", "--scope", "user", "board"], check=True, capture_output=True)
    subprocess.run([claude, "mcp", "add", "--scope", "user", "--transport", "stdio", "board", "--",
                    python, *code_args, "--name", "claude-code"], check=True, capture_output=True)
    subprocess.run([codex, "mcp", "get", "board", "--json"], check=True, capture_output=True)
    installed = json.loads(claude_config.read_text(encoding="utf-8"))["mcpServers"]["board"]
    if installed["command"] != python or installed["args"] != [*code_args, "--name", "claude-code"]:
        raise SystemExit("Claude registration verification failed")
    # Desktop runs its own board definition once and shares it between every local
    # Code chat, overriding the per-chat registration. Remove it from every profile.
    from board.desktop_config import duplicates, strip_board
    for desktop_config in [options.desktop_config] if options.desktop_config else duplicates():
        if desktop_config.exists() and strip_board(desktop_config):
            print("Removed duplicate board server (backup kept):", desktop_config)
    print("Installed board for Codex and user-level Claude Code (including Desktop local Code).")
    from board.runtime import ROUTE
    operator_config = workspace / ROUTE
    operator_config.parent.mkdir(parents=True, exist_ok=True)
    operator_config.write_text(json.dumps({'workspace': str(workspace), 'database': str(database), 'port': options.port}, indent=2), encoding='utf-8')


if __name__ == "__main__":
    main()
