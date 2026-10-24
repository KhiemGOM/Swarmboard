import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.runtime import workspace_connection, ensure_hub


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / 'a'
        self.b = self.root / 'b'
        self.a.mkdir()
        self.b.mkdir()

    def save(self, folder, workspace, port=4318):
        path = folder / '.codex/board-operator.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'workspace': str(workspace),
                                    'database': str(self.root / 'existing.sqlite'), 'port': port}))

    def test_unconfigured_launch_directory_is_refused_and_explicit_defaults_are_distinct(self):
        # 0.12: an unconfigured launch directory (Desktop starts bridges in C:\) is never a board.
        with patch('board.runtime.Path.cwd', return_value=self.a):
            with self.assertRaisesRegex(BoardError, 'WORKSPACE_REQUIRED'):
                workspace_connection()
        a, b = workspace_connection(self.a), workspace_connection(self.b)
        self.assertEqual(a[0], str(self.a))
        self.assertEqual(a[1], str(self.a / '.board/hub.sqlite'))
        self.assertNotEqual(a[2], b[2])

    def test_local_config_preserves_existing_state(self):
        self.save(self.a, self.a)
        self.assertEqual(workspace_connection(self.a),
                         (str(self.a), str(self.root / 'existing.sqlite'), 'http://127.0.0.1:4318'))

    def test_automatic_subfolder_uses_configured_project(self):
        self.save(self.a, self.a)
        child = self.a / 'src' / 'nested'
        child.mkdir(parents=True)
        with patch('board.runtime.Path.cwd', return_value=child):
            self.assertEqual(workspace_connection(), workspace_connection(self.a))

    def test_explicit_subfolder_is_not_redirected(self):
        self.save(self.a, self.a)
        child = self.a / 'src'
        child.mkdir()
        self.assertEqual(workspace_connection(child)[0], str(child))

    def test_nearest_nested_configuration_wins(self):
        self.save(self.a, self.a)
        child = self.a / 'nested'
        child.mkdir()
        self.save(child, child, port=4319)
        leaf = child / 'src'
        leaf.mkdir()
        with patch('board.runtime.Path.cwd', return_value=leaf):
            result = workspace_connection()
        self.assertEqual(result[0], str(child))
        self.assertEqual(result[2], 'http://127.0.0.1:4319')

    def test_invalid_parent_configuration_fails_closed(self):
        self.save(self.a, self.b)
        child = self.a / 'src'
        child.mkdir()
        with patch('board.runtime.Path.cwd', return_value=child):
            with self.assertRaisesRegex(BoardError, 'different workspace'):
                workspace_connection()

    def test_wrong_local_workspace_fails_closed(self):
        self.save(self.a, self.b)
        with self.assertRaisesRegex(BoardError, 'different workspace'):
            workspace_connection(self.a)

    def test_wrong_database_fails_before_start_or_registration(self):
        Hub(self.root / 'existing.sqlite', self.b)
        self.save(self.a, self.a)
        with self.assertRaisesRegex(BoardError, 'Database belongs'):
            workspace_connection(self.a)

    def test_existing_wrong_checkout_operator_is_never_a_fallback(self):
        path = self.a / '.board/operator.json'
        path.parent.mkdir()
        path.write_text(json.dumps({'workspace': str(self.b)}))
        with self.assertRaisesRegex(BoardError, 'different workspace'):
            workspace_connection(self.a)

    def test_port_collision_does_not_join_another_project(self):
        with patch('board.runtime.health', return_value={'service': 'agent-board', 'workspace': str(self.b)}):
            with self.assertRaisesRegex(BoardError, 'different workspace'):
                ensure_hub('http://127.0.0.1:4318', self.a, self.a / 'hub.sqlite', True)

    def test_project_config_precedes_legacy_operator(self):
        self.save(self.a, self.a)
        old = self.a / '.board/operator.json'
        old.parent.mkdir()
        old.write_text(json.dumps({'workspace': str(self.b)}))
        self.assertEqual(workspace_connection(self.a)[0], str(self.a))
