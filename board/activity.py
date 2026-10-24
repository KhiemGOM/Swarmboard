"""Short model-assigned labels; awake state is recorded only by native hooks."""
import time

from .core import BoardError


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS agent_activity (agent TEXT PRIMARY KEY, title TEXT, description TEXT, updated REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS native_activity (agent TEXT PRIMARY KEY, harness TEXT, session_key TEXT, awake INTEGER, event TEXT, updated REAL)')


def record(db, auth, harness, session_key, event):
    if event not in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'):
        return False
    valid = db.execute("SELECT 1 FROM agents a JOIN native_sessions n ON n.agent=a.id WHERE a.id=? AND a.token=? AND a.state='active' AND a.disconnected IS NULL AND n.harness=? AND n.session_key=?",
                       (auth['agent_id'], auth['session_token'], harness, session_key)).fetchone()
    if not valid:
        return False
    initialize(db)
    db.execute('INSERT OR REPLACE INTO native_activity VALUES (?,?,?,?,?,?)',
               (auth['agent_id'], harness, session_key, int(event != 'Stop'), event, time.time()))
    return True


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    if action == 'update':
        title = p.get('title')
        description = p.get('description', '')
        if not isinstance(title, str) or not title.strip() or len(title) > 60:
            raise BoardError('title must contain 1 to 60 characters')
        if not isinstance(description, str) or len(description) > 120:
            raise BoardError('description must be at most 120 characters')
        if any(key in p for key in ('awake', 'running', 'state')):
            raise BoardError('Awake state comes from native hooks, not activity labels')
        db.execute('INSERT OR REPLACE INTO agent_activity VALUES (?,?,?,?)',
                   (agent, title.strip(), description.strip(), now))
    elif action == 'clear':
        db.execute('DELETE FROM agent_activity WHERE agent=?', (agent,))
    elif action != 'status':
        raise BoardError('action must be status, update or clear')
    from .agent_status import enrich
    entry = dict(db.execute('SELECT * FROM agents WHERE id=?', (agent,)).fetchone())
    enrich(db, [entry])
    return {key: entry[key] for key in ('activity', 'awake', 'native_activity')}
