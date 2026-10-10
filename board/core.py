import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from .rules import COORDINATION_RULES


class BoardError(Exception):
    pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Hub:
    def __init__(self, database, workspace):
        self.workspace = Path(workspace).resolve()
        self.database = str(database)
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY, name TEXT, token TEXT, state TEXT,
                    started REAL, seen REAL, reason TEXT);
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY, agent TEXT, path TEXT, reason TEXT,
                    started REAL, expires REAL, active INTEGER);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, sender TEXT, recipient TEXT,
                    body TEXT, created REAL, acknowledged REAL);
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY, agent TEXT, body TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY, kind TEXT, agent TEXT, payload TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS custody (
                    path TEXT PRIMARY KEY, owner TEXT, state TEXT, reason TEXT, commit_hash TEXT, updated REAL);
            """)
            if "kind" not in {row[1] for row in db.execute("PRAGMA table_info(claims)")}:
                db.execute("ALTER TABLE claims ADD COLUMN kind TEXT NOT NULL DEFAULT 'file'")
            for row in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file' ORDER BY started").fetchall():
                db.execute("INSERT OR IGNORE INTO custody VALUES (?,?,'ACTIVE',?,NULL,?)",
                           (row["path"], row["agent"], row["reason"], row["started"]))
            configured = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
            if configured and configured[0] != str(self.workspace):
                raise BoardError("This database belongs to a different workspace")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('workspace', ?)", (str(self.workspace),))

    def connect(self):
        db = sqlite3.connect(self.database, timeout=10, factory=ClosingConnection)
        db.row_factory = sqlite3.Row
        return db

    def path(self, value):
        if not isinstance(value, str) or not value.strip():
            raise BoardError("A non-empty file or folder path is required")
        if any(char in value for char in "*?["):
            raise BoardError("Use concrete files or folders; globs are not supported yet")
        candidate = (self.workspace / value).resolve()
        try:
            relative = candidate.relative_to(self.workspace)
        except ValueError:
            raise BoardError("Path must stay inside the registered workspace")
        return os.path.normcase(relative.as_posix()).replace("\\", "/") or "."

    @staticmethod
    def overlaps(a, b):
        return a == "." or b == "." or a == b or a.startswith(b + "/") or b.startswith(a + "/")

    @staticmethod
    def event(db, kind, agent, payload):
        db.execute("INSERT INTO events(kind,agent,payload,created) VALUES (?,?,?,?)",
                   (kind, agent, json.dumps(payload), time.time()))

    def call(self, method, params):
        with self.connect() as db:
            # Serializes claim-check + insertion and takeover + transfer.
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            expired = db.execute("SELECT id,agent,path,kind FROM claims WHERE active=1 AND expires<=?", (now,)).fetchall()
            for item in expired:
                self.event(db, "claim_expired", item["agent"], dict(item))
                if item["kind"] == "file":
                    db.execute("UPDATE custody SET state='HELD',reason='Lease expired; completion evidence still required',updated=? WHERE path=? AND owner=? AND state='ACTIVE'",
                               (now, item["path"], item["agent"]))
            db.execute("UPDATE claims SET active=0 WHERE active=1 AND expires<=?", (now,))
            if method == "register":
                name = params.get("name", "agent")
                if not isinstance(name, str) or not name.strip():
                    raise BoardError("Agent name is required")
                agent = secrets.token_hex(8)
                token = secrets.token_urlsafe(32)
                db.execute("INSERT INTO agents VALUES (?,?,?,'active',?,?,NULL)", (agent, name, token, now, now))
                self.event(db, "registered", agent, {"name": name})
                return {"agent_id": agent, "session_token": token, "workspace": str(self.workspace),
                        "instructions": COORDINATION_RULES}
            agent = params.get("agent_id")
            identity = db.execute("SELECT * FROM agents WHERE id=?", (agent,)).fetchone()
            if not identity or not secrets.compare_digest(identity["token"], str(params.get("session_token", ""))):
                raise BoardError("Unknown agent or invalid session token")
            if identity["state"] != "active":
                raise BoardError("Session retired. Register a new session and obtain fresh claims.")
            db.execute("UPDATE agents SET seen=? WHERE id=?", (now, agent))
            result = self.dispatch(db, method, params, agent, now)
            unread = db.execute("SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged IS NULL", (agent,)).fetchone()[0]
            result["unread_inbox"] = unread
            if unread:
                result["inbox_reminder"] = f"You have {unread} unread message(s). Call inbox_read, then inbox_ack after processing."
            held = db.execute("SELECT COUNT(*) FROM custody WHERE owner=? AND state='HELD'", (agent,)).fetchone()[0]
            if held:
                result["custody_reminder"] = f"You retain custody of {held} HELD path(s). Complete and record commit evidence with custody_resolve, or arrange an explicit takeover."
            return result

    def dispatch(self, db, method, p, agent, now):
        if method in ("heartbeat", "board_status"):
            agents = [dict(r) for r in db.execute("SELECT id,name,state,started,seen,reason FROM agents ORDER BY started")]
            claims = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 ORDER BY id")]
            custody = [dict(r) for r in db.execute("SELECT * FROM custody ORDER BY updated")]
            return {"workspace": str(self.workspace), "agents": agents, "claims": claims,
                    "custody": custody, "coordination_rules": COORDINATION_RULES}
        if method in ("claim", "mutex_claim", "release", "custody_resolve"):
            from .claims import dispatch
            return dispatch(self, db, method, p, agent, now)
        if method == "takeover":
            target = p.get("target_agent_id")
            reason = p.get("reason")
            if target == agent or not isinstance(reason, str) or not reason.strip():
                raise BoardError("Takeover needs a different target and an explicit reason")
            original = db.execute("SELECT state FROM agents WHERE id=?", (target,)).fetchone()
            if not original or original[0] != "active":
                raise BoardError("Target must be an active registered session")
            db.execute("UPDATE agents SET state='retired',reason=? WHERE id=?", (reason, target))
            # Transfer all live claims atomically; the original session loses access immediately.
            db.execute("UPDATE claims SET agent=?,expires=? WHERE agent=? AND active=1", (agent, now + 7200, target))
            db.execute("UPDATE custody SET owner=?,updated=? WHERE owner=? AND state!='COMMITTED'", (agent, now, target))
            self.event(db, "takeover", agent, {"target": target, "reason": reason})
            return {"retired_agent": target, "claims": [dict(r) for r in db.execute("SELECT * FROM claims WHERE agent=? AND active=1", (agent,))]}
        if method in ("noticeboard_post", "inbox_send"):
            body = p.get("body")
            if not isinstance(body, str) or not body.strip() or len(body) > 16000:
                raise BoardError("Message must contain 1 to 16000 characters")
            if method == "noticeboard_post":
                cursor = db.execute("INSERT INTO posts(agent,body,created) VALUES (?,?,?)", (agent, body, now))
            else:
                recipient = p.get("recipient_agent_id")
                if not db.execute("SELECT 1 FROM agents WHERE id=?", (recipient,)).fetchone():
                    raise BoardError("Unknown recipient")
                cursor = db.execute("INSERT INTO messages(sender,recipient,body,created) VALUES (?,?,?,?)", (agent, recipient, body, now))
            self.event(db, method, agent, {"id": cursor.lastrowid})
            return {"id": cursor.lastrowid}
        if method == "noticeboard_read":
            return {"posts": [dict(r) for r in db.execute("SELECT * FROM posts WHERE id>? ORDER BY id LIMIT 100", (p.get("after_id", 0),))]}
        if method == "inbox_read":
            return {"messages": [dict(r) for r in db.execute("SELECT * FROM messages WHERE recipient=? AND acknowledged IS NULL ORDER BY id LIMIT 100", (agent,))]}
        if method == "inbox_ack":
            ids = p.get("message_ids")
            if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
                raise BoardError("message_ids must be a non-empty list of integers")
            for message_id in ids:
                if not db.execute("SELECT 1 FROM messages WHERE id=? AND recipient=?", (message_id, agent)).fetchone():
                    raise BoardError("Can only acknowledge your own messages")
                db.execute("UPDATE messages SET acknowledged=? WHERE id=?", (now, message_id))
            self.event(db, "inbox_ack", agent, {"message_ids": ids})
            return {"acknowledged": ids}
        raise BoardError("Unknown operation")
