"""Opt-in one-shot conditions delivered through the normal durable inbox."""
import json
import sqlite3
import time
from pathlib import Path

from .core import BoardError


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS board_waits (
        id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, target TEXT, state TEXT,
        created REAL, expires REAL, message_id INTEGER)''')


def process(db, now):
    for wait in db.execute("SELECT * FROM board_waits WHERE state='WAITING' ORDER BY id").fetchall():
        kind, target, done = wait['kind'], wait['target'], False
        duplicate = False
        if kind == 'message_ack':
            row = db.execute('SELECT acknowledged FROM messages WHERE id=?', (int(target),)).fetchone()
            done = bool(row and row[0] is not None)
        elif kind == 'mutex_free':
            entity = json.loads(target)
            if 'resource' in entity:
                done = not db.execute("SELECT 1 FROM claims WHERE kind='resource' AND path=? AND active=1 AND expires>?", (entity['resource'], now)).fetchone()
            else:
                path = entity['path']
                def overlap(other):
                    return path == '.' or other == '.' or path == other or path.startswith(other + '/') or other.startswith(path + '/')
                busy = any(overlap(row[0]) for row in db.execute("SELECT path FROM claims WHERE kind='file' AND active=1 AND expires>?", (now,)))
                queued = any(overlap(row[0]) for row in db.execute("SELECT path FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')"))
                done = not busy and not queued
        elif kind == 'patch_finalized':
            row = db.execute('SELECT state FROM patch_batches WHERE id=?', (int(target),)).fetchone()
            done = bool(row and row[0] in ('APPLIED', 'BLOCKED'))
            duplicate = bool(db.execute('SELECT 1 FROM patch_members WHERE batch_id=? AND agent=?', (int(target), wait['agent'])).fetchone())
        expired = wait['expires'] <= now
        if not done and not expired:
            continue
        message_id = None
        if not (done and duplicate):
            text = (f'Swarmboard wait #{wait["id"]}: {kind} {target} ' +
                    ('is ready.' if done else 'timed out.') +
                    ' Availability is a snapshot, not a reservation. Inspect current state before acting. This notification does not authorize new work.')
            message_id = db.execute("INSERT INTO messages(sender,recipient,body,created) VALUES ('system',?,?,?)", (wait['agent'], text, now)).lastrowid
        db.execute('UPDATE board_waits SET state=?,message_id=? WHERE id=?', ('FIRED' if done else 'EXPIRED', message_id, wait['id']))


def poll(database, workspace):
    """The existing Claude watcher checks timers without registering a new agent."""
    db = sqlite3.connect(Path(database).resolve().as_uri() + '?mode=rw', uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            configured = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
            if not configured or Path(configured[0]).resolve() != Path(workspace).resolve():
                return
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='board_waits'").fetchone():
                process(db, time.time())
    finally:
        db.close()


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    if action == 'list':
        after = p.get('after_id', 0)
        if type(after) is not int or after < 0:
            raise BoardError('after_id must be a nonnegative integer')
        return {'waits': [dict(r) for r in db.execute('SELECT * FROM board_waits WHERE agent=? AND id>? ORDER BY id LIMIT 50', (agent, after))]}
    if action == 'cancel':
        wait_id = p.get('wait_id')
        if type(wait_id) is not int:
            raise BoardError('wait_id must be an integer')
        if not db.execute('SELECT 1 FROM board_waits WHERE id=? AND agent=?', (wait_id, agent)).fetchone():
            raise BoardError('Can only cancel your own wait')
        db.execute("UPDATE board_waits SET state='CANCELLED' WHERE id=? AND state='WAITING'", (wait_id,))
        return {'wait_id': wait_id, 'cancelled': True}
    if action != 'subscribe':
        raise BoardError('action must be subscribe, list or cancel')
    kind = p.get('kind')
    if kind == 'mutex_free':
        from .claims import resource
        if ('path' in p) == ('resource' in p):
            raise BoardError('Provide exactly one path or resource for mutex_free')
        target = json.dumps({'path': hub.path(p['path'])} if 'path' in p else {'resource': resource(p['resource'])})
    elif kind == 'message_ack':
        target = p.get('message_id')
        if type(target) is not int or not db.execute('SELECT 1 FROM messages WHERE id=? AND sender=?', (target, agent)).fetchone():
            raise BoardError('message_id must identify a message you sent')
        target = str(target)
    elif kind == 'patch_finalized':
        target = p.get('batch_id')
        if type(target) is not int or not db.execute('SELECT 1 FROM patch_batches WHERE id=?', (target,)).fetchone():
            raise BoardError('batch_id must identify an existing patch batch')
        target = str(target)
    else:
        raise BoardError('kind must be message_ack, mutex_free or patch_finalized; inbox delivery is already automatic')
    seconds = p.get('timeout_seconds', 3600)
    if type(seconds) is not int or not 1 <= seconds <= 86400:
        raise BoardError('timeout_seconds must be 1 to 86400')
    existing = db.execute("SELECT id FROM board_waits WHERE agent=? AND kind=? AND target=? AND state='WAITING'", (agent, kind, target)).fetchone()
    wait_id = existing[0] if existing else db.execute("INSERT INTO board_waits(agent,kind,target,state,created,expires) VALUES (?,?,?,'WAITING',?,?)", (agent, kind, target, now, now + seconds)).lastrowid
    process(db, now)  # check already-met conditions, closing subscribe/event races
    return {'wait': dict(db.execute('SELECT * FROM board_waits WHERE id=?', (wait_id,)).fetchone()),
            'delivery': 'One system inbox notification. Claude uses the existing linked async watcher; Codex sees it on its next interaction. Patch participants already get the final message, so their completion wait adds no duplicate.'}
