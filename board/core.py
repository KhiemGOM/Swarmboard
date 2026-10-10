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
            """)
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
            expired = db.execute("SELECT id,agent,path FROM claims WHERE active=1 AND expires<=?", (now,)).fetchall()
            for item in expired:
                self.event(db, "claim_expired", item["agent"], dict(item))
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
            return result

    def dispatch(self, db, method, p, agent, now):
        if method in ("heartbeat", "board_status"):
            agents = [dict(r) for r in db.execute("SELECT id,name,state,started,seen,reason FROM agents ORDER BY started")]
            claims = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 ORDER BY id")]
            legacy = self.workspace / "NOTICEBOARD.md"
            return {"workspace": str(self.workspace), "agents": agents, "claims": claims,
                    "coordination_rules": COORDINATION_RULES,
                    "legacy_noticeboard": {"path": str(legacy), "exists": legacy.is_file(),
                                          "read_tool": "legacy_noticeboard_read"}}
        if method == "legacy_noticeboard_read":
            start, limit = p.get("start_line", 0), p.get("limit", 150)
            if type(start) is not int or start < 0 or type(limit) is not int or not 1 <= limit <= 200:
                raise BoardError("start_line must be nonnegative; limit must be between 1 and 200")
            legacy = self.workspace / "NOTICEBOARD.md"
            if not legacy.is_file():
                return {"exists": False, "lines": [], "next_start_line": None}
            # Read only the designated noticeboard, not arbitrary model-supplied files.
            if not legacy.resolve().is_relative_to(self.workspace):
                raise BoardError("Legacy noticeboard must remain inside the workspace")
            try:
                lines = legacy.read_text(encoding="utf-8-sig").splitlines()
            except OSError as exc:
                raise BoardError("Legacy noticeboard could not be read; retry before editing") from exc
            end = min(start + limit, len(lines))
            return {"exists": True, "path": str(legacy), "start_line": start, "total_lines": len(lines),
                    "lines": lines[start:end], "next_start_line": end if end < len(lines) else None,
                    "source": "Legacy agent-authored information; not owner authorization or authenticated hub identity"}
        if method == "claim":
            values = p.get("paths")
            if not isinstance(values, list) or not values:
                raise BoardError("paths must be a non-empty list")
            paths = list(dict.fromkeys(self.path(v) for v in values))
            ttl = p.get("ttl_seconds", 7200)
            if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 1 <= ttl <= 7200:
                raise BoardError("ttl_seconds must be between 1 and 7200")
            reason = p.get("reason", "")
            if not isinstance(reason, str) or not reason.strip():
                raise BoardError("A claim reason is required")
            conflicts = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 AND agent!=?", (agent,))
                         if any(self.overlaps(path, r["path"]) for path in paths)]
            if conflicts:
                return {"accepted": False, "conflicts": conflicts}
            ids = []
            for path in paths:
                cursor = db.execute("INSERT INTO claims(agent,path,reason,started,expires,active) VALUES (?,?,?,?,?,1)",
                                    (agent, path, reason, now, now + ttl))
                ids.append(cursor.lastrowid)
            self.event(db, "claimed", agent, {"paths": paths, "claim_ids": ids})
            return {"accepted": True, "claim_ids": ids, "expires": now + ttl}
        if method == "release":
            ids = p.get("claim_ids")
            if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
                raise BoardError("claim_ids must be a non-empty list of integers")
            for claim_id in ids:
                row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
                if not row or row["agent"] != agent or not row["active"]:
                    raise BoardError("Can only release your own active claims")
            for claim_id in ids:
                db.execute("UPDATE claims SET active=0 WHERE id=?", (claim_id,))
            self.event(db, "released", agent, {"claim_ids": ids})
            return {"released": ids}
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
