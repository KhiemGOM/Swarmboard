"""Command hook entry point for Codex and Claude Code."""
import argparse
import json
import sqlite3
import sys

from board.core import BoardError
from board.hooks import Adapter, context, deny


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--harness', choices=['codex', 'claude'], required=True)
    parser.add_argument('--url', default='http://127.0.0.1:4317')
    parser.add_argument('--watch-inbox', action='store_true')
    parser.add_argument('--watch-seconds', type=float, default=3600)
    args = parser.parse_args()
    payload = {}
    try:
        payload = json.load(sys.stdin)
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
    except (BoardError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        message = 'Swarmboard hook unavailable: ' + str(exc) + '. Reconnect MCP and check coordination before writes.'
        result = deny(message) if payload.get('hook_event_name') == 'PreToolUse' else context(payload.get('hook_event_name'), message)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
