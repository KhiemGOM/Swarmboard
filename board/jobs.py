"""Claimable jobs with fenced leases and explicit completion evidence."""
import json
import secrets

from .core import BoardError
from .claims import commit


def text(value, field, limit=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise BoardError(f'{field} must contain 1 to {limit} characters')
    return value.strip()


def ttl(p):
    value = p.get('ttl_seconds', 1800)
    if type(value) is not int or not 1 <= value <= 7200:
        raise BoardError('ttl_seconds must be an integer between 1 and 7200')
    return value


def view(row, agent):
    result = dict(row)
    if result['owner'] != agent or result['state'] != 'CLAIMED':
        result.pop('lease_token', None)
    result['checks'] = json.loads(result['checks']) if result['checks'] else []
    result['followups'] = json.loads(result.get('followups') or '[]')
    return result


def reopen(hub, db, row, now, reason):
    db.execute("UPDATE jobs SET state='OPEN',previous_owner=owner,owner=NULL,lease_token=NULL,expires=NULL,recovery_reason=?,updated=? WHERE id=?",
               (reason, now, row['id']))
    hub.event(db, 'job_reopened', row['owner'], {'job_id': row['id'], 'reason': reason})


def expire(hub, db, now):
    for row in db.execute("SELECT * FROM jobs WHERE state='CLAIMED' AND expires<=?", (now,)).fetchall():
        reopen(hub, db, row, now, 'Job lease expired; inspect prior work and acquire file/resource locks before continuing')


def disconnect(hub, db, agent, now, reason):
    db.execute('UPDATE agents SET disconnected=?,reason=? WHERE id=?', (now, reason, agent))
    for row in db.execute("SELECT * FROM jobs WHERE state='CLAIMED' AND owner=?", (agent,)).fetchall():
        reopen(hub, db, row, now, reason)
    recover_untracked(hub, db, agent, now, reason)
    hub.event(db, 'disconnected', agent, {'reason': reason})
    return {'disconnected_agent': agent, 'file_claims_preserved': True}


def recover_untracked(hub, db, agent, now, reason):
    # Older sessions may have file custody without ever creating a job.
    custody = [dict(r) for r in db.execute("SELECT path,reason FROM custody WHERE owner=? AND state!='COMMITTED'", (agent,))]
    if not custody or db.execute("SELECT 1 FROM jobs WHERE (owner=? AND state='CLAIMED') OR (previous_owner=? AND state='OPEN')", (agent, agent)).fetchone():
        return
    description = 'Recovery review of prior file custody; do not assume changes are complete or expand authorization.\n' + json.dumps(custody)
    job_id = db.execute("INSERT INTO jobs(title,description,acceptance,kind,state,creator,previous_owner,recovery_reason,created,updated) VALUES (?,?,?,'code','OPEN',?,?,?,?,?)",
                        ('Recover unfinished agent work', description,
                         'Inspect existing changes and original scope, acquire file/resource locks or explicit takeover, complete appropriate validation and record the resulting commit. If scope is unclear, save a handoff and clarify with the owner.',
                         agent, agent, reason, now, now)).lastrowid
    hub.event(db, 'job_recovery_posted', agent, {'job_id': job_id})


def transfer(hub, db, target, agent, now):
    for row in db.execute("SELECT id FROM jobs WHERE state='CLAIMED' AND owner=?", (target,)).fetchall():
        db.execute('UPDATE jobs SET previous_owner=owner,owner=?,lease_token=?,expires=?,updated=? WHERE id=?',
                   (agent, secrets.token_hex(16), now + 1800, now, row['id']))
        hub.event(db, 'job_transferred', agent, {'job_id': row['id'], 'previous_owner': target})


def dispatch(hub, db, method, p, agent, now):
    if method in ('job_post', 'job_post_and_claim'):
        title = text(p.get('title'), 'title', 200)
        description = text(p.get('description'), 'description')
        acceptance = text(p.get('acceptance'), 'acceptance')
        kind = p.get('kind', 'code')
        if kind not in ('code', 'noncode'):
            raise BoardError('kind must be code or noncode')
        parent = p.get('parent_job_id')
        duration = ttl(p) if method == 'job_post_and_claim' else None
        if parent is not None and (type(parent) is not int or not db.execute('SELECT 1 FROM jobs WHERE id=?', (parent,)).fetchone()):
            raise BoardError('parent_job_id must identify an existing job')
        job_id = db.execute("INSERT INTO jobs(title,description,acceptance,kind,state,creator,parent_job_id,created,updated) VALUES (?,?,?,?,'OPEN',?,?,?,?)",
                            (title, description, acceptance, kind, agent, parent, now, now)).lastrowid
        if duration is not None:
            db.execute("UPDATE jobs SET state='CLAIMED',owner=?,lease_token=?,expires=? WHERE id=?",
                       (agent, secrets.token_hex(16), now + duration, job_id))
        hub.event(db, method, agent, {'job_id': job_id})
        result = {'job': view(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), agent)}
        if duration is not None:
            result.update(accepted=True, locks_unchanged=True, activity_reminder='Update your short current-work label with board_activity.')
        return result
    if method == 'job_list':
        state = p.get('state')
        state = 'LIVE' if state is None else state
        if state not in ('LIVE', 'OPEN', 'CLAIMED', 'DONE', 'DELETED', 'ALL'):
            raise BoardError('state must be LIVE, OPEN, CLAIMED, DONE, DELETED or ALL')
        from .responses import page
        after, limit = page(p)
        rows = db.execute("SELECT * FROM jobs WHERE id>? AND (?='ALL' OR (?='LIVE' AND state IN ('OPEN','CLAIMED')) OR state=?) ORDER BY id LIMIT ?", (after, state, state, state, limit + 1))
        return {'jobs': [view(row, agent) for row in rows]}
    job_id = p.get('job_id')
    if type(job_id) is not int:
        raise BoardError('job_id must be an integer')
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise BoardError('Unknown job')
    if method == 'job_delete':
        reason = text(p.get('reason'), 'reason', 4000)
        if agent not in (row['creator'], row['owner'], row['previous_owner']):
            raise BoardError('job_delete requires job creator, current owner or previous owner')
        if row['state'] == 'CLAIMED' and (row['owner'] != agent or not secrets.compare_digest(row['lease_token'] or '', str(p.get('lease_token', '')))):
            raise BoardError('job_delete cannot remove another live owner; current owner and lease_token required')
        if row['state'] == 'DELETED':
            return {'deleted_job_id': job_id, 'already_deleted': True, 'locks_unchanged': True}
        db.execute("UPDATE jobs SET state='DELETED',deleted_at=?,deletion_reason=?,lease_token=NULL,expires=NULL,updated=? WHERE id=?", (now, reason, now, job_id))
        hub.event(db, method, agent, {'job_id': job_id, 'reason': reason})
        return {'deleted_job_id': job_id, 'history_preserved': True, 'locks_unchanged': True}
    if method == 'job_followup':
        request = text(p.get('request'), 'request')
        duration = ttl(p)
        if row['state'] not in ('CLAIMED', 'DONE'):
            raise BoardError('Claim OPEN work before adding follow-ups')
        if row['state'] == 'CLAIMED' and (row['owner'] != agent or not secrets.compare_digest(row['lease_token'] or '', str(p.get('lease_token', '')))):
            raise BoardError('Current job lease_token required')
        followups = json.loads(row['followups'] or '[]')
        entry = {'request': request, 'author': agent, 'created': now}
        if row['state'] == 'DONE':
            entry['previous_completion'] = {'owner': row['owner'], 'summary': row['summary'], 'commit_hash': row['commit_hash'],
                                            'checks': json.loads(row['checks'] or '[]')}
        followups.append(entry)
        if len(followups) > 100 or len(json.dumps(followups)) > 100000:
            raise BoardError('Follow-up history is full; post a linked job')
        if row['state'] == 'DONE':
            db.execute("UPDATE jobs SET previous_owner=owner,owner=?,summary=NULL,commit_hash=NULL,checks=NULL,progress='Follow-up pending' WHERE id=?", (agent, job_id))
        lease = secrets.token_hex(16) if row['state'] == 'DONE' else row['lease_token']
        db.execute("UPDATE jobs SET followups=?,state='CLAIMED',lease_token=?,expires=?,updated=? WHERE id=?",
                   (json.dumps(followups), lease, now + duration, now, job_id))
        hub.event(db, method, agent, {'job_id': job_id, 'reopened': row['state'] == 'DONE'})
        return {'accepted': True, 'job': view(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), agent),
                'locks_unchanged': True, 'activity_reminder': 'Update your short current-work label with board_activity.'}
    if method == 'job_claim':
        if row['state'] != 'OPEN':
            return {'accepted': False, 'job': view(row, agent)}
        db.execute("UPDATE jobs SET state='CLAIMED',owner=?,lease_token=?,expires=?,updated=? WHERE id=?",
                   (agent, secrets.token_hex(16), now + ttl(p), now, job_id))
    else:
        if row['state'] != 'CLAIMED' or row['owner'] != agent or not secrets.compare_digest(row['lease_token'] or '', str(p.get('lease_token', ''))):
            raise BoardError('Current job owner and lease_token required; expired or superseded workers must stop')
        if method == 'job_update':
            progress = text(p.get('progress'), 'progress')
            db.execute('UPDATE jobs SET progress=?,expires=?,updated=? WHERE id=?', (progress, now + ttl(p), now, job_id))
        elif method == 'job_yield':
            progress = text(p.get('reason'), 'reason')
            db.execute('UPDATE jobs SET progress=? WHERE id=?', (progress, job_id))
            reopen(hub, db, row, now, progress)
        elif method == 'job_finish':
            summary = text(p.get('summary'), 'summary')
            sha = commit(p.get('commit_hash')) if row['kind'] == 'code' else None
            if row['kind'] == 'noncode' and p.get('commit_hash') is not None:
                sha = commit(p['commit_hash'])
            checks = p.get('checks')
            if not isinstance(checks, list) or not 1 <= len(checks) <= 30:
                raise BoardError('checks must be 1 to 30 objects: {"command":"python -m unittest","result":"passed","evidence":"Tests passed"}')
            normalized = []
            for index, check in enumerate(checks):
                if not isinstance(check, dict) or check.get('result') not in ('passed', 'not_applicable'):
                    raise BoardError('checks[] requires command, result (passed or not_applicable), evidence. Example: {"command":"python -m unittest","result":"passed","evidence":"Tests passed"}')
                normalized.append({'command': text(check.get('command'), f'checks[{index}].command', 2000),
                                   'result': check['result'], 'evidence': text(check.get('evidence'), f'checks[{index}].evidence', 4000)})
            db.execute("UPDATE jobs SET state='DONE',summary=?,commit_hash=?,checks=?,lease_token=NULL,expires=NULL,updated=? WHERE id=?",
                       (summary, sha, json.dumps(normalized), now, job_id))
        else:
            raise BoardError('Unknown job operation')
    hub.event(db, method, agent, {'job_id': job_id})
    result = {'accepted': True, 'job': view(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), agent),
            'locks_unchanged': True, 'evidence_verified': False}
    if method == 'job_claim':
        result['activity_reminder'] = 'Update your short current-work label with board_activity.'
    return result
