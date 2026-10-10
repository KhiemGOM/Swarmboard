"""Merge Swarmboard command hooks into both harnesses, preserving other settings."""
import argparse
import json
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def ours(handler):
    return 'swarmboard_hook.py' in handler.get('command', '') or any('swarmboard_hook.py' in str(arg) for arg in handler.get('args', []))


def install(path, command, arguments=None):
    saved = json.loads(path.read_text(encoding='utf-8-sig')) if path.exists() else {}
    hooks = saved.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('Existing hooks must be an object')
    # Only replace handlers using our exact entry point; preserve unrelated hooks.
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise ValueError('Existing hook event must contain a list')
        kept = []
        for entry in entries:
            copy = dict(entry)
            copy['hooks'] = [h for h in entry.get('hooks', []) if not ours(h)]
            if copy['hooks']:
                kept.append(copy)
        hooks[event] = kept
    for event, matcher in [('SessionStart', None), ('PreToolUse', 'Edit|Write|MultiEdit|NotebookEdit|apply_patch|Bash|exec_command'),
                           ('PostToolUse', 'mcp__board__.*'), ('Stop', None), ('SessionEnd', None)]:
        entry = {'hooks': [{'type': 'command', 'command': command, 'timeout': 3 if event == 'SessionEnd' else 10}]}
        if arguments is not None:
            entry['hooks'][0]['args'] = arguments
        if matcher:
            entry['matcher'] = matcher
        hooks.setdefault(event, []).append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + '.swarmboard-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak'))
    path.write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--url', default='http://127.0.0.1:4317')
    parser.add_argument('--codex-config', type=Path, default=Path.home() / '.codex/hooks.json')
    parser.add_argument('--claude-config', type=Path, default=Path.home() / '.claude/settings.json')
    args = parser.parse_args()
    for harness, config in [('codex', args.codex_config), ('claude', args.claude_config)]:
        argv = [sys.executable, str(Path(__file__).resolve().with_name('swarmboard_hook.py')), '--harness', harness,
                '--workspace', str(Path(args.workspace).resolve()), '--database', str(Path(args.database).resolve()), '--url', args.url]
        command = subprocess.list2cmdline(argv) if sys.platform == 'win32' else shlex.join(argv)
        install(config, argv[0] if harness == 'claude' else command, argv[1:] if harness == 'claude' else None)
        print(f'Installed {harness} Swarmboard hooks: {config}')


if __name__ == '__main__':
    main()
