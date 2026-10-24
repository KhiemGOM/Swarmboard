"""Start or connect to the single local daemon without opening a console window."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

from .core import BoardError


def health(url):
    try:
        with build_opener(ProxyHandler({})).open(url.rstrip("/") + "/health", timeout=1) as response:
            return json.load(response)
    except (URLError, ValueError, OSError):
        return None


def ensure_hub(url, workspace, database, autostart=False):
    expected = Path(workspace).resolve()
    running = health(url)
    if running is None and autostart:
        address = urlparse(url)
        if address.scheme != "http" or address.hostname != "127.0.0.1" or address.path not in ("", "/"):
            raise BoardError("Autostart requires an http://127.0.0.1:PORT hub URL")
        if not database:
            raise BoardError("Autostart requires an explicit database path")
        database = Path(database).resolve()
        database.parent.mkdir(parents=True, exist_ok=True)
        entry = Path(__file__).resolve().parents[1] / "run_board.py"
        command = [sys.executable, str(entry), "server", "--workspace", str(expected),
                   "--database", str(database), "--port", str(address.port or 80)]
        options = {"stdin": subprocess.DEVNULL, "cwd": str(entry.parent)}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            options["start_new_session"] = True
        with (database.parent / "hub.log").open("ab") as log:
            subprocess.Popen(command, stdout=log, stderr=log, **options)
        for _ in range(40):
            time.sleep(0.1)
            running = health(url)
            if running:
                break
    if not running or running.get("service") != "agent-board":
        raise BoardError("Agent Board is unavailable or the port belongs to another service")
    if Path(running.get("workspace", "")).resolve() != expected:
        raise BoardError("The connected hub belongs to a different workspace; refusing registration")
    if database and Path(database).exists() and running.get('database_id'):
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(f'{Path(database).resolve().as_uri()}?mode=ro', uri=True)) as db:
            stored = db.execute("SELECT value FROM settings WHERE key='database_id'").fetchone()
        if not stored or stored[0] != running['database_id']:
            raise BoardError('The connected hub uses a different database for this workspace; verify MCP/hook database and port, refusing registration')
    return running


ROUTE = '.board/operator.json'
LEGACY_ROUTE = '.codex/board-operator.json'  # Read until /board migrates it.
HOOK_STATE_DAYS = 14


def workspace_required(cwd):
    cwd = Path(cwd).resolve()
    stale = [str(p) for d in (cwd, *cwd.parents) for p in stale_routes(d)]
    return BoardError(f'WORKSPACE_REQUIRED: no Swarmboard project contains {cwd}. Type /board (Codex: $board) in a chat '
                      'opened in the project, or call board_workspace with the absolute project directory and a reason.'
                      + (f' Ignored route file(s) naming a different workspace: {", ".join(stale)}.' if stale else ''))


def read_route(directory, legacy=True):
    """A route file is valid only in the directory it names; copies elsewhere are stale and ignored."""
    directory = Path(directory).resolve()
    for name in (ROUTE, LEGACY_ROUTE) if legacy else (ROUTE,):
        try:
            saved = json.loads((directory / name).read_text(encoding='utf-8'))
            if isinstance(saved, dict) and saved.get('workspace') and Path(saved['workspace']).resolve() == directory:
                return saved
        except (OSError, ValueError, TypeError):
            continue
    return None


def stale_routes(directory):
    directory = Path(directory).resolve()
    return [directory / name for name in (ROUTE, LEGACY_ROUTE) if (directory / name).is_file()] \
        if read_route(directory) is None else []


def find_project(cwd):
    """Nearest folder with a valid route; a stale copy in between never redirects the chat."""
    cwd = Path(cwd).resolve()
    return next((d for d in (cwd, *cwd.parents) if read_route(d)), None)


def default_port(workspace):
    # Stable project-specific port; an occupied port still fails closed in ensure_hub.
    fingerprint = hashlib.sha256(os.path.normcase(str(workspace)).encode('utf-8')).digest()
    return 20000 + int.from_bytes(fingerprint[:4], 'big') % 30000


def prepare_route(cwd):
    """/board: make the chat's project routable through one canonical file and drop stale routing state."""
    cwd = Path(cwd).resolve()
    root = find_project(cwd) or next((d for d in (cwd, *cwd.parents) if (d / '.git').exists()), cwd)
    saved = read_route(root) or {}
    database = Path(saved.get('database') or root / '.board/hub.sqlite')
    route = {'workspace': str(root), 'database': str(database if database.is_absolute() else root / database),
             'port': saved.get('port', default_port(root))}
    cleaned = []
    canonical, legacy = root / ROUTE, root / LEGACY_ROUTE
    try:
        previous = json.loads(canonical.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous = None
    if previous != route:
        if canonical.exists():
            cleaned.append(f'replaced stale {canonical}')
        canonical.parent.mkdir(parents=True, exist_ok=True)
        temporary = canonical.with_name(canonical.name + '.tmp')
        temporary.write_text(json.dumps(route, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, canonical)
    if legacy.exists():
        legacy.unlink()
        cleaned.append(f'removed legacy {legacy}')
    state = Path(route['database']).parent
    if (state / 'native-reconnect').exists():
        import shutil
        shutil.rmtree(state / 'native-reconnect', ignore_errors=True)
        cleaned.append('removed native-reconnect cache')
    cutoff = time.time() - HOOK_STATE_DAYS * 86400
    old = [p for p in (state / 'hook-sessions').glob('*.json') if p.stat().st_mtime < cutoff] \
        if (state / 'hook-sessions').is_dir() else []
    for path in old:
        path.unlink(missing_ok=True)
    if old:
        cleaned.append(f'removed {len(old)} old hook session file(s)')
    return (*workspace_connection(root), cleaned)


def workspace_connection(workspace=None, database=None, url=None):
    """Explicit roots stay exact; otherwise only a configured project above cwd, never cwd itself."""
    if workspace is None:
        expected = find_project(Path.cwd())
        if expected is None:
            raise workspace_required(Path.cwd())
    else:
        expected = Path(workspace).resolve()
    if not expected.is_dir():
        raise BoardError("Workspace must be an existing directory")
    if stale_routes(expected):
        raise BoardError(f'Local board configuration in {expected} belongs to a different workspace; refusing connection. '
                         'Type /board in a chat opened in this project to repair it.')
    saved = read_route(expected) or {}
    location = database or saved.get('database') or expected / '.board/hub.sqlite'
    location = Path(location)
    if not location.is_absolute():
        location = expected / location
    location = location.resolve()
    port = saved.get('port', default_port(expected))
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BoardError("Invalid local board port")
    if location.exists():
        import sqlite3
        from contextlib import closing
        try:
            with closing(sqlite3.connect(f'{location.as_uri()}?mode=ro', uri=True)) as db:
                row = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        except sqlite3.OperationalError as exc:
            if 'readonly' not in str(exc).lower():
                raise
            # SQLite must roll back a hot journal before a read-only SELECT can run.
            # mode=rw requires this exact existing file; never delete journals/create a replacement.
            with closing(sqlite3.connect(f'{location.as_uri()}?mode=rw', uri=True)) as db:
                row = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        if not row or Path(row[0]).resolve() != expected:
            raise BoardError("Database belongs to a different workspace; refusing connection")
    return str(expected), str(location), url or f'http://127.0.0.1:{port}'
