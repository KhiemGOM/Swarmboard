"""Close verified ignored, untracked scratch custody while retaining an audit."""
import json
import subprocess
from .core import BoardError


def ignored_paths(hub, paths):
    if not paths or not (hub.workspace / '.git').exists():
        return set()
    valid = [path for path in paths if path != '.']
    for path in valid:
        hub.path(path)
    probes = valid + [path.rstrip('/') + '/' for path in valid if not (hub.workspace / path).is_file()]
    options = {'cwd': hub.workspace, 'capture_output': True, 'timeout': 3}
    try:
        excluded = subprocess.run(['git', 'check-ignore', '-z', '--stdin'], input=('\0'.join(probes) + '\0').encode('utf-8'), **options)
        candidates = {path.rstrip('/') for path in excluded.stdout.decode('utf-8', errors='surrogateescape').split('\0') if path} if excluded.returncode in (0, 1) else set()
        if not candidates:
            return set()
        tracked = subprocess.run(['git', '--literal-pathspecs', 'ls-files', '-z', '--', *sorted(candidates)], **options)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if tracked.returncode != 0:
        return set()
    names = tracked.stdout.decode('utf-8', errors='surrogateescape').split('\0')
    return {path for path in candidates if not any(name == path or name.startswith(path.rstrip('/') + '/') for name in names)}


def ignored(hub, path):
    return path in ignored_paths(hub, [path])


def close(hub, db, agent, now, paths=None, strict=False):
    db.execute('CREATE TABLE IF NOT EXISTS scratch_completions (id INTEGER PRIMARY KEY,path TEXT,owner TEXT,previous TEXT,created REAL)')
    rows = db.execute("SELECT * FROM custody WHERE owner=? AND state='HELD'", (agent,)).fetchall()
    selected = [row for row in rows if paths is None or row['path'] in paths]
    done = []
    eligible = ignored_paths(hub, [row['path'] for row in selected])
    from .claims import intersects
    for row in selected:
        path = row['path']
        blocked = any(intersects(c, path) for c in db.execute("SELECT * FROM claims WHERE active=1 AND kind='file'"))
        queued = any(hub.overlaps(q[0], path) for q in db.execute("SELECT path FROM patch_batches WHERE state IN ('OPEN','READY','APPLYING')"))
        patches = any(hub.overlaps(q[0], path) for q in db.execute("SELECT b.path FROM patches p JOIN patch_batches b ON b.id=p.batch_id WHERE p.state='APPLIED'"))
        if blocked or queued or patches or path not in eligible:
            if strict:
                raise BoardError(f'{path}: scratch completion requires an ignored, untracked path without live locks or unfinished patches')
            continue
        db.execute('INSERT INTO scratch_completions(path,owner,previous,created) VALUES (?,?,?,?)', (path, agent, json.dumps(dict(row)), now))
        db.execute('DELETE FROM custody WHERE path=?', (path,))
        db.execute('DELETE FROM custody_age WHERE path=?', (path,))
        hub.event(db, 'scratch_completed', agent, {'path': path, 'reason': 'Verified ignored and untracked; no commit required'})
        done.append(path)
    return done
