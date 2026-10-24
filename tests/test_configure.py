import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.configure import configure, connection_text
from board.core import BoardError
from boardctl import operator_connection
from types import SimpleNamespace


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name).resolve()
        self.config = self.root / 'config.toml'
        self.operator = self.root / 'operator.json'
        self.database = self.root / 'hub.sqlite'
        self.original = ('[mcp_servers.other]\ncommand = "keep"\n\n'
                         '[mcp_servers.board]\ntool_timeout_sec = 15\n'
                         'command = "python"\nargs = [\n  "old",\n]\n'
                         'default_tools_approval_mode = "approve"\n\n'
                         '[desktop]\nsetting = true\n')
        self.config.write_text(self.original)
        self.operator.write_text('{"port": 4317, "extra": true}')
        self.health = patch('board.configure.health', return_value=None)
        self.health.start()
        self.addCleanup(self.health.stop)
        self.addCleanup(self.temp.cleanup)

    def switch(self, **kwargs):
        return configure(self.root, kwargs.get('database', self.database),
                         kwargs.get('port', 4318), self.operator, self.root, self.config)

    def test_preserves_other_connections_and_board_policy(self):
        result = self.switch()
        text = self.config.read_text()
        self.assertTrue(text.startswith('[mcp_servers.other]\ncommand = "keep"\n\n'))
        self.assertIn('default_tools_approval_mode = "approve"', text)
        self.assertIn('tool_timeout_sec = 15', text)
        self.assertIn('[desktop]\nsetting = true', text)
        line = next(line for line in text.splitlines() if line.startswith('args = '))
        args = json.loads(line.partition(' = ')[2])
        self.assertEqual(args[args.index('--workspace') + 1], str(self.root))
        self.assertEqual(args[args.index('--url') + 1], 'http://127.0.0.1:4318')
        saved = json.loads(self.operator.read_text())
        self.assertEqual(saved['workspace'], str(self.root))
        self.assertTrue(saved['extra'])
        self.assertEqual(Path(result['backups'][0]).read_text(), self.original)
        self.assertFalse(self.database.exists())

    def test_wrong_database_leaves_configs_untouched(self):
        with sqlite3.connect(self.database) as db:
            db.execute('CREATE TABLE settings (key TEXT, value TEXT)')
            db.execute('INSERT INTO settings VALUES (?, ?)', ('workspace', str(self.root / 'other')))
        with self.assertRaisesRegex(BoardError, 'different workspace'):
            self.switch()
        self.assertEqual(self.config.read_text(), self.original)

    def test_wrong_hub_leaves_configs_untouched(self):
        with patch('board.configure.health', return_value={'service': 'agent-board', 'workspace': str(self.root / 'other')}):
            with self.assertRaisesRegex(BoardError, 'another workspace'):
                self.switch()
        self.assertEqual(self.config.read_text(), self.original)

    def test_failed_operator_write_rolls_back_codex_config(self):
        from board.configure import atomic_write
        def write(path, content):
            if path == self.operator:
                raise OSError('simulated failure')
            atomic_write(path, content)
        with patch('board.configure.atomic_write', side_effect=write):
            with self.assertRaises(OSError):
                self.switch()
        self.assertEqual(self.config.read_text(), self.original)
        self.assertEqual(json.loads(self.operator.read_text())['port'], 4317)

    def test_repeated_switch_does_not_duplicate_board(self):
        self.switch()
        self.switch(port=4319)
        text = self.config.read_text()
        self.assertEqual(text.count('[mcp_servers.board]'), 1)
        self.assertEqual(text.count('args = '), 1)
        self.assertIn('127.0.0.1:4319', text)

    def test_default_config_is_project_scoped(self):
        with patch.dict('os.environ', {'CODEX_HOME': str(self.root / 'global')}):
            result = configure(self.root, self.database, 4318, self.operator, self.root)
        self.assertEqual(Path(result['mcp_config']), self.root / '.codex/config.toml')
        self.assertFalse((self.root / 'global/config.toml').exists())

    def test_new_configuration_and_invalid_port(self):
        text = connection_text('', self.root, self.database, 4318, self.root)
        self.assertEqual(text.count('[mcp_servers.board]'), 1)
        with self.assertRaisesRegex(BoardError, 'Port must be'):
            self.switch(port=0)

    def test_operator_uses_complete_project_route_without_global_defaults(self):
        local = self.root / '.codex' / 'board-operator.json'
        local.parent.mkdir()
        local.write_text(json.dumps({'workspace':str(self.root),'database':str(self.database),'port':4318}))
        args = SimpleNamespace(config=None,workspace=str(self.root),database=None,port=None)
        workspace, database, port = operator_connection(args)
        self.assertEqual((workspace,database,port),(self.root,self.database,4318))

    def test_operator_rejects_missing_or_cross_project_explicit_config(self):
        args = SimpleNamespace(config=self.root/'missing.json',workspace=None,database=None,port=None)
        with self.assertRaisesRegex(BoardError,'existing'):
            operator_connection(args)
        self.operator.write_text(json.dumps({'workspace':str(self.root),'database':str(self.database),'port':4318}))
        args.config=self.operator
        args.workspace=str(self.root/'other')
        with self.assertRaisesRegex(BoardError,'mixed project'):
            operator_connection(args)


if __name__ == '__main__':
    unittest.main()
