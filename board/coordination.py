"""Explicit hot-file policy, cooperative lock conversion and incident inboxes."""
import hashlib
import json
import re
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
                evidence = json.dumps({'reason': f'Queued file differs from recorded base in batch #{batch["id"]}. No author is known.',
                                       'before_hash': batch['base_hash'], 'after_hash': digest(current),
                                       'before_verification': 'recorded_queue_base', 'after_verification': 'observed_file_bytes'})
            except BoardError as exc:
                signature = [batch['id'], str(exc)]
                evidence = json.dumps({'reason': f'Queued file cannot be read in batch #{batch["id"]}: {str(exc)[:500]}',
                                       'before_hash': batch['base_hash'], 'after_hash': None, 'after_verification': 'unreadable'})
            incident(hub, db, batch['path'], 'queued_file_changed', evidence, signature=signature)


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    path = hub.path(p.get('path'))
    if action == 'history':
        after, limit = p.get('after_id', 0), p.get('limit', 5)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 10:
            raise BoardError('history after_id must be nonnegative and limit must be 1 to 10')
        records = [dict(row) for row in db.execute('SELECT id,kind,reporter,evidence,created FROM coordination_incidents WHERE path=? AND id>? ORDER BY id LIMIT ?', (path, after, limit))]
        for record in records:
            try:
                record['evidence'] = json.loads(record['evidence'])
            except ValueError:
                record['evidence'] = {'reason': record['evidence']}  # Keep historical free-text reports.
        return {'path': path, 'incidents': records, 'next_after_id': records[-1]['id'] if records else after,
                'instructions': 'Durable file custody history; inbox acknowledgments and ownership changes do not remove it. Evidence of a change does not identify its author.'}
    reason = p.get('reason')
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
        raise BoardError('A reason/evidence of 1 to 1000 characters is required')
    if action == 'report':
        hashes = [p.get(key) for key in ('before_hash', 'after_hash')]
        if any(not isinstance(value, str) or not re.fullmatch('[0-9a-fA-F]{64}', value) for value in hashes):
            raise BoardError('report requires before_hash and after_hash: SHA-256 of raw file bytes, each 64 hex characters')
        before, after = (value.lower() for value in hashes)
        if before == after:
            raise BoardError('Report hashes are identical; no content change is evidenced')
        file = hub.workspace / path
        try:
            if not file.is_file() or file.stat().st_size > 16 * 1024 * 1024:
                raise BoardError('Hash reports require an existing regular file up to 16 MiB')
            observed = hashlib.sha256()
            with file.open('rb') as stream:
                total = 0
                for chunk in iter(lambda: stream.read(65536), b''):
                    total += len(chunk)
                    if total > 16 * 1024 * 1024:
                        raise BoardError('Report file grew beyond 16 MiB')
                    observed.update(chunk)
        except OSError as exc:
            raise BoardError('Cannot verify report file: ' + str(exc)) from exc
        if observed.hexdigest() != after:
            raise BoardError('after_hash does not match current raw file bytes; recheck before reporting')
        batch = db.execute("SELECT base_hash FROM patch_batches WHERE path=? AND state IN ('OPEN','READY','APPLYING')", (path,)).fetchone()
        if batch and before != batch['base_hash']:
            raise BoardError('before_hash must match the recorded queue base for this file')
        evidence = {'reason': reason, 'before_hash': before, 'after_hash': after,
                    'before_verification': 'recorded_queue_base' if batch else 'reporter_provided',
                    'after_verification': 'current_file_bytes'}
        return {**incident(hub, db, path, 'reported_interference', json.dumps(evidence), reporter=agent,
                           signature=[before, after]), **evidence,
                'custody_history': {'action': 'history', 'path': path}}
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
