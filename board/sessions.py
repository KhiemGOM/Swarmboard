"""Native chat continuity; retirement ends ownership without banning future joins."""
import secrets

from .core import BoardError


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS native_sessions (harness TEXT NOT NULL, session_key TEXT NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(harness,session_key), UNIQUE(harness,agent))')


def migrate(db):
    initialize(db)
    # A legacy hook may have cached a provisional reconnect ID. The verified
    # desktop binding identifies the actual inbox recipient and takes priority.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_wake_bindings'").fetchone():
        import hashlib
        for row in db.execute('SELECT agent,thread_id FROM desktop_wake_bindings').fetchall():
            key = hashlib.sha256(('codex:'+row['thread_id']).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO native_sessions VALUES (?,?,?)', ('codex',key,row['agent']))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='hook_inbox_bindings'").fetchone():
        db.execute('INSERT OR IGNORE INTO native_sessions SELECT harness,session_key,agent FROM hook_inbox_bindings')


def bind(db, harness, key, agent):
    initialize(db)
    previous = db.execute('SELECT agent FROM native_sessions WHERE harness=? AND session_key=?', (harness, key)).fetchone()
    if previous and previous[0] != agent:
        old = db.execute('SELECT successor_id FROM agents WHERE id=?', (previous[0],)).fetchone()
        if not old or old[0] != agent:
            raise BoardError('Native chat already has an identity; reconnect it instead of registering another')
    other = db.execute('SELECT session_key FROM native_sessions WHERE harness=? AND agent=?', (harness, agent)).fetchone()
    if other and other[0] != key:
        raise BoardError('Agent identity already belongs to another native chat')
    if previous and previous[0] != agent:
        db.execute('UPDATE native_sessions SET agent=? WHERE harness=? AND session_key=?', (agent,harness,key))
    db.execute('INSERT OR IGNORE INTO native_sessions VALUES (?,?,?)', (harness, key, agent))


def invalidate(db, target, now, reason, successor=None):
    # Keep native ownership so the same chat can resume its ID after retirement.
    # Handoffs revoke credentials/work, not native ownership. Preserve history.
    db.execute("UPDATE agents SET state='retired',disconnected=?,reason=?,successor_id=COALESCE(?,successor_id),inheritance_hash=NULL WHERE id=?", (now, reason, successor, target))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_wake_bindings'").fetchone():
        db.execute('UPDATE desktop_wake_bindings SET enabled=0 WHERE agent=?', (target,))


def identify(db, harness, key, agent):
    """Accept an explicit local identity declaration; never transfer its work."""
    initialize(db)
    db.execute('DELETE FROM native_sessions WHERE harness=? AND (session_key=? OR agent=?)', (harness, key, agent))
    db.execute('INSERT INTO native_sessions VALUES (?,?,?)', (harness, key, agent))
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='hook_inbox_bindings'").fetchone():
        db.execute('DELETE FROM hook_inbox_bindings WHERE harness=? AND (session_key=? OR agent=?)', (harness, key, agent))
        db.execute('INSERT INTO hook_inbox_bindings VALUES (?,?,?)', (harness, agent, key))
    if harness == 'codex' and db.execute("SELECT 1 FROM sqlite_master WHERE name='desktop_wake_bindings'").fetchone():
        import hashlib
        for bound, thread in db.execute('SELECT agent,thread_id FROM desktop_wake_bindings').fetchall():
            same_chat = hashlib.sha256(('codex:'+thread).encode()).hexdigest() == key
            if (bound == agent and not same_chat) or (bound != agent and same_chat):
                db.execute('DELETE FROM desktop_wake_bindings WHERE agent=?', (bound,))


def reconnect(hub, db, p, agent, now):
    harness, key = p.get('harness'), p.get('session_key')
    if key is not None:
        import re
        if harness not in ('codex', 'claude', 'claude-code', 'claude-desktop') or not isinstance(key, str) or not re.fullmatch('[0-9a-f]{64}', key):
            raise BoardError('Invalid native session key; use target_agent_id alone to resume by board ID')
    canonical = db.execute('SELECT agent FROM native_sessions WHERE harness=? AND session_key=?', (harness, key)).fetchone() if key else None
    old_id = canonical[0] if canonical else p.get('target_agent_id')
    old = db.execute('SELECT * FROM agents WHERE id=?', (old_id,)).fetchone()
    if not old:
        raise BoardError('Native reconnect identity no longer exists')
    busy = (db.execute('SELECT 1 FROM claims WHERE agent=? AND active=1', (agent,)).fetchone()
            or db.execute("SELECT 1 FROM custody WHERE owner=? AND state!='COMMITTED'", (agent,)).fetchone()
            or db.execute("SELECT 1 FROM jobs WHERE owner=? AND state='CLAIMED'", (agent,)).fetchone()
            or db.execute("SELECT 1 FROM patch_members WHERE agent=? AND state='ACTIVE'", (agent,)).fetchone()
            or db.execute("SELECT 1 FROM patches WHERE agent=? AND state='APPLIED'", (agent,)).fetchone()
            or db.execute('SELECT 1 FROM messages WHERE recipient=? AND acknowledged IS NULL', (agent,)).fetchone())
    if busy and agent != old_id:
        raise BoardError('Reconnect provisional identity already owns work or inbox; reconcile before changing identity')
    if key:
        identify(db, harness, key, old_id)
    if agent != old_id:
        invalidate(db, agent, now, 'Same native chat reconnected to existing identity', old_id)
    token = secrets.token_urlsafe(32) if old['state'] == 'retired' else old['token']
    db.execute("UPDATE agents SET token=?,state='active',seen=?,disconnected=NULL,reason=NULL,successor_id=NULL WHERE id=?", (token, now, old_id))
    if old['state'] == 'retired':
        from .inheritance import issue
        issue(db, old_id)
        hub.event(db, 'agent_resumed', old_id, {'source': 'native_hook_reconnect', 'previous_successor_id': old['successor_id']})
    hub.event(db, 'native_reconnected', old_id, {'provisional_agent': agent})
    return {'agent_id': old_id, 'session_token': token}
