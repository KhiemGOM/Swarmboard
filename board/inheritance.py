"""One-use context handoff proof and atomic inheritance into a fresh identity."""
import hashlib
import secrets

from .core import BoardError
from .jobs import text, transfer, view

OWNER_OVERRIDE = 'OWNER_EXPLICITLY_REQUESTED_INHERITANCE'


def issue(db, agent):
    secret = secrets.token_urlsafe(12)  # 96 bits; short enough for handoff context.
    db.execute('UPDATE agents SET inheritance_hash=? WHERE id=?', (digest(secret), agent))
    return secret


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def inherit(hub, db, p, agent, now):
    target = p.get('target_agent_id')
    reason = text(p.get('reason'), 'inheritance reason')
    if not isinstance(target, str) or not target or target == agent:
        raise BoardError('Inheritance requires a different exact predecessor agent ID')
    old = db.execute('SELECT * FROM agents WHERE id=?', (target,)).fetchone()
    if not old:
        raise BoardError('Unknown predecessor agent ID')
    if old['successor_id']:
        raise BoardError('Predecessor was already inherited; use its current successor instead')
    proof, override = p.get('inheritance_secret'), p.get('owner_override')
    if override is not None:
        if override != OWNER_OVERRIDE or proof is not None:
            raise BoardError('Use a secret or the explicit owner override, not both')
    elif not isinstance(proof, str) or not old['inheritance_hash'] or not secrets.compare_digest(digest(proof), old['inheritance_hash']):
        raise BoardError('Invalid inheritance secret. An explicit owner-requested override is required if the handoff secret is lost')
    # Do not revive expired/released mutexes or take work already adopted by peers.
    claims = [row['id'] for row in db.execute('SELECT id FROM claims WHERE agent=? AND active=1', (target,))]
    db.execute('UPDATE claims SET agent=? WHERE agent=? AND active=1', (agent, target))
    custody = [row['path'] for row in db.execute("SELECT path FROM custody WHERE owner=? AND state!='COMMITTED'", (target,))]
    db.execute("UPDATE custody SET owner=?,updated=? WHERE owner=? AND state!='COMMITTED'", (agent, now, target))
    transfer(hub, db, target, agent, now)
    for row in db.execute("SELECT id FROM jobs WHERE state='OPEN' AND previous_owner=?", (target,)).fetchall():
        db.execute("UPDATE jobs SET state='CLAIMED',owner=?,lease_token=?,expires=?,updated=? WHERE id=?",
                   (agent, secrets.token_hex(16), now + 1800, now, row['id']))
    inbox = db.execute('UPDATE messages SET recipient=? WHERE recipient=? AND acknowledged IS NULL', (agent, target)).rowcount
    db.execute("UPDATE agents SET state='retired',reason=?,disconnected=?,successor_id=?,inheritance_hash=NULL WHERE id=?",
               ('Inherited by ' + agent + ': ' + reason, now, agent, target))
    db.execute('UPDATE agents SET inherited_from=?,inherited_at=? WHERE id=?', (target, now, agent))
    hub.event(db, 'agent_inherited', agent, {'predecessor': target, 'reason': reason,
                                           'owner_override': override is not None, 'claim_ids': claims,
                                           'custody_paths': custody, 'unread_messages_transferred': inbox})
    return {'inherited_from': target, 'successor_agent_id': agent, 'predecessor_retired': True,
            'proof_mode': 'owner_override' if override else 'secret', 'claim_ids': claims,
            'custody_paths': custody, 'unread_messages_transferred': inbox,
            'jobs': [view(row, agent) for row in db.execute("SELECT * FROM jobs WHERE state='CLAIMED' AND owner=? AND previous_owner=?", (agent, target))],
            'instructions': 'Keep your new ID. The predecessor must stop writes. Inspect inherited work and obtain fresh claims for expired/released paths. Existing live mutex expiry is preserved; inheritance does not certify completion or terminate OS processes.'}
