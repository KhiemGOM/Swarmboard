"""One-time, idempotent import of a Markdown board tail into SQLite notices."""
import argparse
import hashlib
import json
import secrets
import time
from pathlib import Path
from board.core import BoardError, Hub


def migrate(hub, source, archive, limit=30, retire=False):
    source = Path(source).resolve()
    if not source.is_relative_to(hub.workspace) or not source.is_file():
        raise BoardError("Source must be an existing file inside the coordinated workspace")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise BoardError("lines must be between 1 and 1000")
    original = source.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    lines = original.decode("utf-8-sig").splitlines()
    tail = lines[-limit:]
    archive = Path(archive).resolve()
    archive.mkdir(parents=True, exist_ok=True)
    snapshot = archive / (source.name + "." + digest[:16] + ".archive")
    snapshot.write_bytes(original)
    key = hashlib.sha256((str(source) + digest + str(limit)).encode()).hexdigest()
    with hub.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS notice_imports (key TEXT PRIMARY KEY, source TEXT, sha256 TEXT, source_lines INTEGER, post_ids TEXT, imported REAL)")
        previous = db.execute("SELECT post_ids FROM notice_imports WHERE key=?", (key,)).fetchone()
        if previous:
            ids = json.loads(previous[0])
        else:
            actor, now = secrets.token_hex(8), time.time()
            db.execute("INSERT INTO agents(id,name,token,state,started,seen,reason) VALUES (?, 'noticeboard-migration', ?, 'retired', ?, ?, 'One-time historical import')",
                       (actor, secrets.token_urlsafe(32), now, now))
            ids = []
            for offset, line in enumerate(tail, start=max(1, len(lines) - limit + 1)):
                if not line.strip():
                    continue
                body = f"Imported history from {source.name}, line {offset}. Original labels are historical attribution, not authenticated agent IDs or current locks.\n{line}"
                if len(body) > 16000:
                    raise BoardError("A source entry is too long to import as one notice")
                ids.append(db.execute("INSERT INTO posts(agent,body,created) VALUES (?,?,?)", (actor, body, now)).lastrowid)
            db.execute("INSERT INTO notice_imports VALUES (?,?,?,?,?,?)", (key, str(source), digest, len(tail), json.dumps(ids), now))
            hub.event(db, "noticeboard_import", actor, {"sha256": digest, "source_lines": len(tail), "post_ids": ids})
        for post_id in ids:
            if not db.execute("SELECT 1 FROM posts WHERE id=?", (post_id,)).fetchone():
                raise BoardError("Imported notice verification failed; source retained")
    if retire:
        if source.read_bytes() != original:
            raise BoardError("Source changed during import; archive preserved, but source was not retired. Retry.")
        source.unlink()
    return {"source_lines": len(tail), "imported_entries": len(ids), "post_ids": ids,
            "archive": str(snapshot), "sha256": digest, "retired": retire, "already_imported": previous is not None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--database", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--archive", required=True)
    parser.add_argument("--lines", type=int, default=30)
    parser.add_argument("--retire-source", action="store_true")
    args = parser.parse_args()
    try:
        result = migrate(Hub(args.database, args.workspace), args.source, args.archive, args.lines, args.retire_source)
        print(json.dumps(result, indent=2))
    except (BoardError, OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
