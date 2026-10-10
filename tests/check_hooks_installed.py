"""Exercise saved hook commands with native-shaped payloads; no model turn or file edits."""
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    home = Path.home()
    claude_mcp = json.loads((home / '.claude.json').read_text(encoding='utf-8'))['mcpServers']['board']
    codex_mcp = json.loads(subprocess.run(['codex', 'mcp', 'get', 'board', '--json'], capture_output=True, text=True, check=True).stdout)['transport']
    for harness, bridge, config in [('codex', codex_mcp, home / '.codex/hooks.json'),
                                     ('claude', claude_mcp, home / '.claude/settings.json')]:
        hooks = json.loads(config.read_text(encoding='utf-8'))['hooks']
        args = list(bridge['args'])
        args[args.index('--name') + 1] = harness + '-hook-payload-probe'
        workspace = args[args.index('--workspace') + 1]
        process = subprocess.Popen([bridge['command'], *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def rpc(method, params):
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            reply = json.loads(process.stdout.readline())
            assert 'result' in reply, reply
            return reply['result']
        def event(name, **fields):
            handler = next(h for entry in hooks[name] for h in entry['hooks'] if 'swarmboard_hook.py' in h['command'] or any('swarmboard_hook.py' in str(arg) for arg in h.get('args', [])))
            if 'args' in handler:
                command = [handler['command'], *handler['args']]
            elif sys.platform == 'win32':
                command = [shutil.which('pwsh') or 'powershell.exe', '-NoProfile', '-NonInteractive', '-Command', handler.get('commandWindows', handler['command'])]
            else:
                command = ['/bin/sh', '-c', handler['command']]
            result = subprocess.run(command,
                                    input=json.dumps({'session_id': harness + '-hook-payload-probe', 'cwd': workspace, 'hook_event_name': name, **fields}),
                                    capture_output=True, text=True, timeout=15, check=True)
            return json.loads(result.stdout)
        try:
            rpc('initialize', {'protocolVersion': '2025-11-25'})
            status = rpc('tools/call', {'name': 'board_status', 'arguments': {}})
            assert not status['isError'], status
            event('SessionStart')
            bound = event('PostToolUse', tool_name='mcp__board__board_status', tool_response=status)
            assert 'linked' in bound['hookSpecificOutput']['additionalContext'], bound
            guarded = event('PreToolUse', tool_name='Write', tool_input={'file_path': str(Path(workspace) / '__swarmboard_unclaimed_probe__.txt')})
            assert guarded['hookSpecificOutput']['permissionDecision'] == 'deny', guarded
            guarded = event('PreToolUse', tool_name='Bash', tool_input={'command': 'git commit -m hook-probe'})
            assert guarded['hookSpecificOutput']['permissionDecision'] == 'deny', guarded
            event('Stop')
            event('SessionEnd')
            print(json.dumps({'harness': harness, 'saved_hook_payloads': 'passed', 'unclaimed_edit': 'denied', 'unclaimed_git_index': 'denied', 'files_edited': False,
                              'hook_launcher': 'direct argv' if harness == 'claude' else 'shell',
                              'native_harness_dispatch': 'not exercised; review changed Codex definitions in /hooks'}))
        finally:
            process.stdin.close()
            process.wait(timeout=10)
            process.stdout.close()
            process.stderr.close()


if __name__ == '__main__':
    main()
