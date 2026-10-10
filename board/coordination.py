"""Explicit hot-file policy, cooperative lock conversion and incident inboxes."""
import hashlib
import json
import time

from .core import BoardError


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS relax_requests (
            claim_id INTEGER, requester TEXT, reason TEXT, created REAL,
            PRIMARY KEY(claim_id,requester));
        CREATE TABLE IF NOT EXISTS coordination_incidents (
            id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, path TEXT,
            kind TEXT, reporter TEXT, evidence TEXT, created REAL);
    ''')


def hot_files(hub):
    config = hub.workspace / '.swarmboard.json'
    if not config.exists():
        return []
    try:
        if config.stat().st_size > 32000:
            raise ValueError('config exceeds 32000 bytes')
        value = json.loads(config.read_text(encoding='utf-8'))
        paths = value.get('queue_only', [])
        if not isinstance(paths, list) or len(paths) > 64:
            raise ValueError('queue_only must be a list of at most 64 concrete files')
        result = []
        for path in paths:
            if not isinstance(path, str) or len(path) > 240:
                raise ValueError('queue_only paths must be strings up to 240 characters')
            relative = hub.path(path)
            if relative == '.' or (hub.workspace / relative).is_dir():
                raise ValueError('queue_only accepts files, not directories')
            result.append(relative)
        return list(dict.fromkeys(result))
    except (OSError, ValueError, AttributeError) as exc:
        raise BoardError('Invalid .swarmboard.json: ' + str(exc)) from exc


def message(db, recipient, body, now):
    db.execute("INSERT INTO messages(sender,recipient,body,created) VALUES ('system',?,?,?)", (recipient, body, now))


def incident(hub, db, path, kind, evidence, reporter=None, signature=None):
    fingerprint = hashlib.sha256(json.dumps([path, kind, signature or evidence]).encode()).hexdigest()
    previous = db.execute('SELECT id FROM coordination_incidents WHERE fingerprint=?', (fingerprint,)).fetchone()
    if previous:
        return {'incident_id': previous[0], 'duplicate': True, 'notified': 0}
    now = time.time()
    ident = db.execute('INSERT INTO coordination_incidents(fingerprint,path,kind,reporter,evidence,created) VALUES (?,?,?,?,?,?)',
                       (fingerprint, path, kind, reporter, evidence, now)).lastrowid
    body = (f'Swarmboard coordination incident #{ident}: {kind}. File: {path}. '
            f'Source: {"agent report " + reporter if reporter else "board observation"}. Evidence: {evidence}\n'
            'Check whether your edits, commands or background processes caused this. If responsible, stop those writes, '
            'report what happened to affected agents, and confirm you will acquire the required lock or submit queued '
            'patches before continuing. Preserve everyone\'s changes; do not revert blindly. This is a self-check, '
            'not proof against any agent or permission for new work. Acknowledge after checking.')
    recipients = [r[0] for r in db.execute("SELECT id FROM agents WHERE state='active' AND id!=?", (reporter or '',))]
    for recipient in recipients:
        message(db, recipient, body, now)
    hub.event(db, 'coordination_incident', reporter or 'system', {'incident_id': ident, 'path': path, 'kind': kind})
    return {'incident_id': ident, 'duplicate': False, 'notified': len(recipients)}


def scan(hub):
    """Observed on hub interactions; no filesystem watcher or culprit inference."""
    from .patch_queue import read_file, digest
    with hub.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for batch in db.execute("SELECT * FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')").fetchall():
            try:
                current = read_file(hub, batch['path'])
                if current == batch['base'] or (batch['state'] == 'APPLYING' and current == batch['candidate']):
                    continue
                signature = [batch['id'], digest(current)]
                evidence = f'Queued file differs from recorded base in batch #{batch["id"]}. No author is known.'
            except BoardError as exc:
                signature = [batch['id'], str(exc)]
                evidence = f'Queued file cannot be read in batch #{batch["id"]}: {str(exc)[:500]}'
            incident(hub, db, batch['path'], 'queued_file_changed', evidence, signature=signature)


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    path = hub.path(p.get('path'))
    reason = p.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise BoardError('A reason/evidence of 1 to 1000 characters is required')
    if action == 'report':
        return incident(hub, db, path, 'reported_interference', reason, reporter=agent)
    claims = [r for r in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file'") if hub.overlaps(path, r['path'])]
    if len(claims) != 1 or claims[0]['path'] != path:
        raise BoardError('Relax needs one exact file claim; folder holders must narrow their claims first')
    claim = claims[0]
    if action == 'request_relax':
        if claim['agent'] == agent:
            raise BoardError('You own this claim; use relax after stopping direct writes')
        exists = db.execute('SELECT 1 FROM relax_requests WHERE claim_id=? AND requester=?', (claim['id'], agent)).fetchone()
        if not exists:
            db.execute('INSERT INTO relax_requests VALUES (?,?,?,?)', (claim['id'], agent, reason, now))
            message(db, claim['agent'], f'Agent {agent} requests patch queue mode for {path}. Reason: {reason}. '
                    'If appropriate, stop direct edits and background writers, then run /board relax FILE. '
                    'Keep exclusive mode if currently required and explain the blocker via inbox. This request does not revoke your claim.', now)
        return {'requested': True, 'duplicate': bool(exists), 'holder': claim['agent'], 'claim_id': claim['id']}
    if action != 'relax':
        raise BoardError('action must be request_relax, relax or report')
    if claim['agent'] != agent:
        raise BoardError('Only the claim holder can relax it')
    # Everything below commits together: no unlocked release/reclaim interval.
    db.execute('UPDATE claims SET active=0 WHERE id=?', (claim['id'],))
    db.execute("UPDATE custody SET state='HELD',reason=?,updated=? WHERE path=? AND owner=? AND state!='COMMITTED'",
               ('Converted to patch queue: ' + reason, now, path, agent))
    from .patch_queue import dispatch as queue_dispatch
    result = queue_dispatch(hub, db, {'action': 'join', 'path': str(hub.workspace / path), 'reason': reason}, agent, now)
    if not result.get('accepted'):
        raise BoardError('Cannot convert claim to queue; exclusive claim retained')
    notified = 0
    for row in db.execute('SELECT requester FROM relax_requests WHERE claim_id=?', (claim['id'],)).fetchall():
        recipient = row[0]
        # Follow successors without registering or impersonating another session.
        for _ in range(100):
            who = db.execute('SELECT state,successor_id FROM agents WHERE id=?', (recipient,)).fetchone()
            if not who or not who['successor_id']:
                break
            recipient = who['successor_id']
        if who and who['state'] == 'active':
            message(db, recipient, f'{path} is now patch queue #{result["batch"]["id"]}. Join explicitly and read its new base before preparing your patch; you have not been joined automatically.', now)
            notified += 1
    db.execute('DELETE FROM relax_requests WHERE claim_id=?', (claim['id'],))
    hub.event(db, 'claim_relaxed', agent, {'claim_id': claim['id'], 'batch_id': result['batch']['id']})
    return {**result, 'relaxed_claim_id': claim['id'], 'requesters_notified': notified,
            'custody': 'Existing saved work remains yours as HELD; resolve it with actual commit evidence. Future changes use queue patches.'}
