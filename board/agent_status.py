"""Stored agent metadata for workspace-wide discovery."""


def enrich(db, agents):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for agent in agents:
        ident = agent['id']
        sessions = [dict(row) for row in db.execute(
            'SELECT harness,session_key FROM native_sessions WHERE agent=? ORDER BY harness', (ident,))]
        hooks = [dict(row) for row in db.execute(
            'SELECT harness,session_key FROM hook_inbox_bindings WHERE agent=? ORDER BY harness', (ident,))] if 'hook_inbox_bindings' in tables else []
        binding = db.execute('SELECT * FROM desktop_wake_bindings WHERE agent=?', (ident,)).fetchone() if 'desktop_wake_bindings' in tables else None
        harnesses = sorted({row['harness'] for row in sessions + hooks} | ({'codex'} if binding else set()))
        agent['harnesses'] = harnesses
        agent['harness'] = harnesses[0] if len(harnesses) == 1 else None
        agent['native_sessions'] = sessions
        agent['hook_bindings'] = hooks
        agent['native_chat_id'] = binding['thread_id'] if binding else None
        agent['desktop_wake'] = dict(binding) if binding else None
        if agent['desktop_wake']:
            agent['desktop_wake'].pop('agent')
            agent['desktop_wake']['enabled'] = bool(binding['enabled'])
        delivery = db.execute(
            'SELECT thread_id,relay,through_id,state,created,detail FROM desktop_wake_deliveries WHERE agent=? ORDER BY created DESC LIMIT 1',
            (ident,)).fetchone() if 'desktop_wake_deliveries' in tables else None
        agent['last_wake_delivery'] = dict(delivery) if delivery else None
        agent['unread_inbox'] = db.execute(
            'SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged IS NULL', (ident,)).fetchone()[0]
        agent['job_counts'] = {row['state']: row['n'] for row in db.execute(
            'SELECT state,COUNT(*) AS n FROM jobs WHERE owner=? GROUP BY state', (ident,))}
        agent['current_jobs'] = [dict(row) for row in db.execute(
            "SELECT id,title,kind,state,expires,progress,updated FROM jobs WHERE owner=? AND state='CLAIMED' ORDER BY id", (ident,))]
        label = db.execute('SELECT title,description,updated FROM agent_activity WHERE agent=?', (ident,)).fetchone() if 'agent_activity' in tables else None
        native = db.execute('SELECT harness,session_key,awake,event,updated FROM native_activity WHERE agent=?', (ident,)).fetchone() if 'native_activity' in tables else None
        agent['activity'] = dict(label) if label else None
        agent['native_activity'] = dict(native) if native else None
        # Hook observations are authoritative; last contact and labels are not liveness.
        agent['model'] = None
        activity = binding['activity'] if binding and 'activity' in binding.keys() else 'unknown'
        awake = bool(native['awake']) if native else {'busy': True, 'idle': False}.get(activity)
        if agent['state'] != 'active' or agent.get('disconnected') is not None:
            awake = False
        agent['awake'] = agent['running'] = awake
