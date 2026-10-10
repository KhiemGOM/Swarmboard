"""Transactional path/resource claims and durable commit custody."""
import re
from .core import BoardError


def commit(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", value) or len(value) > 40 and len(value) != 64:
        raise BoardError("commit_hash must be a 7-40 character Git SHA, or a full 64-character SHA-256")
    return value.lower()


def resource(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*:[^\s\x00-\x1f]{1,200}", value):
        raise BoardError("Use a concrete resource ID such as db:core_test, port:3101, build:web/dist, git:index or misc:fixture")
    return value.casefold()


def dispatch(hub, db, method, p, agent, now):
    if method in ("claim", "mutex_claim"):
        files = p.get("paths", [])
        resources = p.get("resources", [])
        if not isinstance(files, list) or not isinstance(resources, list) or not (files or resources):
            raise BoardError("Provide a non-empty paths or resources list")
        if method == "mutex_claim" and (files or not resources):
            raise BoardError("mutex_claim accepts resources only")
        entities = list(dict.fromkeys([("file", hub.path(v)) for v in files] + [("resource", resource(v)) for v in resources]))
        ttl = p.get("ttl_seconds", 7200)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 1 <= ttl <= 7200:
            raise BoardError("ttl_seconds must be between 1 and 7200")
        reason = p.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BoardError("A claim reason is required")
        conflicts = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 AND agent!=?", (agent,))
                     if any(kind == r["kind"] and (hub.overlaps(path, r["path"]) if kind == "file" else path == r["path"])
                            for kind, path in entities)]
        if conflicts:
            return {"accepted": False, "conflicts": conflicts}
        queues = [dict(row) for row in db.execute("SELECT id,path,state FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')")
                  if any(kind == 'file' and hub.overlaps(path, row['path']) for kind, path in entities)]
        if queues:
            return {'accepted': False, 'patch_queue_conflicts': queues, 'reason': 'Patch queue lock forbids direct edits; join the queue or wait for finalization.'}
        ids = []
        for kind, path in entities:
            own = db.execute("SELECT id FROM claims WHERE active=1 AND agent=? AND kind=? AND path=?", (agent, kind, path)).fetchone()
            if own:
                db.execute("UPDATE claims SET expires=?,reason=? WHERE id=?", (now + ttl, reason, own[0]))
                claim_id = own[0]
            else:
                claim_id = db.execute("INSERT INTO claims(agent,path,kind,reason,started,expires,active) VALUES (?,?,?,?,?,?,1)",
                                      (agent, path, kind, reason, now, now + ttl)).lastrowid
            ids.append(claim_id)
            if kind == "file":
                prior = db.execute('SELECT state FROM custody WHERE path=?', (path,)).fetchone()
                if prior is None or prior['state'] == 'COMMITTED':
                    db.execute('INSERT OR REPLACE INTO custody_age VALUES (?,?)', (path, now))
                else:
                    db.execute('INSERT OR IGNORE INTO custody_age VALUES (?,?)', (path, now))
                # Continuing overlapping unfinished work also accepts its custody.
                for old in db.execute("SELECT path,owner FROM custody WHERE state!='COMMITTED'").fetchall():
                    if hub.overlaps(path, old["path"]) and old["owner"] != agent:
                        db.execute("UPDATE custody SET owner=?,updated=? WHERE path=?", (agent, now, old["path"]))
                        hub.event(db, "custody_transferred", agent, {"path": old["path"], "previous_owner": old["owner"]})
                db.execute("INSERT INTO custody VALUES (?,?,'ACTIVE',?,NULL,?) ON CONFLICT(path) DO UPDATE SET owner=excluded.owner,state='ACTIVE',reason=excluded.reason,commit_hash=NULL,updated=excluded.updated",
                           (path, agent, reason, now))
        hub.event(db, "claimed", agent, {"entities": entities, "claim_ids": ids})
        return {"accepted": True, "claim_ids": ids, "expires": now + ttl, "advisory_resources": [path for kind, path in entities if kind == "resource"]}
    if method == "release":
        ids = p.get("claim_ids")
        if not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids):
            raise BoardError("claim_ids must be a non-empty list of integers")
        rows = []
        for claim_id in dict.fromkeys(ids):
            row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if not row or row["agent"] != agent or not row["active"]:
                raise BoardError("Can only release your own active claims")
            rows.append(row)
        sha, held = p.get("commit_hash"), p.get("held_reason")
        if sha is not None and held is not None:
            raise BoardError("Provide either commit_hash or held_reason, not both")
        if sha is not None:
            sha = commit(sha)
        if held is not None and (not isinstance(held, str) or not held.strip()):
            raise BoardError("held_reason must explain unfinished work and your handoff plan")
        if any(r["kind"] == "file" for r in rows) and sha is None and held is None:
            raise BoardError("File release requires commit_hash or held_reason; custody cannot silently disappear")
        reason = p.get("reason", "")
        if not sha and not held and (not isinstance(reason, str) or not reason.strip()):
            raise BoardError("Resource release requires a reason, such as tests finished")
        for row in rows:
            db.execute("UPDATE claims SET active=0 WHERE id=?", (row["id"],))
            if row["kind"] == "file":
                for custody in db.execute("SELECT path FROM custody WHERE owner=? AND state!='COMMITTED'", (agent,)).fetchall():
                    path = custody["path"]
                    if row["path"] == "." or path == row["path"] or path.startswith(row["path"] + "/"):
                        db.execute("UPDATE custody SET owner=?,state=?,reason=?,commit_hash=?,updated=? WHERE path=?",
                                   (agent, "COMMITTED" if sha else "HELD", held or "Commit evidence recorded", sha, now, path))
        hub.event(db, "released", agent, {"claim_ids": ids, "commit_hash": sha, "held_reason": held, "reason": reason})
        return {"released": ids, "commit_hash": sha, "held_reason": held}
    if method == "custody_resolve":
        sha = commit(p.get("commit_hash"))
        values = p.get("paths")
        if not isinstance(values, list) or not values:
            raise BoardError("paths must identify your held custody records")
        paths = list(dict.fromkeys(hub.path(v) for v in values))
        for path in paths:
            row = db.execute("SELECT owner,state FROM custody WHERE path=?", (path,)).fetchone()
            if not row or row["owner"] != agent or row["state"] != "HELD":
                raise BoardError("Only the custodian can resolve their HELD records; active work must use release")
        for path in paths:
            db.execute("UPDATE custody SET state='COMMITTED',commit_hash=?,reason='Commit evidence recorded',updated=? WHERE path=?", (sha, now, path))
        hub.event(db, "custody_resolved", agent, {"paths": paths, "commit_hash": sha})
        return {"resolved": paths, "commit_hash": sha}
