"""Exercise coordination against real paths, without changing the target repo."""
import argparse
import concurrent.futures
import json
import subprocess
import sys
import threading
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board.core import Hub
from board.server import serve


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--path", required=True, help="Existing workspace-relative file for the isolated claim exercise")
    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()
    project = Path(__file__).resolve().parents[1]
    state = project / ".board" / "probes" / uuid.uuid4().hex
    state.mkdir(parents=True)
    git = subprocess.run(["git", "--no-optional-locks", "-C", str(workspace), "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                         capture_output=True, check=True)
    entries = git.stdout.decode("utf-8", errors="replace").split("\0")
    statuses = []
    migrations = []
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            index += 1  # porcelain -z includes a second path for rename/copy
        statuses.append(code)
        if code == "??" and path.startswith("db/migrations/"):
            migrations.append(path)
    notice = workspace / "NOTICEBOARD.md"
    notice_lines = notice.read_text(encoding="utf-8-sig").splitlines() if notice.exists() else []
    recent = []
    for line in notice_lines:
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 3 and parts[0].startswith("2026-"):
            recent.append({"label": parts[1], "status": parts[2]})
    hub = Hub(state / "hub.sqlite", workspace)
    server = serve(hub, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    bridges = [subprocess.Popen([sys.executable, str(project / "run_board.py"), "mcp", "--url", url, "--name", name],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
               for name in ["SIMULATED-Codex", "SIMULATED-Claude"]]
    def rpc(process, method, params=None):
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}) + "\n")
        process.stdin.flush()
        result = json.loads(process.stdout.readline())
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["result"]
    def tool(process, name, arguments=None):
        result = rpc(process, "tools/call", {"name": name, "arguments": arguments or {}})
        if result["isError"]:
            raise RuntimeError(result["content"])
        return json.loads(result["content"][0]["text"])
    report = {"workspace": str(workspace), "mode": "isolated simulated agents, real directory paths",
              "dirty_entries_at_snapshot": len(statuses), "git_status_counts": dict(Counter(statuses)),
              "untracked_migrations": migrations, "noticeboard_lines_at_snapshot": len(notice_lines),
              "recent_notice_statuses": recent[-12:],
              "noticeboard_attribution": "Historical labels only; not authenticated agent identities or authoritative live locks"}
    try:
        for bridge in bridges:
            rpc(bridge, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "workspace-probe", "version": "1"}})
        path = args.path
        if not (workspace / path).is_file():
            raise RuntimeError("Expected source path is absent")
        barrier = threading.Barrier(2)
        def compete(bridge):
            barrier.wait()
            return tool(bridge, "claim", {"paths": [path], "reason": "Isolated coordination probe; no real edit"})
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(compete, bridges))
        assert sum(r["accepted"] for r in results) == 1
        winner = 0 if results[0]["accepted"] else 1
        holder, successor = bridges[winner], bridges[1 - winner]
        holder_id = tool(holder, "board_status")["your_agent_id"]
        successor_id = tool(successor, "board_status")["your_agent_id"]
        conflict = tool(successor, "claim", {"paths": ["web/src/edit"], "reason": "Parent-folder conflict probe"})
        assert not conflict["accepted"]
        message = tool(holder, "inbox_send", {"recipient_agent_id": successor_id, "body": "Simulated handoff only; live swarm remains on NOTICEBOARD.md"})
        assert tool(successor, "noticeboard_read")["unread_inbox"] == 1
        assert tool(successor, "inbox_read")["unread_inbox"] == 1
        tool(successor, "takeover", {"target_agent_id": holder_id, "reason": "Continuing simulated agent work; simulated predecessor is dead"})
        rejected = rpc(holder, "tools/call", {"name": "board_status"})
        assert rejected["isError"]
        assert tool(successor, "inbox_ack", {"message_ids": [message["id"]]})["unread_inbox"] == 0
        tool(successor, "release", {"claim_ids": results[winner]["claim_ids"]})
        assert tool(successor, "board_status")["claims"] == []
        report["checks"] = {"simultaneous_claim_one_winner": True, "parent_folder_conflict_refused": True,
                            "inbox_reminder_and_ack": True, "takeover_revoked_predecessor": True,
                            "claims_released": True}
        report["target_changes"] = "No target source edits, noticeboard writes, Git mutations, builds, or application database access performed"
    finally:
        for bridge in bridges:
            bridge.stdin.close()
            try:
                bridge.wait(timeout=5)
            except subprocess.TimeoutExpired:
                bridge.kill()
                bridge.wait()
            bridge.stdout.close()
            bridge.stderr.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    output = state / "report.json"
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(output), **report}, indent=2))


if __name__ == "__main__":
    main()
