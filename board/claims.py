"""Transactional path/resource claims and durable commit custody."""
import re
import json
from .core import BoardError


def exclusions(claim):
    value = dict(claim).get('exclusions', [])
    return json.loads(value) if isinstance(value, str) else value


def intersects(claim, path):
    from .core import Hub
    return Hub.overlaps(claim['path'], path) and not any(path == item or path.startswith(item + '/') for item in exclusions(claim))


def covers(claim, path):
    return (claim['path'] == '.' or path == claim['path'] or path.startswith(claim['path'] + '/')) and intersects(claim, path)


def shared(a, b):
    root = a['path'] if a['path'] != '.' and (b['path'] == '.' or len(a['path']) >= len(b['path'])) else b['path']
    return intersects(a, root) and intersects(b, root)


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
        proposed = {(kind, path): db.execute('SELECT * FROM claims WHERE active=1 AND agent=? AND kind=? AND path=?',
                    (agent, kind, path)).fetchone() or {'path': path, 'exclusions': []} for kind, path in entities}
        ttl = p.get("ttl_seconds", 7200)
        if not isinstance(ttl, (int, float)) or isinstance(ttl, bool) or not 1 <= ttl <= 7200:
            raise BoardError("ttl_seconds must be between 1 and 7200")
        reason = p.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise BoardError("A claim reason is required")
        if type(p.get('partial', False)) is not bool:
            raise BoardError('partial must be a boolean; default false preserves atomic claims')
        if p.get('partial'):
            results = []
            for kind, path in entities:
                item = dispatch(hub, db, method, {**p, 'partial': False, 'paths': [path] if kind == 'file' else [],
                                'resources': [path] if kind == 'resource' else []}, agent, now)
                results.append({'kind': kind, 'path': path, **item})
            return {'accepted': all(r['accepted'] for r in results), 'partial': True, 'results': results,
                    'claim_ids': [i for r in results if r['accepted'] for i in r['claim_ids']]}
        from .coordination import hot_files
        hot = [file for file in (hot_files(hub) if files else []) if any(kind == 'file' and hub.overlaps(path, file) for kind, path in entities)]
        # Grandfather existing claims without allowing a new enclosing folder claim.
        new_hot = [file for file in hot if any(kind == 'file' and hub.overlaps(path, file) and not
                   db.execute("SELECT 1 FROM claims WHERE active=1 AND agent=? AND kind='file' AND path=?", (agent, path)).fetchone()
                   for kind, path in entities)]
        if new_hot:
            return {'accepted': False, 'queue_only_files': new_hot, 'reason': 'Queue-only policy: use patch_queue join; narrow folder claims to exclude these files.'}
        conflicts = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 AND agent!=?", (agent,))
                     if any(kind == r["kind"] and (shared(proposed[kind, path], r) if kind == "file" else path == r["path"])
                            for kind, path in entities)]
        if conflicts:
            return {"accepted": False, "conflicts": conflicts,
                    'next_step': 'For a common file, use board_coordination request_relax with the exact file path and reason. Holder cooperation is required; do not edit or snapshot a queued patch behind a direct writer.'}
        queues = [dict(row) for row in db.execute("SELECT id,path,state FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')")
                  if any(kind == 'file' and intersects(proposed[kind, path], row['path']) for kind, path in entities)]
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
                    protected_elsewhere = any(not intersects(held, path) for held in db.execute(
                        "SELECT * FROM claims WHERE active=1 AND kind='file' AND agent=? AND path=?", (old['owner'], old['path'])))
                    if protected_elsewhere:
                        continue  # A carved-out file does not transfer the remaining folder's custody.
                    if intersects(proposed[kind, path], old["path"]) and old["owner"] != agent:
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
            if not row or row["agent"] != agent:
                raise BoardError("Can only release your own active claims")
            if not row['active']:
                # Finish the deletion's custody without releasing a new claim.
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE name='file_deletions'").fetchone()
                deleted_claim = has_table and any(row['id'] in json.loads(record[0]) for record in db.execute(
                    'SELECT released_claims FROM file_deletions WHERE path=? AND owner=?', (row['path'], agent)))
                custodian = db.execute('SELECT owner,state FROM custody WHERE path=?', (row['path'],)).fetchone()
                newer = db.execute("SELECT 1 FROM claims WHERE active=1 AND kind='file' AND path=?", (row['path'],)).fetchone()
                if not deleted_claim or not custodian or custodian['owner'] != agent or custodian['state'] != 'HELD' or newer:
                    raise BoardError("Can only release your own active claims or finish your deleted file's HELD custody")
            rows.append(row)
        sha, held = p.get("commit_hash"), p.get("held_reason")
        if sha is not None and held is not None:
            raise BoardError("Provide either commit_hash or held_reason, not both")
        if sha is not None:
            sha = commit(sha)
        if held is not None and (not isinstance(held, str) or not held.strip()):
            raise BoardError("held_reason must explain unfinished work and your handoff plan")
        if any(r["kind"] == "file" for r in rows) and sha is None and held is None:
            from .custody_policy import ignored
            if not all(ignored(hub, r['path']) for r in rows if r['kind'] == 'file'):
                raise BoardError("File release requires commit_hash or held_reason; ignored untracked scratch may complete without a commit")
        reason = p.get("reason", "")
        if not sha and not held and (not isinstance(reason, str) or not reason.strip()):
            raise BoardError("Resource release requires a reason, such as tests finished")
        for row in rows:
            db.execute("UPDATE claims SET active=0 WHERE id=?", (row["id"],))
            if row["kind"] == "file":
                for custody in db.execute("SELECT path FROM custody WHERE owner=? AND state!='COMMITTED'", (agent,)).fetchall():
                    path = custody["path"]
                    if covers(row, path):
                        db.execute("UPDATE custody SET owner=?,state=?,reason=?,commit_hash=?,updated=? WHERE path=?",
                                   (agent, "COMMITTED" if sha else "HELD", held or "Commit evidence recorded", sha, now, path))
        hub.event(db, "released", agent, {"claim_ids": ids, "commit_hash": sha, "held_reason": held, "reason": reason})
        from .custody_policy import close
        closed = close(hub, db, agent, now)
        return {"released": ids, "commit_hash": sha, "held_reason": held, 'scratch_completed': closed}
    if method == "custody_resolve":
        scratch = p.get('completion', 'commit') == 'scratch'
        if p.get('completion', 'commit') not in ('commit', 'scratch'):
            raise BoardError('completion must be commit or scratch')
        sha = None if scratch else commit(p.get("commit_hash"))
        values = p.get("paths")
        if not isinstance(values, list) or not values:
            raise BoardError("paths must identify your held custody records")
        paths = list(dict.fromkeys(hub.path(v) for v in values))
        for path in paths:
            row = db.execute("SELECT owner,state FROM custody WHERE path=?", (path,)).fetchone()
            if not row or row["owner"] != agent or row["state"] != "HELD":
                raise BoardError("Only the custodian can resolve their HELD records; active work must use release")
        if scratch:
            from .custody_policy import close
            return {'resolved': close(hub, db, agent, now, paths, strict=True), 'completion': 'scratch'}
        for path in paths:
            db.execute("UPDATE custody SET state='COMMITTED',commit_hash=?,reason='Commit evidence recorded',updated=? WHERE path=?", (sha, now, path))
        hub.event(db, "custody_resolved", agent, {"paths": paths, "commit_hash": sha})
        return {"resolved": paths, "commit_hash": sha}
