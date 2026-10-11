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
    return running


def workspace_connection(workspace=None, database=None, url=None):
    """Use the nearest configured project for automatic launches; explicit roots stay exact."""
    expected = Path(workspace or Path.cwd()).resolve()
    if not expected.is_dir():
        raise BoardError("Workspace must be an existing directory")
    if workspace is None:
        for directory in (expected, *expected.parents):
            if any((directory / name).is_file() for name in
                   ('.codex/board-operator.json', '.board/operator.json')):
                expected = directory
                break
    saved = {}
    for config in (expected / '.codex/board-operator.json', expected / '.board/operator.json'):
        if config.is_file():
            try:
                saved = json.loads(config.read_text(encoding='utf-8'))
                if not isinstance(saved, dict) or not saved.get('workspace'):
                    raise ValueError('Missing workspace')
                if Path(saved['workspace']).resolve() != expected:
                    raise BoardError("Local board configuration belongs to a different workspace; refusing connection")
            except (ValueError, TypeError) as exc:
                raise BoardError("Invalid local board configuration") from exc
            break
    location = database or saved.get('database') or expected / '.board/hub.sqlite'
    location = Path(location)
    if not location.is_absolute():
        location = expected / location
    location = location.resolve()
    # Stable project-specific fallback; an occupied port still fails closed in ensure_hub.
    fingerprint = hashlib.sha256(os.path.normcase(str(expected)).encode('utf-8')).digest()
    port = saved.get('port', 20000 + int.from_bytes(fingerprint[:4], 'big') % 30000)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise BoardError("Invalid local board port")
    if location.exists():
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(f'{location.as_uri()}?mode=ro', uri=True)) as db:
            row = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
        if not row or Path(row[0]).resolve() != expected:
            raise BoardError("Database belongs to a different workspace; refusing connection")
    return str(expected), str(location), url or f'http://127.0.0.1:{port}'
