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
    if method == 'job_post':
        title = text(p.get('title'), 'title', 200)
        description = text(p.get('description'), 'description')
        acceptance = text(p.get('acceptance'), 'acceptance')
        kind = p.get('kind', 'code')
        if kind not in ('code', 'noncode'):
            raise BoardError('kind must be code or noncode')
        parent = p.get('parent_job_id')
        if parent is not None and (type(parent) is not int or not db.execute('SELECT 1 FROM jobs WHERE id=?', (parent,)).fetchone()):
            raise BoardError('parent_job_id must identify an existing job')
        job_id = db.execute("INSERT INTO jobs(title,description,acceptance,kind,state,creator,parent_job_id,created,updated) VALUES (?,?,?,?,'OPEN',?,?,?,?)",
                            (title, description, acceptance, kind, agent, parent, now, now)).lastrowid
        hub.event(db, method, agent, {'job_id': job_id})
        return {'job': view(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), agent)}
    if method == 'job_list':
        state = p.get('state')
        if state is not None and state not in ('OPEN', 'CLAIMED', 'DONE'):
            raise BoardError('state must be OPEN, CLAIMED or DONE')
        after = p.get('after_id', 0)
        if type(after) is not int or after < 0:
            raise BoardError('after_id must be a non-negative integer')
        rows = db.execute('SELECT * FROM jobs WHERE id>? AND (? IS NULL OR state=?) ORDER BY id LIMIT 100', (after, state, state))
        return {'jobs': [view(row, agent) for row in rows]}
    job_id = p.get('job_id')
    if type(job_id) is not int:
        raise BoardError('job_id must be an integer')
    row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise BoardError('Unknown job')
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
                raise BoardError('checks must contain 1 to 30 evidence records')
            normalized = []
            for check in checks:
                if not isinstance(check, dict) or check.get('result') not in ('passed', 'not_applicable'):
                    raise BoardError('Each check needs result passed or not_applicable; failed checks cannot finish a job')
                normalized.append({'command': text(check.get('command'), 'check command', 2000),
                                   'result': check['result'], 'evidence': text(check.get('evidence'), 'check evidence', 4000)})
            db.execute("UPDATE jobs SET state='DONE',summary=?,commit_hash=?,checks=?,lease_token=NULL,expires=NULL,updated=? WHERE id=?",
                       (summary, sha, json.dumps(normalized), now, job_id))
        else:
            raise BoardError('Unknown job operation')
    hub.event(db, method, agent, {'job_id': job_id})
    return {'accepted': True, 'job': view(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone(), agent),
            'locks_unchanged': True, 'evidence_verified': False}
