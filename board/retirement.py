"""Atomic retirement shared by authenticated agents and a local operator."""
from .core import BoardError
from .jobs import reopen, recover_untracked, text


def retire(hub, db, target, reason, actor, now):
    reason = text(reason, 'retirement reason')
    if not isinstance(target, str) or not target.strip():
        raise BoardError('An exact target agent ID is required')
    original = db.execute('SELECT state FROM agents WHERE id=?', (target,)).fetchone()
    if not original:
        raise BoardError('Unknown target agent ID; display names are not IDs')
    claims = [r['id'] for r in db.execute('SELECT id FROM claims WHERE agent=? AND active=1', (target,))]
    jobs = db.execute("SELECT * FROM jobs WHERE owner=? AND state='CLAIMED'", (target,)).fetchall()
    for row in jobs:
        reopen(hub, db, row, now, reason)
    db.execute("UPDATE custody SET state='HELD',reason=?,updated=? WHERE owner=? AND state!='COMMITTED'",
               ('Agent retired: ' + reason, now, target))
    recover_untracked(hub, db, target, now, reason)
    db.execute('UPDATE claims SET active=0 WHERE agent=? AND active=1', (target,))
    from .patch_queue import retire_member
    retire_member(db, target, now)
    db.execute("UPDATE board_waits SET state='CANCELLED' WHERE agent=? AND state='WAITING'", (target,))
    db.execute("UPDATE agents SET state='retired',reason=?,disconnected=? WHERE id=?", (reason, now, target))
    hub.event(db, 'agent_retired', actor, {'target': target, 'reason': reason, 'released_claim_ids': claims,
                                         'reopened_job_ids': [r['id'] for r in jobs]})
    return {'retired_agent': target, 'already_retired': original['state'] == 'retired',
            'released_claim_ids': claims, 'reopened_job_ids': [r['id'] for r in jobs],
            'available_recovery_job_ids': [r['id'] for r in db.execute("SELECT id FROM jobs WHERE previous_owner=? AND state='OPEN'", (target,))],
            'custody_preserved': True, 'stop_session': actor == target,
            'instructions': 'Retired sessions must stop writes. Other agents can claim OPEN jobs and released paths; inspect existing work and accept unfinished custody. Running processes are not terminated.'}
