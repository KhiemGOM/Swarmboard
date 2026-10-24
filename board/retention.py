"""Recent coordination matters: prune old finished history, never live work; reset wipes on request."""
RETENTION_DAYS = 14

# An agent with any of these still matters, however long ago it was seen.
LIVE_WORK = (
    'SELECT 1 FROM claims WHERE agent=:a AND active=1',
    "SELECT 1 FROM custody WHERE owner=:a AND state!='COMMITTED'",
    "SELECT 1 FROM jobs WHERE owner=:a AND state='CLAIMED'",
    'SELECT 1 FROM messages WHERE recipient=:a AND acknowledged IS NULL',
    "SELECT 1 FROM patch_members WHERE agent=:a AND state='ACTIVE'",
    "SELECT 1 FROM patches WHERE agent=:a AND state='APPLIED'",
)
AGENT_TABLES = (('native_sessions', 'agent'), ('hook_inbox_bindings', 'agent'), ('agent_activity', 'agent'),
                ('native_activity', 'agent'), ('desktop_wake_bindings', 'agent'), ('reminder_delivery', 'agent'))


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def prune(db, now, days=RETENTION_DAYS):
    cutoff = now - days * 86400
    present = tables(db)
    removed = {}

    def delete(table, where, *args):
        if table in present:
            removed[table] = removed.get(table, 0) + db.execute(f'DELETE FROM {table} WHERE {where}', args).rowcount

    delete('events', 'created<?', cutoff)
    delete('posts', 'created<?', cutoff)
    delete('messages', 'acknowledged IS NOT NULL AND acknowledged<?', cutoff)
    delete('jobs', "state IN ('DONE','DELETED') AND updated<?", cutoff)
    delete('claims', 'active=0 AND started<?', cutoff)
    delete('file_relax_requests', 'claim_id NOT IN (SELECT id FROM claims)')
    delete('custody', "state='COMMITTED' AND updated<?", cutoff)
    delete('custody_age', 'path NOT IN (SELECT path FROM custody)')
    if {'patch_batches', 'patches', 'patch_members'} <= present:
        finished = """state NOT IN ('OPEN','READY','APPLYING') AND updated<? AND id NOT IN
                      (SELECT batch_id FROM patches WHERE state='APPLIED')"""
        delete('patches', f'batch_id IN (SELECT id FROM patch_batches WHERE {finished})', cutoff)
        delete('patch_members', f'batch_id IN (SELECT id FROM patch_batches WHERE {finished})', cutoff)
        delete('patch_batches', finished, cutoff)
    delete('desktop_wake_deliveries', "state NOT IN ('IN_FLIGHT','UNCERTAIN') AND created<?", cutoff)
    delete('board_waits', "state!='WAITING' AND created<?", cutoff)
    delete('scratch_completions', 'created<?', cutoff)
    delete('file_deletions', 'created<?', cutoff)
    checks = [sql for sql in LIVE_WORK if sql.split('FROM ')[1].split()[0] in present]
    departed = db.execute("""SELECT id FROM agents WHERE (state!='active' OR disconnected IS NOT NULL)
                             AND COALESCE(disconnected, seen, started)<?""", (cutoff,)).fetchall()
    for (agent,) in departed:
        if any(db.execute(sql, {'a': agent}).fetchone() for sql in checks):
            continue
        for table, column in AGENT_TABLES:
            delete(table, f'{column}=?', agent)
        delete('agents', 'id=?', agent)
    return {table: count for table, count in removed.items() if count}


def reset(db):
    """Wipe every board record; settings keep the workspace and database identity."""
    removed = {}
    for table in sorted(tables(db) - {'settings', 'sqlite_sequence'}):
        removed[table] = db.execute(f'DELETE FROM {table}').rowcount
    return {table: count for table, count in removed.items() if count}
