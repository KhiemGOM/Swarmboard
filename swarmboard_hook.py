"""Command hook entry point for Codex and Claude Code."""
import argparse
import json
import sys

from board.core import BoardError
from board.hooks import Adapter, context, deny


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--database', required=True)
    parser.add_argument('--harness', choices=['codex', 'claude'], required=True)
    parser.add_argument('--url', default='http://127.0.0.1:4317')
    args = parser.parse_args()
    payload = {}
    try:
        payload = json.load(sys.stdin)
        result = Adapter(args.workspace, args.database, args.harness, args.url).handle(payload)
    except (BoardError, OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        message = 'Swarmboard hook unavailable: ' + str(exc) + '. Reconnect MCP and check coordination before writes.'
        result = deny(message) if payload.get('hook_event_name') == 'PreToolUse' else context(payload.get('hook_event_name'), message)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
