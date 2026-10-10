"""Read-only tool checks through both saved MCP registrations."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from board.version import VERSION
from board.runtime import ensure_hub
from board.core import BoardError


def main():
    codex = json.loads(subprocess.run([shutil.which("codex"), "mcp", "get", "board", "--json"],
                       check=True, capture_output=True, text=True).stdout)["transport"]
    claude = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))["mcpServers"]["board"]
    ids = []
    desktop_path = Path.home() / "AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude_desktop_config.json"
    connections = [("codex", codex), ("claude-code", claude)]
    if desktop_path.exists():
        desktop = json.loads(desktop_path.read_text(encoding="utf-8"))["mcpServers"].get("board")
        if desktop:
            connections.append(("claude-desktop", desktop))
    for name, config in connections:
        args = list(config["args"])
        args[args.index("--name") + 1] = name + "-connection-check"
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "installed-check", "version": "1"}}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "board_status", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "inbox_read", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "board_status", "arguments": {"target_agent_id": "self", "include_rules": False}}},
            {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "board_status", "arguments": {"resources": ["git:index"], "include_rules": False}}},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "board_info", "arguments": {}}},
        ]
        result = subprocess.run([config["command"], *args], input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, timeout=25, cwd=Path.home())
        result.check_returncode()
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        assert len(replies) == 7, result.stderr
        assert all("result" in reply for reply in replies), replies
        assert len(replies[1]["result"]["tools"]) == 24
        assert {'board_coordination', 'patch_queue', 'board_wait'} <= {tool['name'] for tool in replies[1]['result']['tools']}
        coordination = next(t for t in replies[1]['result']['tools'] if t['name'] == 'board_coordination')
        assert set(coordination['inputSchema']['properties']['action']['enum']) == {'request_relax', 'relax', 'report'}
        info = json.loads(replies[6]['result']['content'][0]['text'])
        assert not replies[6]['result']['isError'] and info['version_match'] and info['version'] == VERSION, info
        assert info['max_response_chars'] == 24000
        assert {'claim_relaxation', 'queue_only_policy', 'coordination_incidents'} <= set(info['features'])
        assert isinstance(info['queue_only_files'], list)
        properties = next(t for t in replies[1]['result']['tools'] if t['name'] == 'board_status')['inputSchema']['properties']
        assert {'target_agent_id', 'paths', 'resources', 'include_rules'} <= properties.keys()
        status = json.loads(replies[2]["result"]["content"][0]["text"])
        inbox = json.loads(replies[3]["result"]["content"][0]["text"])
        if replies[2]['result']['isError']:
            assert 'RESPONSE_TOO_LARGE' in status['error'], status
            assert len(replies[2]['result']['content'][0]['text']) < 24000
            status = json.loads(replies[4]['result']['content'][0]['text'])
        expected = Path(args[args.index("--workspace") + 1]).resolve()
        assert Path(status["workspace"]).resolve() == expected
        assert not replies[3]["result"]["isError"]
        focused = json.loads(replies[4]['result']['content'][0]['text'])
        entity = json.loads(replies[5]['result']['content'][0]['text'])
        assert not replies[4]['result']['isError'], focused
        assert [a['id'] for a in focused['agents']] == [status['your_agent_id']]
        assert 'coordination_rules' not in focused
        assert not replies[5]['result']['isError'], entity
        assert entity['matches'][0]['query'] == 'git:index'
        assert all(c['kind'] == 'resource' and c['path'] == 'git:index' for c in entity['claims'])
        ids.append(status["your_agent_id"])
        print(json.dumps({"harness": name, "agent_id": ids[-1], "tools": 24, 'version': info['version'], "workspace": status["workspace"],
                          "workspace_status_chars": len(replies[2]['result']['content'][0]['text']),
                          "self_status_chars": len(replies[4]['result']['content'][0]['text']), 'entity_query': 'passed'}))
    assert len(set(ids)) == len(ids)
    try:
        url = args[args.index("--url") + 1] if "--url" in args else "http://127.0.0.1:4317"
        ensure_hub(url, Path(status["workspace"]) / "different-workspace", None)
    except BoardError as exc:
        assert "different workspace" in str(exc)
    else:
        raise AssertionError("Wrong-workspace connection accepted")
    print("Saved connections, unique IDs, automatic startup and wrong-workspace refusal verified.")


if __name__ == "__main__":
    main()
