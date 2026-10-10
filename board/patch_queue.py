"""Shared preparation, serialized patch acceptance, journaled file publication."""
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path

from .core import BoardError

LIVE = ('OPEN', 'READY', 'APPLYING')
MAX_FILE = 1024 * 1024


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS patch_batches (
            id INTEGER PRIMARY KEY, path TEXT, base TEXT, candidate TEXT,
            base_hash TEXT, state TEXT, reason TEXT, created REAL, updated REAL);
        CREATE UNIQUE INDEX IF NOT EXISTS patch_live_path ON patch_batches(path)
            WHERE state IN ('OPEN','READY','APPLYING');
        CREATE TABLE IF NOT EXISTS patch_members (
            batch_id INTEGER, agent TEXT, state TEXT, joined REAL, released REAL,
            PRIMARY KEY(batch_id,agent));
        CREATE TABLE IF NOT EXISTS patches (
            id INTEGER PRIMARY KEY, batch_id INTEGER, agent TEXT, diff TEXT,
            edits TEXT, state TEXT, created REAL, updated REAL, commit_hash TEXT);
        CREATE TABLE IF NOT EXISTS custody_age (path TEXT PRIMARY KEY, since REAL);
    ''')
    db.execute("INSERT OR IGNORE INTO custody_age SELECT path,updated FROM custody WHERE state!='COMMITTED'")


def digest(content):
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def file_path(hub, relative):
    path = hub.workspace / relative
    # Revalidate components before each write; queues support ordinary files only.
    for component in [path, *path.parents]:
        if component == hub.workspace:
            break
        if component.is_symlink() or (component.exists() and getattr(component.stat(), 'st_file_attributes', 0) & 0x400):
            raise BoardError('Patch queues do not follow symbolic links or reparse points')
    if not path.is_file():
        raise BoardError('Patch queues require an existing regular UTF-8 file')
    if path.stat().st_size > MAX_FILE:
        raise BoardError('Patch queue file exceeds 1 MiB')
    return path


def read_file(hub, relative):
    try:
        raw = file_path(hub, relative).read_bytes()
        text = raw.decode('utf-8')
    except (OSError, UnicodeError) as exc:
        raise BoardError('Cannot read patch file as UTF-8: ' + str(exc)) from exc
    if '\x00' in text:
        raise BoardError('Binary files are not supported by patch queues')
    return text


def proposed(base, patch, path):
    """Strict single-file unified diff, with exact base/context and hunk counts."""
    if not isinstance(patch, str) or not patch or len(patch) > 32000:
        raise BoardError('patch must be a single-file unified diff of 1 to 32000 characters')
    lines = patch.splitlines(keepends=True)
    if lines and lines[0].startswith('diff --git '):
        lines.pop(0)
    if len(lines) < 3 or not lines[0].startswith('--- ') or not lines[1].startswith('+++ '):
        raise BoardError('Use --- a/path and +++ b/path unified diff headers')
    for line, prefix in zip(lines[:2], ('a/', 'b/')):
        name = line[4:].rstrip('\r\n').split('\t')[0]
        canonical = lambda value: os.path.normcase(value).replace('\\', '/')
        if canonical(name) not in (canonical(path), canonical(prefix + path)):
            raise BoardError('Patch headers must identify the exact queued file; no rename or multiple files')
    source, output, cursor, index = base.splitlines(keepends=True), [], 0, 2
    edits = []
    while index < len(lines):
        match = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\r\n]*[\r\n]*', lines[index])
        if not match:
            raise BoardError('Invalid unified diff hunk header')
        old_start, old_count, new_start, new_count = (int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1))
        start = old_start - 1 if old_count else old_start
        if start < cursor or start > len(source):
            raise BoardError('Overlapping or out-of-range diff hunks')
        output.extend(source[cursor:start])
        if (new_start - 1 if new_count else new_start) != len(output):
            raise BoardError('Incorrect new-file hunk position')
        index += 1
        operations = []
        while index < len(lines) and not lines[index].startswith('@@ '):
            line = lines[index]
            if line.startswith('\\ No newline at end of file'):
                if not operations or not operations[-1][1].endswith('\n'):
                    raise BoardError('Invalid no-newline marker')
                operations[-1] = (operations[-1][0], operations[-1][1].removesuffix('\n').removesuffix('\r'))
            elif line[:1] in (' ', '+', '-'):
                operations.append((line[0], line[1:]))
            else:
                raise BoardError('Invalid hunk content; only one file is accepted')
            index += 1
        old = [text for kind, text in operations if kind != '+']
        new = [text for kind, text in operations if kind != '-']
        normalized = lambda values: [value.replace('\r\n', '\n') for value in values]
        if len(old) != old_count or len(new) != new_count or normalized(source[start:start + old_count]) != normalized(old):
            raise BoardError('Patch context/count does not match the recorded base')
        # Keep the submitter's exact base positions. Re-diffing whole files can
        # relocate edits across repeated identical lines and hide a collision.
        position, change_start, replacement = start, None, []
        for kind, value in operations:
            if kind == ' ':
                if change_start is not None:
                    edits.append([change_start, position, replacement])
                    change_start, replacement = None, []
                position += 1
            else:
                if change_start is None:
                    change_start = position
                if kind == '-':
                    position += 1
                else:
                    replacement.append(value)
        if change_start is not None:
            edits.append([change_start, position, replacement])
        output.extend(new)
        cursor = start + old_count
    output.extend(source[cursor:])
    # Context matching ignores CRLF/LF differences. Retain original bytes on
    # untouched lines; replacements inherit their base lines' endings and new
    # lines use the nearest base ending. No-newline markers remain meaningful.
    def ending(value):
        return '\r\n' if value.endswith('\r\n') else '\n' if value.endswith('\n') else None
    fallback = next((ending(value) for value in source if ending(value)), '\n')
    for edit in edits:
        start, end, replacement = edit
        nearby = (ending(source[start]) if start < len(source) else
                  ending(source[start - 1]) if start else None) or fallback
        for index, value in enumerate(replacement):
            if value.endswith('\n'):
                original = ending(source[start + index]) if start + index < end else None
                replacement[index] = value.removesuffix('\n').removesuffix('\r') + (original or nearby)
    result = compose(base, edits)
    if '\x00' in result or len(result.encode('utf-8')) > MAX_FILE:
        raise BoardError('Candidate must be UTF-8 text at most 1 MiB')
    return result, [edit for edit in edits if source[edit[0]:edit[1]] != edit[2]]


def overlaps(a, b):
    x, y = a[:2], b[:2]
    if x[0] == x[1]:
        return y[0] <= x[0] <= y[1]
    if y[0] == y[1]:
        return x[0] <= y[0] <= x[1]
    return max(x[0], y[0]) < min(x[1], y[1])


def compose(base, edits):
    source, result, cursor = base.splitlines(keepends=True), [], 0
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1])):
        result.extend(source[cursor:start])
        result.extend(replacement)
        cursor = end
    result.extend(source[cursor:])
    candidate = ''.join(result)
    if len(candidate.encode('utf-8')) > MAX_FILE:
        raise BoardError('Combined candidate exceeds 1 MiB')
    return candidate


def summary(db, batch):
    return {key: batch[key] for key in ('id', 'path', 'base_hash', 'state', 'reason', 'created', 'updated')} | {
        'candidate_hash': digest(batch['candidate']),
        'participants': [dict(r) for r in db.execute('SELECT agent,state FROM patch_members WHERE batch_id=? ORDER BY joined,agent', (batch['id'],))],
        'patches': [dict(r) for r in db.execute('SELECT id,agent,state,commit_hash FROM patches WHERE batch_id=? ORDER BY id', (batch['id'],))],
    }


def ready(db, batch_id, now):
    if not db.execute("SELECT 1 FROM patch_members WHERE batch_id=? AND state='ACTIVE'", (batch_id,)).fetchone():
        db.execute("UPDATE patch_batches SET state='READY',updated=? WHERE id=? AND state='OPEN'", (now, batch_id))


def retire_member(db, agent, now):
    ids = [r[0] for r in db.execute("SELECT batch_id FROM patch_members WHERE agent=? AND state='ACTIVE'", (agent,))]
    db.execute("UPDATE patch_members SET state='WITHDRAWN',released=? WHERE agent=? AND state='ACTIVE'", (now, agent))
    for batch_id in ids:
        ready(db, batch_id, now)


def transfer(db, old, new):
    for member in db.execute('SELECT * FROM patch_members WHERE agent=?', (old,)).fetchall():
        existing = db.execute('SELECT state FROM patch_members WHERE batch_id=? AND agent=?', (member['batch_id'], new)).fetchone()
        if existing:
            if member['state'] == 'ACTIVE':
                db.execute("UPDATE patch_members SET state='ACTIVE',released=NULL WHERE batch_id=? AND agent=?", (member['batch_id'], new))
            db.execute('DELETE FROM patch_members WHERE batch_id=? AND agent=?', (member['batch_id'], old))
        else:
            db.execute('UPDATE patch_members SET agent=? WHERE batch_id=? AND agent=?', (new, member['batch_id'], old))
    db.execute("UPDATE patches SET agent=? WHERE agent=? AND state!='COMMITTED'", (new, old))


def dispatch(hub, db, p, agent, now):
    action = p.get('action')
    if action == 'join':
        path = hub.path(p.get('path'))
        # Reject lexical aliases too; hub.path resolves symlinks.
        raw = Path(p.get('path'))
        raw = raw if raw.is_absolute() else hub.workspace / raw
        if raw.resolve() != raw.absolute():
            raise BoardError('Use the canonical file path, without links or parent traversal')
        base = read_file(hub, path)
        from .claims import intersects
        conflicts = [dict(r) for r in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file'") if intersects(r, path)]
        if conflicts:
            return {'accepted': False, 'exclusive_claims': conflicts}
        batch = db.execute("SELECT * FROM patch_batches WHERE path=? AND state IN ('OPEN','READY','APPLYING')", (path,)).fetchone()
        if batch and batch['state'] != 'OPEN':
            raise BoardError('This batch is finalizing; retry after its final result')
        if batch and base != batch['base']:
            raise BoardError('Shared file changed outside the queue; existing batch must finish blocked before a new batch')
        if not batch:
            reason = p.get('reason')
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
                raise BoardError('A reason of 1 to 1000 characters is required')
            batch_id = db.execute("INSERT INTO patch_batches(path,base,candidate,base_hash,state,reason,created,updated) VALUES (?,?,?,?,'OPEN',?,?,?)",
                                  (path, base, base, digest(base), reason, now, now)).lastrowid
            batch = db.execute('SELECT * FROM patch_batches WHERE id=?', (batch_id,)).fetchone()
        existing = db.execute('SELECT state FROM patch_members WHERE batch_id=? AND agent=?', (batch['id'], agent)).fetchone()
        if existing and existing['state'] != 'ACTIVE':
            raise BoardError('Already released this batch; join the next batch after finalization')
        count = db.execute('SELECT COUNT(*) FROM patch_members WHERE batch_id=?', (batch['id'],)).fetchone()[0]
        if not existing and count >= 32:
            raise BoardError('A batch supports at most 32 participants')
        db.execute("INSERT OR IGNORE INTO patch_members VALUES (?,?,'ACTIVE',?,NULL)", (batch['id'], agent, now))
        return {'accepted': True, 'batch': summary(db, batch), 'instructions': 'Read the recorded base with patch_queue action=read, content=base. Submit a unified diff against that base. Never edit the queued shared file directly. Release when done; finalization is automatic.'}
    if action == 'record_commit':
        from .claims import commit
        ids = p.get('patch_ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 32 or any(type(i) is not int for i in ids):
            raise BoardError('patch_ids must contain 1 to 32 integer IDs')
        sha = commit(p.get('commit_hash'))
        for patch_id in ids:
            row = db.execute('SELECT agent,state FROM patches WHERE id=?', (patch_id,)).fetchone()
            if not row or row['agent'] != agent or row['state'] != 'APPLIED':
                raise BoardError('Only your applied patch custody can be resolved with commit evidence')
        db.executemany("UPDATE patches SET state='COMMITTED',commit_hash=?,updated=? WHERE id=?", [(sha, now, i) for i in ids])
        return {'resolved_patch_ids': ids, 'commit_hash': sha}
    batch_id = p.get('batch_id')
    if type(batch_id) is not int:
        raise BoardError('batch_id must be an integer')
    batch = db.execute('SELECT * FROM patch_batches WHERE id=?', (batch_id,)).fetchone()
    if not batch:
        raise BoardError('Unknown patch batch')
    if action == 'read':
        result = {'batch': summary(db, batch)}
        content = p.get('content')
        if content is not None:
            if content not in ('base', 'candidate', 'patch'):
                raise BoardError('content must be base, candidate or patch')
            value = batch[content] if content != 'patch' else None
            if content == 'patch':
                row = db.execute('SELECT diff FROM patches WHERE id=? AND batch_id=?', (p.get('patch_id'), batch_id)).fetchone()
                if not row:
                    raise BoardError('patch_id must belong to this batch')
                value = row[0]
            offset, limit = p.get('offset', 0), p.get('limit', 8000)
            if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
                raise BoardError('offset must be nonnegative and limit must be 1 to 8000 characters')
            result.update(content=value[offset:offset + limit], offset=offset, total_chars=len(value), next_offset=min(len(value), offset + limit))
        return result
    member = db.execute('SELECT state FROM patch_members WHERE batch_id=? AND agent=?', (batch_id, agent)).fetchone()
    if batch['state'] != 'OPEN' or not member or member[0] != 'ACTIVE':
        raise BoardError('Join this open batch before submitting/releasing; released participants cannot write')
    if action == 'submit':
        if read_file(hub, batch['path']) != batch['base']:
            raise BoardError('Shared file changed outside the queue; do not submit more patches. Incident alert sent on the board check; preserve changes and reconcile.')
        if p.get('base_hash') != batch['base_hash']:
            raise BoardError('base_hash must match the immutable batch base')
        proposed_content, edits = proposed(batch['base'], p.get('patch'), batch['path'])
        if not edits or proposed_content == batch['base']:
            raise BoardError('Patch contains no changes')
        if compose(batch['base'], edits) != proposed_content:
            raise BoardError('Patch hunks have ambiguous ordering; use a canonical unified diff')
        own = list(db.execute('SELECT id FROM patches WHERE batch_id=? AND agent=?', (batch_id, agent)))
        if len(own) > 1:
            raise BoardError('Inherited multiple patches in this batch; release them and use a later batch to revise')
        others = list(db.execute('SELECT id,agent,edits FROM patches WHERE batch_id=? AND agent!=?', (batch_id, agent)))
        conflicts = [{'patch_id': row['id'], 'agent': row['agent']} for row in others
                     if any(overlaps(a, b) for a in edits for b in json.loads(row['edits']))]
        if conflicts:
            return {'accepted': False, 'batch_id': batch_id, 'conflicts': conflicts, 'reason': 'Overlapping base-line edits; your previously accepted patch (if any) is unchanged. Revise against the same base.'}
        candidate = compose(batch['base'], edits + [edit for row in others for edit in json.loads(row['edits'])])
        if own:
            patch_id = own[0]['id']
            db.execute('UPDATE patches SET diff=?,edits=?,updated=? WHERE id=?', (p['patch'], json.dumps(edits), now, patch_id))
        else:
            patch_id = db.execute("INSERT INTO patches(batch_id,agent,diff,edits,state,created,updated) VALUES (?,?,?,?,'QUEUED',?,?)",
                                  (batch_id, agent, p['patch'], json.dumps(edits), now, now)).lastrowid
        db.execute('UPDATE patch_batches SET candidate=?,updated=? WHERE id=?', (candidate, now, batch_id))
        hub.event(db, 'patch_accepted', agent, {'batch_id': batch_id, 'patch_id': patch_id})
        return {'accepted': True, 'batch_id': batch_id, 'patch_id': patch_id, 'candidate_hash': digest(candidate), 'instructions': 'Accepted into candidate only. Release when done. Inspect and run targeted tests after the one final inbox notification.'}
    if action in ('release', 'withdraw'):
        db.execute('UPDATE patch_members SET state=?,released=? WHERE batch_id=? AND agent=?', ('RELEASED' if action == 'release' else 'WITHDRAWN', now, batch_id, agent))
        ready(db, batch_id, now)
        return {'batch_id': batch_id, 'accepted_patches_preserved': True}
    raise BoardError('Unknown patch_queue action')


def finalize_pending(hub):
    """READY is durable before any write. Recover APPLYING after a crash."""
    while True:
        with hub.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            batch = db.execute("SELECT * FROM patch_batches WHERE state IN ('READY','APPLYING') ORDER BY id LIMIT 1").fetchone()
            if not batch:
                return
            if batch['state'] == 'READY':
                db.execute("UPDATE patch_batches SET state='APPLYING',updated=? WHERE id=?", (time.time(), batch['id']))
                continue  # commit journal before touching the shared file
            temp = None
            try:
                path = file_path(hub, batch['path'])
                current = read_file(hub, batch['path'])
                if current != batch['candidate']:
                    if current != batch['base']:
                        raise BoardError('Shared file differs from recorded base; no overwrite performed. Candidate preserved.')
                    descriptor, temp = tempfile.mkstemp(prefix='.swarmboard-', dir=path.parent)
                    with os.fdopen(descriptor, 'wb') as stream:
                        stream.write(batch['candidate'].encode('utf-8'))
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
                    # Cooperative writers hold the queue lock; detect outsiders
                    # immediately before replacement too (not an OS-wide CAS).
                    if read_file(hub, batch['path']) != batch['base']:
                        raise BoardError('Shared file changed during finalization; candidate preserved')
                    os.replace(temp, path)
                state, reason = 'APPLIED', 'Candidate published. Inspect your changes and run targeted tests; acceptance is not validation.'
            except (BoardError, OSError, UnicodeError) as exc:
                state, reason = 'BLOCKED', str(exc)[:1000]
            finally:
                if temp and os.path.exists(temp):
                    os.unlink(temp)
            now = time.time()
            db.execute('UPDATE patch_batches SET state=?,reason=?,updated=? WHERE id=?', (state, reason, now, batch['id']))
            db.execute('UPDATE patches SET state=?,updated=? WHERE batch_id=?', ('APPLIED' if state == 'APPLIED' else 'BLOCKED', now, batch['id']))
            for member in db.execute('SELECT agent FROM patch_members WHERE batch_id=?', (batch['id'],)).fetchall():
                ids = [r[0] for r in db.execute('SELECT id FROM patches WHERE batch_id=? AND agent=?', (batch['id'], member[0]))]
                body = f"Patch queue #{batch['id']} finalized: {state}. File: {batch['path']}. Your patch IDs: {ids}. {reason} Read patch_queue action=read for the combined result. Send failure evidence to relevant contributors through inbox_send when authorized. Record actual commit evidence with action=record_commit after committing."
                db.execute("INSERT INTO messages(sender,recipient,body,created) VALUES ('system',?,?,?)", (member[0], body, now))
            hub.event(db, 'patch_finalized', 'system', {'batch_id': batch['id'], 'state': state})
