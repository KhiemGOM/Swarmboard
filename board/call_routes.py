"""Per-call chat routes handed from the PreToolUse hook to the bridge, keyed by Claude's tool_use_id.

Claude Code's auto mode refuses calls whose input a hook rewrote, so the hook leaves the input alone
and records the route here; the bridge takes it using the _meta["claudecode/toolUseId"] that Claude
Code sends with every MCP tools/call.
"""
import json
import os
import re
import time
from pathlib import Path

TOOL_USE_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')
EXPIRY_SECONDS = 600  # A denied or abandoned call leaves its record behind.


def directory():
    return Path(os.environ.get('SWARMBOARD_CALLS_DIR') or Path.home() / '.swarmboard' / 'calls')


def path_for(tool_use_id):
    return directory() / (tool_use_id + '.json') if isinstance(tool_use_id, str) and TOOL_USE_ID.fullmatch(tool_use_id) else None


def record(tool_use_id, route):
    path = path_for(tool_use_id)
    if path is None:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - EXPIRY_SECONDS
    for old in path.parent.glob('*.json'):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(route), encoding='utf-8')
    os.replace(temporary, path)
    return True


def take(tool_use_id):
    path = path_for(tool_use_id)
    if path is None:
        return None
    try:
        fresh = path.stat().st_mtime >= time.time() - EXPIRY_SECONDS
        value = json.loads(path.read_text(encoding='utf-8'))
        path.unlink()
    except (OSError, ValueError):
        return None
    return value if fresh else None
