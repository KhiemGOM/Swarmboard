"""Command hook entry point for Codex and Claude Code."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from board.runtime import workspace_connection

from board.core import BoardError
from board.hooks import Adapter, context, deny


def hook_connection(payload, workspace=None, database=None, url=None):
    """Route global hooks using native payload cwd, never the launcher checkout."""
    if not isinstance(payload, dict):
        raise BoardError('Hook payload must be an object')
    if workspace:
        return workspace_connection(workspace, database, url)
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise BoardError('Automatic hook routing requires the native payload absolute cwd')
    directory = Path(cwd).resolve()
    # Native cwd may be a subfolder; select its nearest configured project.
    for candidate in (directory, *directory.parents):
        if any((candidate / config).is_file() for config in ('.codex/board-operator.json', '.board/operator.json')):
            directory = candidate
            break
    return workspace_connection(directory, database, url)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', help='Optional fixed workspace for legacy scoped hooks')
    parser.add_argument('--database')
    parser.add_argument('--harness', choices=['codex', 'claude'], required=True)
    parser.add_argument('--url')
    parser.add_argument('--watch-inbox', action='store_true')
    parser.add_argument('--watch-seconds', type=float, default=3600)
    args = parser.parse_args()
    payload = {}
    try:
        payload = json.load(sys.stdin)
        args.workspace, args.database, args.url = hook_connection(payload, args.workspace, args.database, args.url)
        if not Path(args.database).is_file():
            # Global hooks do not create/register boards in unconfigured projects.
            print(json.dumps({}))
            return
        if args.watch_inbox:
            if args.harness != 'claude' or not 0 < args.watch_seconds <= 86400:
                raise ValueError('Inbox wake watcher requires Claude and a bounded positive lifetime')
            from board.inbox_watch import watch_inbox
            try:
                message = watch_inbox(args.workspace, args.database, payload, seconds=args.watch_seconds)
            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                # A watcher failure must not masquerade as a new inbox event.
                return
            if message:
                print(message, file=sys.stderr)
                raise SystemExit(2)  # Claude asyncRewake's documented wake signal.
            return
        result = Adapter(args.workspace, args.database, args.harness, args.url).handle(payload)
    except (BoardError, OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error) as exc:
        message = 'Swarmboard hook unavailable: ' + str(exc) + '. Reconnect MCP and check coordination before writes.'
        result = deny(message) if payload.get('hook_event_name') == 'PreToolUse' else context(payload.get('hook_event_name'), message)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
