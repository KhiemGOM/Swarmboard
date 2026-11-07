import hashlib
import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import swarmboard_hook
from board.core import BoardError, Hub
from board.runtime import ROUTE, default_port, find_project, prepare_route, read_route, workspace_connection


def stored_workspace(database):
    with closing(sqlite3.connect(database)) as db:
        return db.execute("SELECT value FROM settings WHERE key='workspace'").fetchone()[0]


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.old = self.base / 'ClashOfNations'
        self.old.mkdir()
        (self.old / '.git').mkdir()
        workspace, database, _ = prepare_route(self.old)[:3]
        self.hub = Hub(database, workspace)
        self.agent = self.hub.call('register', {'name': 'A'})
        self.hub.call('claim', {**self.agent, 'paths': ['Assets'], 'reason': 'work'})

    def copy(self, name='RustedEmpires'):
        new = self.base / name
        shutil.copytree(self.old, new)
        return new

    def move(self, name='RustedEmpires'):
        new = self.base / name
        self.old.rename(new)
        return new


class MovedProjectTests(Fixture):
    # Basics
    def test_unmoved_project_connects_unchanged(self):
        before = (self.old / ROUTE).read_text()
        self.assertEqual(find_project(self.old / 'Assets'), self.old)
        self.assertEqual(workspace_connection(self.old)[0], str(self.old))
        self.assertEqual((self.old / ROUTE).read_text(), before)

    def test_moved_project_is_adopted_with_its_history(self):
        new = self.move()
        self.assertEqual(find_project(new / 'Assets'), new)
        workspace, database, url = workspace_connection(new)
        self.assertEqual((workspace, database), (str(new), str(new / '.board' / 'hub.sqlite')))
        self.assertEqual(url, f'http://127.0.0.1:{default_port(new)}')
        self.assertEqual(read_route(new)['database'], str(new / '.board' / 'hub.sqlite'))
        self.assertEqual(stored_workspace(database), str(new))
        moved = Hub(database, workspace)
        status = moved.call('board_status', {**self.agent, 'target_agent_id': 'self'})
        self.assertEqual([row['path'] for row in status['claims']], ['assets'])
        with moved.connect() as db:
            event = db.execute("SELECT payload FROM events WHERE kind='workspace_moved'").fetchone()
        self.assertEqual(json.loads(event[0])['previous_workspace'], str(self.old))

    def test_copied_project_gets_its_own_board_and_leaves_the_original_alone(self):
        new = self.copy()
        self.assertEqual(find_project(new), new)
        self.assertNotEqual(workspace_connection(new)[2], workspace_connection(self.old)[2])
        self.assertEqual(stored_workspace(self.old / '.board' / 'hub.sqlite'), str(self.old))
        self.assertEqual(read_route(self.old)['workspace'], str(self.old))

    def test_board_prompt_in_a_copied_project_routes_to_its_own_database(self):
        new = self.copy()
        workspace, database = prepare_route(new)[:2]
        self.assertEqual(database, str(new / '.board' / 'hub.sqlite'))
        self.assertEqual(stored_workspace(database), str(new))

    def test_adoption_after_board_already_rewrote_the_route(self):
        new = self.copy()
        route = {'workspace': str(new), 'database': str(new / '.board' / 'hub.sqlite'), 'port': default_port(new)}
        (new / ROUTE).write_text(json.dumps(route))  # /board ran before this fix: route new, database still old.
        self.assertEqual(workspace_connection(new)[0], str(new))
        self.assertEqual(stored_workspace(new / '.board' / 'hub.sqlite'), str(new))

    # Ambiguous cases stay refused
    def test_database_naming_a_third_folder_is_not_adopted(self):
        new = self.copy()
        with closing(sqlite3.connect(new / '.board' / 'hub.sqlite')) as db, db:
            db.execute("UPDATE settings SET value=? WHERE key='workspace'", (str(self.base / 'Elsewhere'),))
        self.assertIsNone(find_project(new))
        with self.assertRaises(BoardError):
            workspace_connection(new)
        self.assertEqual(stored_workspace(new / '.board' / 'hub.sqlite'), str(self.base / 'Elsewhere'))

    def test_rewritten_route_without_the_original_board_is_not_adopted(self):
        new = self.move()
        route = {'workspace': str(new), 'database': str(new / '.board' / 'hub.sqlite'), 'port': default_port(new)}
        (new / ROUTE).write_text(json.dumps(route))
        with self.assertRaises(BoardError):
            workspace_connection(new)
        self.assertEqual(stored_workspace(new / '.board' / 'hub.sqlite'), str(self.old))

    def test_route_to_a_database_outside_the_folder_is_not_adopted(self):
        new = self.copy()
        route = json.loads((new / ROUTE).read_text())
        route['database'] = str(self.base / 'shared' / 'hub.sqlite')
        (new / ROUTE).write_text(json.dumps(route))
        self.assertIsNone(find_project(new))
        self.assertEqual(stored_workspace(new / '.board' / 'hub.sqlite'), str(self.old))

    def test_copied_route_without_database_is_not_adopted(self):
        new = self.copy()
        (new / '.board' / 'hub.sqlite').unlink()
        self.assertIsNone(find_project(new))

    def test_nested_folder_inside_a_project_is_untouched(self):
        inner = self.old / 'Packages' / 'Vendor'
        inner.mkdir(parents=True)
        self.assertEqual(find_project(inner), self.old)
        self.assertFalse((inner / '.board').exists())


class HookAfterMoveTests(Fixture):
    def hook(self, cwd, tool='mcp__board__board_status', session='chat-one', event='PreToolUse'):
        payload = {'cwd': str(cwd), 'session_id': session, 'hook_event_name': event,
                   'tool_name': tool, 'tool_input': {}}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
        with patch('sys.argv', ['swarmboard_hook.py', '--harness', 'claude']), patch('sys.stdin', stdin), \
                redirect_stdout(io.StringIO()) as output:
            swarmboard_hook.main()
        return json.loads(output.getvalue())

    def activate(self, folder, session='chat-one'):
        key = hashlib.sha256(('claude:' + session).encode()).hexdigest()
        state = folder / '.board' / 'hook-sessions' / (key + '.json')
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({'activated': True, 'agent_id': self.agent['agent_id']}))

    def test_activated_chat_in_a_moved_project_gets_its_route(self):
        self.activate(self.old)
        new = self.move()
        result = self.hook(new)
        self.assertEqual(result['hookSpecificOutput']['updatedInput']['_route']['cwd'], str(new))

    def test_broken_board_config_denies_board_calls_with_the_real_reason(self):
        self.activate(self.old)
        with closing(sqlite3.connect(self.old / '.board' / 'hub.sqlite')) as db, db:
            db.execute("UPDATE settings SET value=? WHERE key='workspace'", (str(self.base / 'Elsewhere'),))
        result = self.hook(self.old)['hookSpecificOutput']
        self.assertEqual(result['permissionDecision'], 'deny')
        self.assertIn('different workspace', result['permissionDecisionReason'])
        self.assertNotIn('claude_desktop_config', result['permissionDecisionReason'])

    def test_broken_board_config_still_lets_board_workspace_choose_a_project(self):
        self.activate(self.old)
        with closing(sqlite3.connect(self.old / '.board' / 'hub.sqlite')) as db, db:
            db.execute("UPDATE settings SET value=? WHERE key='workspace'", (str(self.base / 'Elsewhere'),))
        result = self.hook(self.old, tool='mcp__board__board_workspace')
        self.assertIn('_route', result['hookSpecificOutput']['updatedInput'])

    def test_board_call_outside_any_project_still_carries_the_chat_route(self):
        plain = self.base / 'plain'
        plain.mkdir()
        result = self.hook(plain)
        self.assertEqual(result['hookSpecificOutput']['updatedInput']['_route']['session_id'], 'chat-one')

    def test_non_board_tools_in_a_broken_project_are_not_blocked(self):
        self.activate(self.old)
        with closing(sqlite3.connect(self.old / '.board' / 'hub.sqlite')) as db, db:
            db.execute("UPDATE settings SET value=? WHERE key='workspace'", (str(self.base / 'Elsewhere'),))
        result = self.hook(self.old, tool='Read')
        self.assertNotEqual(result.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')


if __name__ == '__main__':
    unittest.main()
