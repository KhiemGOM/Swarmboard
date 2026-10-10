"""Local /board counterpart: inspect state or retire an agent without model quota."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from board.core import BoardError, Hub
from board.runtime import ensure_hub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['start', 'status', 'mutex', 'jobs', 'retire'], default='start')
    parser.add_argument('agent_id', nargs='?', help='Exact agent ID for retire; terminal cannot infer a chat identity')
    parser.add_argument('--reason', default='Owner declared this agent unavailable')
    parser.add_argument('--workspace')
    parser.add_argument('--database')
    parser.add_argument('--port', type=int)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parent / '.board/operator.json')
    args = parser.parse_args()
    try:
        saved = json.loads(args.config.read_text()) if args.config.exists() else {}
        # Explicit workspace selects its own state; never silently reuse another project's DB.
        workspace = Path(args.workspace or saved.get('workspace') or Path.cwd()).resolve()
        database = Path(args.database or (None if args.workspace else saved.get('database')) or workspace / '.board/hub.sqlite').resolve()
        port = args.port or saved.get('port', 4317)
        if args.command == 'start':
            print(json.dumps(ensure_hub(f'http://127.0.0.1:{port}', workspace, database, autostart=True)))
            return
        if not database.is_file():
            raise BoardError('State database does not exist; pass --database or install/configure this workspace first')
        with closing(sqlite3.connect(f'{database.as_uri()}?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            configured = db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()
            if not configured or Path(configured[0]).resolve() != workspace:
                raise BoardError('Database belongs to a different workspace; refusing operation')
            if args.command == 'retire':
                if not args.agent_id or args.agent_id == 'self':
                    raise BoardError('Terminal retirement needs an exact agent ID from board status; self is only meaningful inside /board retire')
            else:
                if args.agent_id:
                    raise BoardError('agent_id is only accepted with retire')
                if args.command == 'jobs':
                    query = 'SELECT id,title,state,owner,previous_owner,expires,progress,summary,commit_hash FROM jobs ORDER BY id'
                elif args.command == 'mutex':
                    query = 'SELECT id,agent,path,kind,reason,expires FROM claims WHERE active=1 ORDER BY id'
                else:
                    query = 'SELECT id,name,state,seen,reason FROM agents ORDER BY started'
                print(json.dumps({'workspace': str(workspace), args.command: [dict(r) for r in db.execute(query)]}, indent=2))
                return
        print(json.dumps(Hub(database, workspace).retire_local(args.agent_id, args.reason), indent=2))
    except (BoardError, OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
