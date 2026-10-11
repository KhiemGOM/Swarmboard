"""Caller-only workspace switching; no global configuration or state migration."""
from pathlib import Path

from .client import call
from .core import BoardError
from .runtime import ensure_hub, workspace_connection


def leave(hub, db, params, agent, now):
    from .desktop_wake import initialize
    initialize(db)
    reason = params.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise BoardError('Workspace switching requires a reason of 1 to 2000 characters')
    blockers = {
        'claims': db.execute('SELECT COUNT(*) FROM claims WHERE active=1 AND agent=?', (agent,)).fetchone()[0],
        'jobs': db.execute("SELECT COUNT(*) FROM jobs WHERE state='CLAIMED' AND owner=?", (agent,)).fetchone()[0],
        'patch_batches': db.execute("""SELECT COUNT(DISTINCT b.id) FROM patch_batches b
            JOIN patch_members m ON m.batch_id=b.id WHERE m.agent=?
            AND b.state IN ('OPEN','READY','APPLYING')""", (agent,)).fetchone()[0],
        'wake_deliveries': db.execute("""SELECT COUNT(*) FROM desktop_wake_deliveries
            WHERE (agent=? OR relay=?) AND state IN ('IN_FLIGHT','UNCERTAIN')""", (agent, agent)).fetchone()[0],
    }
    if any(blockers.values()):
        raise BoardError('Cannot switch workspace with active work: ' + str(blockers) +
                         '. Release locks, finish/yield jobs, finalize patch batches and settle wake deliveries first.')
    held = db.execute("SELECT COUNT(*) FROM custody WHERE owner=? AND state!='COMMITTED'", (agent,)).fetchone()[0]
    patches = db.execute("SELECT COUNT(*) FROM patches WHERE agent=? AND state='APPLIED'", (agent,)).fetchone()[0]
    unread = db.execute('SELECT COUNT(*) FROM messages WHERE recipient=? AND acknowledged IS NULL', (agent,)).fetchone()[0]
    db.execute('UPDATE desktop_wake_bindings SET enabled=0 WHERE agent=?', (agent,))
    db.execute("UPDATE board_waits SET state='CANCELLED' WHERE agent=? AND state='WAITING'", (agent,))
    db.execute('UPDATE agents SET disconnected=?,reason=? WHERE id=?', (now, reason, agent))
    hub.event(db, 'workspace_left', agent, {'reason': reason, 'held_paths': held, 'uncommitted_patches': patches})
    return {'previous_agent_id': agent, 'previous_workspace': str(hub.workspace),
            'held_paths_retained': held, 'uncommitted_patches_retained': patches,
            'unread_inbox_retained': unread}


def switch(args, identity, arguments):
    # No caller-supplied identity, token, URL, database or target agent is accepted.
    if set(arguments) != {'workspace', 'reason'}:
        raise BoardError('board_workspace accepts only workspace and reason')
    value, reason = arguments['workspace'], arguments['reason']
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise BoardError('workspace must be an absolute existing project directory')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
        raise BoardError('reason must contain 1 to 2000 characters')
    workspace, database, url = workspace_connection(value)
    if identity is None:
        ensure_hub(url, workspace, database, True)
        from .mcp import native_registration
        new_identity = call(url, 'register', native_registration(args.name))
        args.workspace, args.database, args.url = workspace, database, url
        return new_identity, {'switched': True, 'workspace': workspace,
                              'instructions': new_identity['instructions']}
    if Path(workspace) == Path(args.workspace):
        return identity, {'switched': False, 'workspace': workspace}
    # Validate/start destination before touching the old identity. Saved routing
    # is checked against its database and port; never rewrite it to force a match.
    ensure_hub(url, workspace, database, True)
    old_wake = call(args.url, 'board_desktop_wake', {
        'agent_id': identity['agent_id'], 'session_token': identity['session_token'], 'action': 'status'})
    from .mcp import native_registration
    registration = native_registration(args.name)
    if old_wake['enabled'] and args.name == 'codex':
        import hashlib
        registration.update(native_harness='codex', native_session_key=hashlib.sha256(('codex:'+old_wake['thread_id']).encode()).hexdigest())
    new_identity = call(url, 'register', registration)
    try:
        if old_wake['enabled']:
            call(url, 'board_desktop_wake', {'agent_id': new_identity['agent_id'],
                'session_token': new_identity['session_token'], 'action': 'enable',
                'thread_id': old_wake['thread_id'], 'workspace': workspace,
                'reason': 'Preserving this same chat wake binding across owner-authorized workspace switch',
                'owner_authorization': 'OWNER_REQUESTED_DESKTOP_WAKE'})
        previous = call(args.url, 'workspace_leave', {
            'agent_id': identity['agent_id'], 'session_token': identity['session_token'], 'reason': reason})
        # An older running hub may still include its automatic handoff key.
        previous.pop('previous_inheritance_secret', None)
    except BoardError:
        # Registration succeeded but departure did not. Keep the live connection
        # pointed at the original hub; the unused destination session disconnects.
        try:
            call(url, 'disconnect', {'agent_id': new_identity['agent_id'],
                 'session_token': new_identity['session_token'], 'reason': 'Workspace switch did not complete'})
        except BoardError:
            pass
        raise
    args.workspace, args.database, args.url = workspace, database, url
    return new_identity, {**previous, 'switched': True, 'workspace': workspace,
        'desktop_wake_preserved': bool(old_wake['enabled']),
        'instructions': 'Use this response agent ID and workspace, not the cached initialization ID. '
            'Read board_status, noticeboard_read and inbox before work. Old history/custody stays on its board; '
            'Use inheritance_key explicitly before leaving if a private handoff key is needed. This changes only this MCP connection, not the harness cwd, '
            'saved MCP settings or installed hooks/watchers. Hooks must be configured for the target workspace; '
            'An enabled desktop wake binding follows this same chat automatically; wake is always on. '
            'If not bound yet, check board_desktop_wake status and register your verified own chat under the default policy. '
            'Native-bound chats reuse their existing identity when returning to a board. '
            'If native continuity is unavailable, use explicit inheritance with the old private key; never assume a new ID owns old work.'}
