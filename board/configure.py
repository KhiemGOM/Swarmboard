"""Switch the saved Codex board connection without registering another agent."""
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .core import BoardError
from .runtime import health


def replace_setting(section, key, value):
    # Codex emits scalar command and single/multiline array args. Stop at the
    # next setting, retaining permission/time-out fields and unrelated servers.
    pattern = rf'(?ms)^{key}\s*=.*?(?=^[A-Za-z_][A-Za-z_0-9-]*\s*=|\Z)'
    matches = list(re.finditer(pattern, section))
    if len(matches) > 1:
        raise BoardError(f'Duplicate board {key} setting; refusing configuration')
    replacement = f'{key} = {json.dumps(value)}\n'
    if matches:
        return re.sub(pattern, lambda _: replacement, section, count=1)
    return section.rstrip() + '\n' + replacement


def connection_text(text, workspace, database, port, checkout):
    pattern = r'(?ms)^\[mcp_servers\.board\][^\n]*\n.*?(?=^\[|\Z)'
    sections = list(re.finditer(pattern, text))
    if len(sections) > 1:
        raise BoardError('Duplicate board configuration; refusing configuration')
    section = sections[0].group() if sections else '[mcp_servers.board]\n'
    args = [str(checkout / 'run_board.py'), 'mcp', '--workspace', str(workspace),
            '--database', str(database), '--url', f'http://127.0.0.1:{port}',
            '--autostart', '--name', 'codex']
    section = replace_setting(section, 'command', sys.executable)
    section = replace_setting(section, 'args', args)
    if sections:
        match = sections[0]
        return text[:match.start()] + section + '\n' + text[match.end():]
    return text.rstrip() + '\n\n' + section


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def configure(workspace, database, port, operator_config, checkout, mcp_config=None):
    workspace, database = Path(workspace).resolve(), Path(database).resolve()
    if not workspace.is_dir():
        raise BoardError('Workspace must be an existing directory')
    if not 1 <= port <= 65535:
        raise BoardError('Port must be between 1 and 65535')
    if database.exists():
        with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as db:
            row = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        if not row or Path(row[0]).resolve() != workspace:
            raise BoardError('Database belongs to a different workspace; refusing configuration')
    running = health(f'http://127.0.0.1:{port}')
    if running and (running.get('service') != 'agent-board' or
                    Path(running.get('workspace', '')).resolve() != workspace):
        raise BoardError('Port belongs to another workspace or service; choose --port')
    config = Path(mcp_config) if mcp_config else workspace / '.codex/config.toml'
    operator_config = Path(operator_config)
    if config.resolve() == operator_config.resolve():
        raise BoardError('MCP and operator configurations must be different files')
    original = config.read_text(encoding='utf-8') if config.exists() else None
    operator_original = operator_config.read_text(encoding='utf-8') if operator_config.exists() else None
    saved = json.loads(operator_original) if operator_original is not None else {}
    saved.update(workspace=str(workspace), database=str(database), port=port)
    updated = connection_text(original or '', workspace, database, port, Path(checkout).resolve())
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    backups = []
    for path in (config, operator_config):
        if path.exists():
            backup = path.with_name(path.name + '.board-backup-' + stamp)
            shutil.copy2(path, backup)
            backups.append(str(backup))
    changed = []
    try:
        for path, content, before in [(config, updated, original),
                                     (operator_config, json.dumps(saved, indent=2) + '\n', operator_original)]:
            atomic_write(path, content)
            changed.append((path, before))
    except OSError:
        for path, before in reversed(changed):
            if before is None:
                path.unlink()
            else:
                atomic_write(path, before)
        raise
    return {'workspace': str(workspace), 'database': str(database), 'port': port,
            'mcp_config': str(config), 'backups': backups,
            'message': 'Codex saved board connection updated. Reload MCP or restart Codex, then run $board. Existing sessions keep their current connection until reloaded.'}
