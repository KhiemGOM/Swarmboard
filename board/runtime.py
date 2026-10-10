"""Start or connect to the single local daemon without opening a console window."""
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
