"""Shared delivery hints: suppress repeated prose, never acknowledge inbox data."""
import hashlib
import json
import time


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS reminder_delivery (agent TEXT, topic TEXT, signature TEXT, PRIMARY KEY(agent,topic))')


def changed(db, agent, topic, value, force=False):
    initialize(db)
    signature = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    previous = db.execute('SELECT signature FROM reminder_delivery WHERE agent=? AND topic=?', (agent, topic)).fetchone()
    db.execute('INSERT OR REPLACE INTO reminder_delivery VALUES (?,?,?)', (agent, topic, signature))
    return force or not previous or previous[0] != signature


def inbox(db, agent):
    rows = db.execute("SELECT id,sender,body FROM messages m WHERE recipient=? AND acknowledged IS NULL AND NOT EXISTS (SELECT 1 FROM reminder_delivery d WHERE d.agent=m.recipient AND d.topic='message:' || m.id) ORDER BY id LIMIT 3", (agent,)).fetchall()
    preview = []
    for row in rows:
        topic = 'message:' + str(row['id'])
        if db.execute('SELECT 1 FROM reminder_delivery WHERE agent=? AND topic=?', (agent, topic)).fetchone():
            continue
        if len(preview) == 3:
            break
        changed(db, agent, topic, row['id'])
        preview.append({'id': row['id'], 'sender': row['sender'], 'body': row['body'][:1000]})
    return preview


def attach(db, agent, result, force=False, include_inbox=True):
    initialize(db)
    preview = inbox(db, agent) if include_inbox else []
    result['inbox_preview'] = preview
    if preview:
        result['inbox_reminder'] = 'New inbox message(s): read, handle, then acknowledge.'
    held = [tuple(r) for r in db.execute("SELECT c.path,c.updated,a.since FROM custody c LEFT JOIN custody_age a ON a.path=c.path WHERE c.owner=? AND c.state='HELD' ORDER BY c.path", (agent,))]
    old = any(r[2] is not None and time.time() - r[2] >= 7200 for r in held)
    if changed(db, agent, 'custody', [held, old], force) and held:
        result['custody_reminder'] = f'{len(held)} HELD path(s) await completion evidence or handoff.'
        if old:
            result['custody_reminder'] += ' Some work is over 2h old; commit small coherent pieces.'
    jobs = [tuple(r) for r in db.execute("SELECT id,progress FROM jobs WHERE owner=? AND state='CLAIMED' ORDER BY id", (agent,))]
    result['owned_jobs_count'] = len(jobs)
    if jobs:
        result['owned_jobs'] = [dict(r) for r in db.execute("SELECT id,expires FROM jobs WHERE owner=? AND state='CLAIMED' ORDER BY id LIMIT 10", (agent,))]
    if changed(db, agent, 'jobs', jobs, force) and jobs:
        result['job_reminder'] = f'{len(jobs)} unfinished job(s). Save progress, finish with evidence or yield.'
    patches = [r[0] for r in db.execute("SELECT id FROM patches WHERE agent=? AND state='APPLIED' ORDER BY id", (agent,))]
    if changed(db, agent, 'patches', patches, force) and patches:
        result['patch_custody_reminder'] = f'{len(patches)} applied patch(es) await commit evidence.'
