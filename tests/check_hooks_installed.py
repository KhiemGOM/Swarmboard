"""Exercise saved hook commands with native-shaped payloads; no model turn or file edits."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from install_hooks import ours


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
        def rpc(method, params, connection=None):
            connection = connection or process
            connection.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
            connection.stdin.flush()
            reply = json.loads(connection.stdout.readline())
            assert 'result' in reply, reply
            return reply['result']
        def event(name, watch=False, **fields):
            handler = next(h for entry in hooks[name] for h in entry['hooks'] if bool(h.get('asyncRewake')) == watch and ours(h))
            if 'args' in handler:
                command = [handler['command'], *handler['args']]
                if watch:
                    command += ['--watch-seconds', '.2']
            elif sys.platform == 'win32':
                command = [shutil.which('cmd.exe') or 'cmd.exe', '/e:ON', '/v:OFF', '/d', '/c', handler.get('commandWindows', handler['command'])]
            else:
                command = ['/bin/sh', '-c', handler['command']]
            result = subprocess.run(command,
                                    input=json.dumps({'session_id': harness + '-hook-payload-probe', 'cwd': workspace, 'hook_event_name': name, **fields}),
                                    capture_output=True, text=True, timeout=15, check=not watch)
            if watch:
                return result
            return json.loads(result.stdout)
        sender = None
        try:
            rpc('initialize', {'protocolVersion': '2025-11-25'})
            status = rpc('tools/call', {'name': 'board_status', 'arguments': {}})
            assert not status['isError'], status
            event('SessionStart')
            bound = event('PostToolUse', tool_name='mcp__board__board_status', tool_response=status)
            assert 'linked' in bound['hookSpecificOutput']['additionalContext'], bound
            guarded = event('PreToolUse', tool_name='Write', tool_input={'file_path': str(Path(workspace) / '__swarmboard_unclaimed_probe__.txt')})
            assert guarded['hookSpecificOutput']['permissionDecision'] == 'deny', guarded
            sender_args = list(args)
            sender_args[sender_args.index('--name') + 1] += '-sender'
            sender = subprocess.Popen([bridge['command'], *sender_args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            rpc('initialize', {'protocolVersion': '2025-11-25'}, sender)
            identity = json.loads(status['content'][0]['text'])['your_agent_id']
            sent = rpc('tools/call', {'name': 'inbox_send', 'arguments': {'recipient_agent_id': identity, 'body': 'Isolated inbox delivery probe; reply not required.'}}, sender)
            assert not sent['isError'], sent
            message_id = json.loads(sent['content'][0]['text'])['id']
            for name, fields in [('PostToolUse', {'tool_name': 'Read', 'tool_response': 'probe'}), ('UserPromptSubmit', {'prompt': 'Continue probe'})]:
                delivered = event(name, **fields)
                assert 'Isolated inbox delivery probe' in delivered['hookSpecificOutput']['additionalContext'], delivered
            pending = rpc('tools/call', {'name': 'board_status', 'arguments': {'target_agent_id': 'self', 'include_rules': False}})
            preview = json.loads(pending['content'][0]['text'])
            assert preview['unread_inbox'] == 1 and preview['inbox_preview'][0]['id'] == message_id, preview
            if harness == 'claude':
                wake = event('Stop', watch=True)
                assert wake.returncode == 2 and 'Isolated inbox delivery probe' in wake.stderr, wake.stderr
                repeated = event('Stop', watch=True)
                assert repeated.returncode == 0 and not repeated.stderr, repeated.stderr
            rpc('tools/call', {'name': 'inbox_ack', 'arguments': {'message_ids': [message_id]}})
            assert event('PostToolUse', tool_name='Read', tool_response='probe') == {}
            guarded = event('PreToolUse', tool_name='Bash', tool_input={'command': 'git commit -m hook-probe'})
            assert guarded['hookSpecificOutput']['permissionDecision'] == 'deny', guarded
            event('Stop')
            event('SessionEnd')
            print(json.dumps({'harness': harness, 'saved_hook_payloads': 'passed', 'unclaimed_edit': 'denied', 'unclaimed_git_index': 'denied', 'files_edited': False,
                              'hook_launcher': 'direct argv' if harness == 'claude' else 'CMD on Windows / shell on Unix', 'inbox_tool_and_prompt_delivery': 'passed', 'delivery_does_not_ack': 'passed',
                              'claude_wake_signal': 'passed' if harness == 'claude' else 'not installed',
                              'native_harness_dispatch': 'not exercised; reload/review changed hook definitions'}))
        finally:
            for connection in (sender, process):
                if connection:
                    connection.stdin.close()
                    connection.wait(timeout=10)
                    connection.stdout.close()
                    connection.stderr.close()


if __name__ == '__main__':
    main()
