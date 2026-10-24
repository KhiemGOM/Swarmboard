"""Claude Desktop must not define `board`: it would run one bridge shared by every local Code chat."""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

NAME = 'claude_desktop_config.json'


def desktop_configs(home=None):
    home = Path(home or Path.home())
    candidates = [home / 'AppData/Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude' / NAME,
                  home / 'AppData/Roaming/Claude' / NAME,
                  home / 'Library/Application Support/Claude' / NAME,
                  home / '.config/Claude' / NAME,
                  *sorted((home / 'ClaudeProfiles').glob('*/' + NAME))]  # Extra --user-data-dir profiles.
    return [path for path in candidates if path.is_file()]


def duplicates(home=None):
    found = []
    for path in desktop_configs(home):
        try:
            servers = json.loads(path.read_text(encoding='utf-8')).get('mcpServers')
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(servers, dict) and 'board' in servers:
            found.append(path)
    return found


def strip_board(path):
    """Back up, then remove only the board server; other servers and settings stay."""
    path = Path(path)
    saved = json.loads(path.read_text(encoding='utf-8'))
    if 'board' not in saved.get('mcpServers', {}):
        return False
    shutil.copy2(path, path.with_name(path.name + '.board-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')))
    del saved['mcpServers']['board']
    temporary = path.with_name(path.name + '.board-install.tmp')
    temporary.write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)
    return True


def warning(paths):
    return ('Claude Desktop also defines the board MCP server in ' + ', '.join(str(p) for p in paths) +
            '. Desktop runs that copy once and shares it between all chats. Remove it (python install_local.py does) '
            'and restart Claude Desktop.')
