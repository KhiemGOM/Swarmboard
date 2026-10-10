import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.hooks import Adapter, edit_paths
from install_hooks import install, shell_command


class HookTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell launcher')
    def test_windows_shell_preserves_arguments_stdin_and_exit_status(self):
        shells = [path for name in ('powershell.exe', 'pwsh') if (path := shutil.which(name))]
        self.assertTrue(shells)
        argument = "C:/space dir/O'Brien/$literal`;file.json"
        argv = [sys.executable, '-c', 'import json,sys; print(json.dumps([sys.argv[1],sys.stdin.read()])); sys.exit(7)', argument]
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run([shell, '-NoProfile', '-NonInteractive', '-Command', shell_command(argv, windows=True)], input='{"hook_event_name":"Stop"}', capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(json.loads(result.stdout), [argument, '{"hook_event_name":"Stop"}'])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.agent = self.hub.call('register', {'name': 'MCP'})
        self.adapter = Adapter(self.root, self.db, 'claude', caller=lambda url, method, params: self.hub.call(method, params))

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, **params):
        return self.hub.call(method, {**params, 'agent_id': self.agent['agent_id'], 'session_token': self.agent['session_token']})

    def event(self, event, **params):
        return self.adapter.handle({'session_id': 'native-session', 'cwd': str(self.root), 'hook_event_name': event, **params})

    def bind(self):
        return self.event('PostToolUse', tool_name='mcp__board__board_status', tool_response={'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': self.agent['agent_id']})}]})

    def test_link_uses_existing_mcp_identity_and_denies_unclaimed_edits(self):
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'a.py')})['hookSpecificOutput']['permissionDecision'], 'deny')
        self.bind()
        self.assertEqual(len(self.call('board_status')['agents']), 1)
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'a.py')})['hookSpecificOutput']['permissionDecision'], 'deny')
        self.call('claim', paths=['a.py'], reason='Editing')
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'a.py')}), {})
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'b.py')})['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_patch_move_requires_both_paths_and_claim_expiry_is_checked(self):
        self.bind()
        patch = '*** Begin Patch\n*** Update File: a.py\n*** Move to: b.py\n*** End Patch'
        self.assertEqual(edit_paths('apply_patch', {'command': patch}), ['a.py', 'b.py'])
        self.call('claim', paths=['a.py'], reason='Rename')
        self.assertEqual(self.event('PreToolUse', tool_name='apply_patch', tool_input={'command': patch})['hookSpecificOutput']['permissionDecision'], 'deny')
        self.call('claim', paths=['b.py'], reason='Rename target')
        self.assertEqual(self.event('PreToolUse', tool_name='apply_patch', tool_input={'command': patch}), {})
        with self.hub.connect() as db:
            db.execute('UPDATE claims SET expires=0')
        self.assertEqual(self.event('PreToolUse', tool_name='apply_patch', tool_input={'command': patch})['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_git_requires_index_mutex_and_other_shell_commands_get_reminders(self):
        self.bind()
        denied = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'git add a.py'})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.call('mutex_claim', resources=['git:index'], reason='Commit')
        allowed = self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'git add a.py'})
        self.assertNotIn('permissionDecision', allowed['hookSpecificOutput'])
        self.assertIn('cooperative', self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'python arbitrary_writer.py'})['hookSpecificOutput']['additionalContext'])

    def test_stop_nags_and_session_end_reopens_without_committing(self):
        self.bind()
        job = self.call('job_post', title='Bug', description='Fix', acceptance='Regression passes')['job']
        self.call('job_claim', job_id=job['id'])
        self.assertIn('handoff', self.event('Stop')['systemMessage'])
        self.event('SessionEnd')
        status = self.call('job_list')['jobs'][0]
        self.assertEqual(status['state'], 'OPEN')
        self.assertIsNone(status['commit_hash'])

    def test_outside_workspace_and_nonboard_outputs_do_not_bind(self):
        outside = self.root.parent
        self.assertEqual(self.adapter.handle({'cwd': str(outside), 'hook_event_name': 'PreToolUse'}), {})
        self.event('PostToolUse', tool_name='mcp__other__tool', tool_response={'your_agent_id': self.agent['agent_id']})
        self.assertFalse((self.root / 'hook-sessions').exists())

    def test_hooks_request_focused_status(self):
        requests = []
        def caller(url, method, params):
            if method == 'board_status':
                requests.append(params)
            return self.hub.call(method, params)
        self.adapter.caller = caller
        self.bind()
        self.event('Stop')
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(p['target_agent_id'] == 'self' and p['include_rules'] is False for p in requests))

    def test_inbox_surfaces_on_board_calls_and_blocked_edits_until_ack(self):
        sender = self.hub.call('register', {'name': 'Waiting peer'})
        message = self.hub.call('inbox_send', {**sender, 'recipient_agent_id': self.agent['agent_id'], 'body': 'Can you release the test DB? I am waiting.'})
        notice = self.bind()['hookSpecificOutput']['additionalContext']
        self.assertIn('I am waiting', notice)
        self.assertIn(sender['agent_id'], notice)
        self.assertEqual(self.call('inbox_read')['unread_inbox'], 1)
        rejected = self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'unclaimed.py'})
        self.assertEqual(rejected['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('I am waiting', rejected['hookSpecificOutput']['permissionDecisionReason'])
        self.call('inbox_ack', message_ids=[message['id']])
        self.assertEqual(self.bind(), {})

    def test_stop_reviews_active_mutexes_without_releasing_or_renewing(self):
        self.bind()
        claim = self.call('mutex_claim', resources=['port:3519'], reason='Preview still running')
        before = self.call('board_status')['claims']
        warning = self.event('Stop')['systemMessage']
        self.assertIn('/board mutex', warning)
        self.assertEqual(self.call('board_status')['claims'], before)
        self.call('release', claim_ids=claim['claim_ids'], reason='Preview stopped')
        self.assertEqual(self.event('Stop'), {})

    def test_retired_identity_is_rejected_and_unavailable_hub_raises(self):
        self.bind()
        other = self.hub.call('register', {'name': 'Other'})
        self.hub.call('takeover', {**other, 'target_agent_id': self.agent['agent_id'], 'reason': 'Unavailable'})
        with self.assertRaisesRegex(BoardError, 'retired'):
            self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'})

    def test_installer_preserves_settings_and_is_idempotent(self):
        path = self.root / 'settings.json'
        path.write_text(json.dumps({'permissions': {'defaultMode': 'default'}, 'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'other_hook'}]}]}}))
        install(path, 'python /repo/swarmboard_hook.py --harness claude')
        install(path, 'python /repo/swarmboard_hook.py --harness claude')
        result = json.loads(path.read_text())
        self.assertEqual(result['permissions']['defaultMode'], 'default')
        self.assertEqual(len(result['hooks']['PreToolUse']), 2)
        self.assertEqual(result['hooks']['PreToolUse'][0]['hooks'][0]['command'], 'other_hook')
        self.assertEqual(len(result['hooks']['SessionStart']), 1)
        install(path, 'python', ['/repo/swarmboard_hook.py', '--harness', 'claude'])
        install(path, 'python', ['/repo/swarmboard_hook.py', '--harness', 'claude'])
        self.assertEqual(len(json.loads(path.read_text())['hooks']['PreToolUse']), 2)
