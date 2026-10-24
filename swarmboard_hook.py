"""Command hook entry point for Codex and Claude Code."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from board.runtime import find_project, prepare_route, workspace_connection

from board.core import BoardError
from board.hooks import Adapter, context, requests_board


def unavailable(payload, database, harness, reason=None):
    """An unavailable optional coordinator must not veto local tools."""
    session = payload.get('session_id')
    if not database or not isinstance(session, str) or not session:
        return {}
    key = hashlib.sha256((harness + ':' + session).encode()).hexdigest()
    marker = Path(database).parent / 'hook-sessions' / (key + '.offline.json')
    try:
        state_path = marker.with_name(key + '.json')
        state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if not state.get('activated') and not state.get('agent_id'):
            return {}  # Chats that never typed /board are plain repositories.
        if marker.exists():
            return {}
        Adapter.save(marker, {'offline': True, 'reason': str(reason or 'Unavailable')[:500]})
    except (OSError, ValueError):
        return {}  # No repeated warnings if even local hint storage is unavailable.
    return context(payload.get('hook_event_name'), 'Swarmboard disconnected; local work is allowed. Coordination checks resume when connected.')


def hook_connection(payload, workspace=None, database=None, url=None):
    """Route global hooks using native payload cwd, never the launcher checkout."""
    if not isinstance(payload, dict):
        raise BoardError('Hook payload must be an object')
    if workspace:
        return workspace_connection(workspace, database, url)
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise BoardError('Automatic hook routing requires the native payload absolute cwd')
    project = find_project(cwd)
    return workspace_connection(project) if project else None


def activation_notes(payload, harness, cleaned):
    notes = ['Swarmboard cleaned up: ' + '; '.join(cleaned) + '.'] if cleaned else []
    if harness == 'claude':
        from board.desktop_config import duplicates, warning
        found = duplicates()
        if found:
            notes.append(warning(found))
    return notes


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
        cleaned = []
        if requests_board(payload) and not args.workspace and isinstance(payload.get('cwd'), str)                 and Path(payload['cwd']).is_absolute():
            cleaned = prepare_route(payload['cwd'])[3]  # Only /board may create or repair a route.
        connection = hook_connection(payload, args.workspace, args.database, args.url)
        if connection is None:
            print(json.dumps({}))  # Not a Swarmboard project: a normal repository.
            return
        args.workspace, args.database, args.url = connection
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
        notes = activation_notes(payload, args.harness, cleaned) if requests_board(payload) else []
        if notes:
            event = payload.get('hook_event_name')
            existing = result.get('hookSpecificOutput', {}).get('additionalContext')
            result = {**result, **context(event, '\n'.join(([existing] if existing else []) + notes))}
        session = payload.get('session_id')
        if isinstance(session, str) and session:
            key = hashlib.sha256((args.harness + ':' + session).encode()).hexdigest()
            try:
                (Path(args.database).parent / 'hook-sessions' / (key + '.offline.json')).unlink(missing_ok=True)
            except OSError:
                pass
    except (BoardError, OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error) as exc:
        result = unavailable(payload, args.database, args.harness, exc)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
