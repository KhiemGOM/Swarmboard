"""Verified deletion bookkeeping, shared by hooks and ordinary hub interactions."""
import json
import stat

from .core import BoardError


def initialize(db):
    # Single statements preserve the caller's transaction (executescript would commit it).
    db.execute("CREATE TABLE IF NOT EXISTS file_observations (path TEXT PRIMARY KEY, present INTEGER, updated REAL)")
    db.execute("CREATE TABLE IF NOT EXISTS file_deletions (id INTEGER PRIMARY KEY, path TEXT, owner TEXT, observer TEXT, reason TEXT, released_claims TEXT, created REAL)")


def existence(hub, path):
    candidate = hub.workspace / path
    # Recheck containment even for persisted paths; symlink escapes are not absence.
    hub.path(path)
    try:
        mode = candidate.lstat().st_mode
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'
    return 'file' if stat.S_ISREG(mode) else 'other'


def record_deleted(hub, db, path, observer, now, reason):
    """Release vanished exact-file locks; retain custody, folders and queue evidence."""
    initialize(db)
    if path == '.' or existence(hub, path) != 'missing':
        raise BoardError('Deletion cleanup requires a verified missing file inside the workspace')
    claims = list(db.execute("SELECT id,agent FROM claims WHERE active=1 AND kind='file' AND path=?", (path,)))
    previous = db.execute('SELECT present FROM file_observations WHERE path=?', (path,)).fetchone()
    # A newly claimed path starts another lifecycle, even if recreation happened
    # entirely between board calls and the stored observation still says missing.
    if previous and not previous['present'] and not claims:
        prior = db.execute('SELECT id FROM file_deletions WHERE path=? ORDER BY id DESC LIMIT 1', (path,)).fetchone()
        if prior:
            return {'path': path, 'deletion_id': prior[0], 'duplicate': True, 'released_claim_ids': []}
    custody = db.execute('SELECT owner FROM custody WHERE path=?', (path,)).fetchone()
    from .claims import covers
    folder = next((row for row in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file'") if covers(row, path)), None)
    owner = custody[0] if custody else (claims[0]['agent'] if claims else (folder['agent'] if folder else None))
    if owner and not custody:
        db.execute("INSERT INTO custody VALUES (?,?,'HELD',?,NULL,?)", (path, owner, 'Deleted child file; completion evidence required', now))
        db.execute('INSERT OR IGNORE INTO custody_age VALUES (?,?)', (path, now))
    released = [row['id'] for row in claims]
    for claim in claims:
        db.execute('UPDATE claims SET active=0 WHERE id=?', (claim['id'],))
    db.execute("UPDATE custody SET state='HELD',reason=?,updated=? WHERE path=? AND state='ACTIVE'",
               ('File deleted; lock released, deletion awaits commit evidence. ' + reason, now, path))
    ident = db.execute('INSERT INTO file_deletions(path,owner,observer,reason,released_claims,created) VALUES (?,?,?,?,?,?)',
                       (path, owner, observer, reason, json.dumps(released), now)).lastrowid
    db.execute('INSERT INTO file_observations VALUES (?,0,?) ON CONFLICT(path) DO UPDATE SET present=0,updated=excluded.updated', (path, now))
    hub.event(db, 'file_deleted', observer, {'path': path, 'deletion_id': ident, 'released_claim_ids': released, 'reason': reason})
    if owner:
        from .coordination import message
        message(db, owner, f'Swarmboard observed deletion of {path}. Exact file locks released: {released}. '
                'Unfinished custody and history are preserved; record commit evidence for the deletion. '
                'Observation does not establish who deleted it.', now)
    return {'path': path, 'deletion_id': ident, 'duplicate': False, 'released_claim_ids': released}


def reconcile(hub, db, observer, now):
    """Check recorded paths only, on interaction; no directory walk or watcher."""
    initialize(db)
    paths = {row[0] for row in db.execute('SELECT path FROM file_observations')}
    paths.update(row[0] for row in db.execute("SELECT path FROM claims WHERE active=1 AND kind='file'"))
    paths.update(row[0] for row in db.execute("SELECT path FROM custody WHERE state='HELD'"))
    deleted = []
    for path in sorted(paths):
        try:
            state = existence(hub, path)
        except BoardError:
            continue  # Unsafe/changed symlink paths are not missing-file evidence.
        previous = db.execute('SELECT present FROM file_observations WHERE path=?', (path,)).fetchone()
        if state == 'file':
            db.execute('INSERT INTO file_observations VALUES (?,1,?) ON CONFLICT(path) DO UPDATE SET present=1,updated=excluded.updated', (path, now))
        elif state == 'missing' and previous and previous['present']:
            deleted.append(record_deleted(hub, db, path, observer, now, 'Previously observed file is now missing'))
    return deleted


def cleanup(hub, db, observer, now):
    deleted = reconcile(hub, db, observer, now)
    from .custody_policy import close
    completed = close(hub, db, observer, now)
    return {'workspace': str(hub.workspace), 'deletion_count': len(deleted),
            'scratch_completed': completed,
            'released_claim_count': sum(len(row['released_claim_ids']) for row in deleted),
            'deletions': deleted[:10], 'has_more': len(deleted) > 10,
            'history_tool': 'board_file action=history'}


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    if action == 'reconcile':
        return cleanup(hub, db, agent, now)
    path = hub.path(p.get('path'))
    if action == 'history':
        after, limit = p.get('after_id', 0), p.get('limit', 5)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 10:
            raise BoardError('history requires nonnegative after_id and limit between 1 and 10')
        rows = [dict(row) for row in db.execute('SELECT * FROM file_deletions WHERE path=? AND id>? ORDER BY id LIMIT ?', (path, after, limit + 1))]
        for row in rows:
            row['released_claims'] = json.loads(row['released_claims'])
        result = {'path': path, 'records': rows}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='scratch_completions'").fetchone():
            count = db.execute('SELECT COUNT(*) FROM scratch_completions WHERE path=?', (path,)).fetchone()[0]
            if count:
                result['scratch_history'] = {'count': count, 'tool': 'board_read', 'entity': 'scratch', 'record_id': path}
        return result
    if action not in ('deleted', 'observe'):
        raise BoardError('Use action=deleted, observe, reconcile or history')
    from .claims import covers
    owns = db.execute('SELECT 1 FROM custody WHERE path=? AND owner=?', (path, agent)).fetchone() or any(
        covers(row, path) for row in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file' AND agent=?", (agent,)))
    if not owns:
        raise BoardError('Only the file custodian or covering claim holder may report it; use reconcile for board observations')
    if action == 'observe':
        state = existence(hub, path)
        if state == 'file':
            db.execute('INSERT INTO file_observations VALUES (?,1,?) ON CONFLICT(path) DO UPDATE SET present=1,updated=excluded.updated', (path, now))
        return {'path': path, 'observed': state == 'file'}
    reason = p.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise BoardError('Deletion requires a reason up to 1000 characters')
    return record_deleted(hub, db, path, agent, now, reason)


def cleanup_local(hub):
    """Human-run terminal cleanup; no MCP agent registration or impersonation."""
    import time
    with hub.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        deleted = reconcile(hub, db, 'operator', time.time())
        from .waits import process
        process(db, time.time())
        hub.event(db, 'manual_file_cleanup', 'operator', {'deletion_count': len(deleted)})
        return {'workspace': str(hub.workspace), 'deletion_count': len(deleted),
                'released_claim_count': sum(len(row['released_claim_ids']) for row in deleted),
                'deletions': deleted}
