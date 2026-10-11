import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from swarmboard_hook import hook_connection
from install_hooks import main as install_main


class HookRoutingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.projects = []
        for name, port in [('a', 4317), ('b', 4318)]:
            workspace = self.root / name
            workspace.mkdir()
            database = workspace / 'existing.sqlite'
            Hub(database, workspace)
            config = workspace / '.codex/board-operator.json'
            config.parent.mkdir()
            config.write_text(json.dumps({'workspace': str(workspace), 'database': str(database), 'port': port}))
            self.projects.append((workspace, database, port))

    def test_same_global_hook_routes_each_native_project(self):
        for workspace, database, port in self.projects:
            result = hook_connection({'cwd': str(workspace)})
            self.assertEqual(result, (str(workspace), str(database), f'http://127.0.0.1:{port}'))

    def test_native_subdirectory_uses_nearest_project(self):
        workspace, database, port = self.projects[0]
        sub = workspace / 'src'
        sub.mkdir()
        self.assertEqual(hook_connection({'cwd': str(sub)})[0], str(workspace))

    def test_missing_or_relative_native_cwd_is_rejected(self):
        for payload in [{}, {'cwd': '.'}, {'cwd': ''}]:
            with self.assertRaisesRegex(BoardError, 'absolute cwd'):
                hook_connection(payload)

    def test_wrong_local_workspace_is_rejected(self):
        a, database, port = self.projects[0]
        b = self.projects[1][0]
        (a / '.codex/board-operator.json').write_text(json.dumps({'workspace': str(b), 'database': str(database), 'port': port}))
        with self.assertRaisesRegex(BoardError, 'different workspace'):
            hook_connection({'cwd': str(a)})

    def test_installer_global_hooks_have_no_project_arguments(self):
        codex, claude = self.root / 'hooks.json', self.root / 'settings.json'
        with patch.object(sys, 'argv', ['install_hooks.py', '--codex-config', str(codex), '--claude-config', str(claude)]):
            install_main()
        import base64
        for path in [codex, claude]:
            data = json.loads(path.read_text())
            for entries in data['hooks'].values():
                for entry in entries:
                    for hook in entry['hooks']:
                        command = hook['command']
                        prefix = 'powershell.exe -NoProfile -NonInteractive -EncodedCommand '
                        if command.startswith(prefix):
                            command = base64.b64decode(command[len(prefix):]).decode('utf-16-le')
                        rendered = command + ' ' + ' '.join(hook.get('args', []))
                        for flag in ['--workspace', '--database', '--url']:
                            self.assertNotIn(flag, rendered)

    def test_unconfigured_workspace_creates_no_state(self):
        directory = self.root / 'unconfigured'
        directory.mkdir()
        entry = Path(__file__).resolve().parents[1] / 'swarmboard_hook.py'
        payload = {'cwd': str(directory), 'session_id': 'native-test', 'hook_event_name': 'SessionStart'}
        result = subprocess.run([sys.executable, str(entry), '--harness', 'codex'], input=json.dumps(payload), capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), {})
        self.assertFalse((directory / '.board').exists())
