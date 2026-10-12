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
        shells = [[path, '-NoProfile', '-NonInteractive', '-Command'] for name in ('powershell.exe', 'pwsh') if (path := shutil.which(name))]
        if shutil.which('cmd.exe'):
            shells.append([shutil.which('cmd.exe'), '/e:ON', '/v:OFF', '/d', '/c'])
        self.assertTrue(shells)
        argument = "C:/space dir/O'Brien/$literal`;file.json"
        argv = [sys.executable, '-c', 'import json,sys; print(json.dumps([sys.argv[1],sys.stdin.read()])); sys.exit(7)', argument]
        for shell in shells:
            with self.subTest(shell=shell):
                result = subprocess.run([*shell, shell_command(argv, windows=True)], input='{"hook_event_name":"Stop"}', capture_output=True, text=True, timeout=15)
                # An outer PowerShell -Command maps a native nonzero status to
                # 1; CMD preserves it. Both must preserve failure and payload.
                self.assertEqual(result.returncode, 7 if '/c' in shell else 1, result.stderr)
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

    def test_workspace_required_hook_gives_usable_order(self):
        reply = self.event('PostToolUse', tool_name='mcp__board__board_status',
                           tool_response={'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': 'WORKSPACE_REQUIRED: choose project'})}]})
        self.assertIn('then board_status', reply['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('shared inbox', reply['hookSpecificOutput']['additionalContext'])

    def test_codex_hook_link_automatically_restores_wake(self):
        self.adapter.harness = 'codex'
        self.bind()
        status = self.call('board_desktop_wake', action='status')
        self.assertTrue(status['enabled'])
        self.assertEqual(status['thread_id'], 'native-session')
        self.assertEqual(status['activity'], 'busy')
        with self.hub.connect() as db:
            db.execute('DELETE FROM desktop_wake_bindings')
        self.event('UserPromptSubmit')
        self.assertTrue(self.call('board_desktop_wake', action='status')['enabled'])
        self.event('UserPromptSubmit')
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM desktop_wake_bindings').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM desktop_wake_deliveries').fetchone()[0], 0)

    def test_codex_activity_follows_turn_hooks_and_never_acknowledges(self):
        self.adapter.harness = 'codex'
        self.bind()
        peer = self.hub.call('register', {'name': 'sender'})
        self.hub.call('inbox_send', {**peer, 'recipient_agent_id': self.agent['agent_id'], 'body': 'pending'})
        for event in ('PreToolUse', 'PostToolUse', 'UserPromptSubmit'):
            self.event(event, tool_name='Read')
            self.assertEqual(self.call('board_desktop_wake', action='status')['activity'], 'busy')
        self.event('Stop')
        self.assertEqual(self.call('board_desktop_wake', action='status')['activity'], 'idle')
        self.assertEqual(len(self.call('inbox_read')['messages']), 1)
        self.event('UserPromptSubmit')
        self.assertEqual(self.call('board_desktop_wake', action='status')['activity'], 'busy')

    def test_claude_hook_link_does_not_register_codex_wake(self):
        self.bind()
        self.assertFalse(self.call('board_desktop_wake', action='status')['enabled'])

    def test_existing_native_identity_conflict_is_not_accused_of_other_chat(self):
        import hashlib
        key = hashlib.sha256(b'claude:native-session').hexdigest()
        canonical = self.hub.call('register', {'name': 'claude-code', 'native_harness': 'claude', 'native_session_key': key})
        reply = self.bind()
        message = reply['hookSpecificOutput']['additionalContext']
        self.assertIn(canonical['agent_id'], message)
        self.assertIn('already assigned identity', message)
        self.assertIn('Desktop Chat config overrides', message)
        self.assertNotIn('belongs to another native chat', message)
        prompt = self.event('UserPromptSubmit')['hookSpecificOutput']['additionalContext']
        self.assertIn('identity mismatch', prompt)
        blocked = self.event('PreToolUse', tool_name='Write', tool_input={'file_path':str(self.root/'a.py')})
        self.assertEqual(blocked['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_queue_member_cannot_directly_edit_queued_file(self):
        path = self.root / 'common.py'
        path.write_text('original\n')
        self.call('patch_queue', action='join', path=str(path), reason='shared file')
        self.bind()
        response = self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(path)})
        self.assertEqual(response['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('patch queue lock', response['hookSpecificOutput']['permissionDecisionReason'])

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

    def test_late_native_end_with_stale_credentials_does_not_disconnect(self):
        self.bind()
        with self.hub.connect() as db:
            db.execute('UPDATE agents SET token=? WHERE id=?',('new-connection-token',self.agent['agent_id']))
        self.assertEqual(self.event('SessionEnd'),{})
        with self.hub.connect() as db:
            self.assertIsNone(db.execute('SELECT disconnected FROM agents WHERE id=?',(self.agent['agent_id'],)).fetchone()[0])

    def test_retired_native_end_exits_without_hook_error(self):
        self.bind()
        other = self.hub.call('register',{'name':'owner action'})
        self.hub.call('retire',{**other,'target_agent_id':self.agent['agent_id'],'reason':'retired'})
        self.assertEqual(self.event('SessionEnd'),{})

    def test_outside_workspace_and_nonboard_outputs_do_not_bind(self):
        outside = self.root.parent
        self.assertEqual(self.adapter.handle({'cwd': str(outside), 'hook_event_name': 'PreToolUse'}), {})
        self.event('PostToolUse', tool_name='mcp__other__tool', tool_response={'your_agent_id': self.agent['agent_id']})
        states = list((self.root / 'hook-sessions').glob('*.json'))
        self.assertTrue(all('agent_id' not in json.loads(path.read_text()) for path in states))

    def test_large_notice_backlog_pages_safely_for_bash_and_edit(self):
        self.bind()
        self.call('claim', paths=['a.py'], reason='Authorized edit')
        requests = []
        def caller(url, method, params):
            if method == 'noticeboard_read':
                requests.append(dict(params))
            return self.hub.call(method, params)
        self.adapter.caller = caller
        last_id = 0
        for tool, arguments in [('Bash', {'command': 'python --version'}),
                                ('Edit', {'file_path': str(self.root / 'a.py')})]:
            for index in range(7):
                last_id = self.call('noticeboard_post', body=f'{tool} notice {index}: ' + 'x' * 4700)['id']
            with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE'):
                self.call('noticeboard_read')
            requests.clear()
            output = self.event('PreToolUse', tool_name=tool, tool_input=arguments)['hookSpecificOutput']
            self.assertNotIn('permissionDecision', output)
            self.assertIn(f'{tool} notice 6', output['additionalContext'])
            self.assertEqual(len(requests), 8)  # Seven single posts plus the empty terminal page.
            self.assertTrue(all(request['limit'] == 1 for request in requests))
            self.assertEqual(requests[-1]['after_id'], last_id)
            state_path = next((self.root / 'hook-sessions').glob('*.json'))
            self.assertEqual(json.loads(state_path.read_text())['after_id'], last_id)
            # Restarted adapters resume the saved cursor instead of rereading history.
            self.adapter = Adapter(self.root, self.db, 'claude', caller=caller)
            requests.clear()
            self.event('PreToolUse', tool_name=tool, tool_input=arguments)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['after_id'], last_id)

    def test_large_status_does_not_block_claimed_edit_or_allow_unclaimed_edit(self):
        self.bind()
        path = self.root / 'a.py'
        self.call('claim', paths=[str(path)], reason='edit')
        job = self.call('job_post', title='Large job', description='description', acceptance='done')['job']
        owned = self.call('job_claim', job_id=job['id'])['job']
        self.call('job_update', job_id=job['id'], lease_token=owned['lease_token'], progress='x' * 15000)
        with self.hub.connect() as db:
            for i in range(30):
                db.execute("INSERT INTO custody VALUES (?,?,'HELD',?,NULL,0)", (f'old{i}.py', self.agent['agent_id'], 'y' * 1000))
        with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE'):
            self.call('board_status', target_agent_id='self', include_rules=False)
        allowed = self.event('PreToolUse', tool_name='Edit', tool_input={'file_path':str(path)})
        self.assertNotEqual(allowed.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        denied = self.event('PreToolUse', tool_name='Edit', tool_input={'file_path':str(self.root/'unclaimed.py')})
        self.assertEqual(denied['hookSpecificOutput']['permissionDecision'], 'deny')
        self.event('Stop')

    def test_post_action_deletion_cleanup_uses_shared_function(self):
        self.bind()
        path = self.root / 'a.py'
        path.write_text('remove me')
        self.call('claim', paths=['a.py'], reason='Remove obsolete file')
        requests = []
        def caller(url, method, params):
            requests.append((method, params))
            return self.hub.call(method, params)
        self.adapter.caller = caller
        path.unlink()
        self.event('PostToolUse', tool_name='Bash', tool_input={'command': 'rm a.py'})
        self.assertTrue(any(method == 'board_file' and params['action'] == 'reconcile' for method, params in requests))
        status = self.call('board_status')
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['filesystem_state'], 'deleted')
        self.assertEqual(len(self.call('board_file', action='history', path='a.py')['records']), 1)

    def test_hooks_request_focused_status(self):
        requests = []
        def caller(url, method, params):
            if method == 'hook_status':
                requests.append(params)
            return self.hub.call(method, params)
        self.adapter.caller = caller
        self.bind()
        self.event('Stop')
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(p['paths'] == [] and p['resources'] == [] for p in requests))

    def test_inbox_injected_after_ordinary_tools_and_on_next_prompt_without_ack(self):
        self.bind()
        sender = self.hub.call('register', {'name': 'Waiting peer'})
        message = self.hub.call('inbox_send', {**sender, 'recipient_agent_id': self.agent['agent_id'], 'body': 'Please check this reply now'})
        for event, fields in [('PostToolUse', {'tool_name': 'Read', 'tool_response': 'file text'}),
                              ('PostToolUse', {'tool_name': 'mcp__other__read', 'tool_response': {}}),
                              ('UserPromptSubmit', {'prompt': 'Continue'}), ('SessionStart', {})]:
            with self.subTest(event=event, fields=fields):
                output = self.event(event, **fields)['hookSpecificOutput']
                self.assertEqual(output['hookEventName'], event)
                self.assertIn('Please check this reply now', output['additionalContext'])
                self.assertEqual(self.call('inbox_read')['unread_inbox'], 1)
        self.call('inbox_ack', message_ids=[message['id']])
        self.assertEqual(self.event('UserPromptSubmit', prompt='Continue'), {})
        self.assertEqual(self.event('PostToolUse', tool_name='Read'), {})

    def test_unlinked_session_reports_delivery_gap_without_guessing_identity(self):
        first = self.event('PostToolUse', tool_name='Read')
        self.assertIn('not linked', first['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.event('PostToolUse', tool_name='Read'), {})
        self.assertIn('native board_status', self.event('UserPromptSubmit')['hookSpecificOutput']['additionalContext'])
        self.assertIn('linked', self.bind()['hookSpecificOutput']['additionalContext'])

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
        self.assertIn('/board release', warning)
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
        self.assertNotIn('matcher', result['hooks']['PostToolUse'][0])
        self.assertEqual(len(result['hooks']['UserPromptSubmit']), 1)
        install(path, 'python', ['/repo/swarmboard_hook.py', '--harness', 'claude'])
        install(path, 'python', ['/repo/swarmboard_hook.py', '--harness', 'claude'])
        self.assertEqual(len(json.loads(path.read_text())['hooks']['PreToolUse']), 2)
        encoded = shell_command(['python', '/repo/swarmboard_hook.py', '--harness', 'codex'], windows=True)
        install(path, encoded)
        install(path, encoded)
        self.assertEqual(len(json.loads(path.read_text())['hooks']['PreToolUse']), 2)
