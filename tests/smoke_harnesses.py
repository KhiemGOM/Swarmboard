"""Opt-in live harness check: uses each signed-in account for one small turn."""
import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board.core import Hub
from board.server import serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", choices=["all", "codex", "claude"], default="all")
    args = parser.parse_args()
    entry = Path(__file__).resolve().parents[1] / "run_board.py"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        hub = Hub(workspace / "hub.sqlite", workspace)
        server = serve(hub, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        bridge = [str(entry), "mcp", "--url", url]
        config = workspace / "mcp.json"
        config.write_text(json.dumps({"mcpServers": {"board": {"command": sys.executable,
                          "args": bridge + ["--name", "claude-live-probe"]}}}), encoding="utf-8")
        prompt = "Compatibility probe only. Call the board MCP board_status tool exactly once. Report your_agent_id and workspace from its result. Do not run shell commands, edit files, or use other tools."
        commands = {
            "Claude Code": [shutil.which("claude"), "--mcp-config", str(config), "--strict-mcp-config", "--tools", "",
                            "--allowedTools", "mcp__board__board_status", "--max-turns", "3", "-p", prompt],
            "Codex": [shutil.which("codex"), "-c", "mcp_servers.board.command=" + json.dumps(sys.executable),
                      "-c", "mcp_servers.board.args=" + json.dumps(bridge + ["--name", "codex-live-probe"]),
                      "-c", 'mcp_servers.board.tools.board_status.approval_mode="approve"',
                      "exec", "--skip-git-repo-check", "--ephemeral", prompt],
        }
        if args.harness != "all":
            selected = "Codex" if args.harness == "codex" else "Claude Code"
            commands = {selected: commands[selected]}
        def probe(item):
            name, command = item
            if command[0] is None:
                return {"harness": name, "error": "Command not installed"}
            try:
                result = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=90,
                                        encoding="utf-8", errors="replace")
                with hub.connect() as db:
                    label = "claude-live-probe" if name == "Claude Code" else "codex-live-probe"
                    row = db.execute("SELECT id,started,seen FROM agents WHERE name=?", (label,)).fetchone()
                return {"harness": name, "exit_code": result.returncode,
                        "registered": row is not None,
                        "hub_tool_called": bool(row and row["seen"] > row["started"]),
                        "output": result.stdout[-3000:], "diagnostics": result.stderr[-3000:]}
            except subprocess.TimeoutExpired:
                return {"harness": name, "error": "Timed out after 90 seconds"}
        try:
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                for result in pool.map(probe, commands.items()):
                    print(json.dumps(result), flush=True)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    main()
