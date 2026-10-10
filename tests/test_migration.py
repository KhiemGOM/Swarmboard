import tempfile
import unittest
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from board.core import Hub, BoardError
from migrate_noticeboard import migrate


class MigrationTests(unittest.TestCase):
    def test_previous_schema_upgrade_preserves_sessions_and_active_claims(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            database = root / "hub.sqlite"
            with closing(sqlite3.connect(database)) as db, db:
                db.executescript("CREATE TABLE claims (id INTEGER PRIMARY KEY, agent TEXT, path TEXT, reason TEXT, started REAL, expires REAL, active INTEGER); CREATE TABLE agents (id TEXT PRIMARY KEY,name TEXT,token TEXT,state TEXT,started REAL,seen REAL,reason TEXT);")
                db.execute("INSERT INTO agents VALUES ('old','old','token','active',0,0,NULL)")
                db.execute("INSERT INTO claims VALUES (1,'old','a.py','work',0,?,1)", (time.time() + 1000,))
            status = Hub(database, root).call("board_status", {"agent_id": "old", "session_token": "token"})
            self.assertEqual(status["claims"][0]["kind"], "file")
            self.assertEqual(status["custody"][0]["owner"], "old")

    def test_tail_is_imported_once_and_full_source_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "old-board.md"
            content = "\n".join(f"notice {i}" for i in range(50))
            source.write_text(content, encoding="utf-8")
            hub = Hub(root / "hub.sqlite", root)
            first = migrate(hub, source, root / "archive")
            second = migrate(hub, source, root / "archive", retire=True)
            self.assertEqual(first["source_lines"], 30)
            self.assertEqual(first["post_ids"], second["post_ids"])
            self.assertFalse(source.exists())
            self.assertEqual(Path(first["archive"]).read_text(), content)
            with hub.connect() as db:
                posts = db.execute("SELECT body FROM posts ORDER BY id").fetchall()
                self.assertEqual(len(posts), 30)
                self.assertTrue(posts[0][0].endswith("notice 20"))
                self.assertTrue(posts[-1][0].endswith("notice 49"))
                self.assertEqual(db.execute("SELECT state FROM agents").fetchone()[0], "retired")

    def test_escape_source_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "outside.md"
            source.write_text("history")
            with self.assertRaises(BoardError):
                migrate(Hub(root / "hub.sqlite", root / "workspace"), source, root / "archive")
