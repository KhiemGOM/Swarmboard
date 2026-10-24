"""Local /board counterpart: inspect state or retire an agent without model quota."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse

from board.core import BoardError, Hub
from board.runtime import ROUTE, ensure_hub, workspace_connection


def operator_connection(args):
    """Keep workspace, database and port together; never merge global routes."""
    if args.config is not None:
        if not args.config.is_file():
            raise BoardError('--config must identify an existing operator configuration')
        saved = json.loads(args.config.read_text(encoding='utf-8'))
        if not isinstance(saved, dict) or not saved.get('workspace'):
            raise BoardError('--config requires workspace, database and port')
        workspace = Path(args.workspace or saved['workspace']).resolve()
        if workspace != Path(saved['workspace']).resolve():
            raise BoardError('--workspace conflicts with --config workspace; refusing mixed project routing')
        workspace, database, url = workspace_connection(str(workspace), args.database or saved.get('database'))
        port = args.port if args.port is not None else saved.get('port', urlparse(url).port)
    else:
        workspace, database, url = workspace_connection(args.workspace, args.database)
        port = args.port if args.port is not None else urlparse(url).port
    if type(port) is not int or not 1 <= port <= 65535:
        raise BoardError('port must be an integer between 1 and 65535')
    return Path(workspace), Path(database), port


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['start', 'status', 'release', 'mutex', 'jobs', 'retire', 'wake', 'configure', 'cleanup'], default='start')
    parser.add_argument('agent_id', nargs='?', help='Exact agent ID for retire; terminal cannot infer a chat identity')
    parser.add_argument('--reason', default='Owner declared this agent unavailable')
    parser.add_argument('--workspace')
    parser.add_argument('--mcp-config', type=Path, help='Codex config.toml path for configure')
    parser.add_argument('--database')
    parser.add_argument('--port', type=int)
    parser.add_argument('--thread-id')
    parser.add_argument('--socket')
    parser.add_argument('--codex', default='codex')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--include-existing', action='store_true')
    parser.add_argument('--config', type=Path, default=None)
    args = parser.parse_args()
    try:
        if args.command == 'cleanup':
            if args.agent_id:
                raise BoardError('cleanup does not accept an agent ID')
            if args.config:
                raise BoardError('cleanup uses workspace-local routing; use --workspace or --database')
            from board.runtime import workspace_connection
            from board.file_lifecycle import cleanup_local
            workspace, database, _ = workspace_connection(args.workspace, args.database)
            database = Path(database)
            if not database.is_file():
                raise BoardError('State database does not exist; cleanup will not create a board')
            print(json.dumps(cleanup_local(Hub(database, workspace)), indent=2))
            return
        if args.command == 'configure':
            if not args.workspace:
                raise BoardError('configure requires --workspace')
            if args.agent_id:
                raise BoardError('configure does not accept an agent ID')
            from board.configure import configure
            workspace = Path(args.workspace).resolve()
            database = Path(args.database).resolve() if args.database else workspace / '.board/hub.sqlite'
            print(json.dumps(configure(workspace, database, args.port if args.port is not None else 4317,
                                       args.config or workspace / ROUTE,
                                       Path(__file__).resolve().parent, args.mcp_config), indent=2))
            return
        workspace, database, port = operator_connection(args)
        if args.command == 'wake':
            from board.codex_wake import run
            print(json.dumps(run(database, workspace, args.agent_id, args.thread_id,
                                 args.codex, args.socket, args.once, args.include_existing)))
            return
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
                elif args.command in ('release', 'mutex'):
                    query = 'SELECT id,agent,path,kind,reason,expires FROM claims WHERE active=1 ORDER BY id'
                else:
                    query = 'SELECT id,name,state,seen,reason FROM agents ORDER BY started'
                result = {'workspace': str(workspace), args.command: [dict(r) for r in db.execute(query)]}
                if args.command in ('release', 'mutex'):
                    result['patch_queues'] = [dict(r) for r in db.execute(
                        "SELECT b.id AS batch_id,b.path,b.state,m.agent FROM patch_batches b JOIN patch_members m ON m.batch_id=b.id WHERE m.state='ACTIVE' AND b.state IN ('OPEN','READY','APPLYING') ORDER BY b.id,m.agent")]
                print(json.dumps(result, indent=2))
                return
        print(json.dumps(Hub(database, workspace).retire_local(args.agent_id, args.reason), indent=2))
    except (BoardError, OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
