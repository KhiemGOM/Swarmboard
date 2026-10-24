"""Board integration is opt-in per chat (/board), and linked chats take their title from board work.

Plan / potential failures covered here:
- Plain chats in a configured repo must behave like a normal repo: no join nudges, no edit gating,
  no hub registration just because the MCP bridge connected.
- Board tools are denied until the user types /board ($board in Codex); model text, other slash
  commands and lookalikes (/boardgame, "please /board") must not activate it.
- Activation persists across SessionStart (resume) and for chats already linked before upgrade.
- An unavailable hub must not warn chats that never opted in.
- Title sync only for Claude, only when the source title changes (a manual rename is not stomped
  every prompt), job title preferred over activity, control characters stripped and length bounded.
"""
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from board.core import Hub
from board.hooks import Adapter
from install_hooks import install

ROOT = Path(__file__).resolve().parents[1]


class HookCase(unittest.TestCase):
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

    def event(self, event, session='native-session', **params):
        return self.adapter.handle({'session_id': session, 'cwd': str(self.root), 'hook_event_name': event, **params})

    def board_tool(self, session='native-session'):
        return self.event('PreToolUse', session, tool_name='mcp__board__board_status', tool_input={})

    def bind(self, session='native-session'):
        return self.event('PostToolUse', session, tool_name='mcp__board__board_status',
                          tool_response={'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': self.agent['agent_id']})}]})

    def activate(self, session='native-session'):
        return self.event('UserPromptExpansion', session, expansion_type='slash_command', command_name='board', command_args='', prompt='/board')

    def denied(self, reply):
        return reply.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'


class OptInHookTests(HookCase):
    # --- basics: a plain chat is a normal repo ---

    def test_plain_chat_gets_no_board_nudges(self):
        self.assertEqual(self.event('SessionStart', source='startup'), {})
        self.assertEqual(self.event('UserPromptSubmit', prompt='fix the parser'), {})
        self.assertEqual(self.event('Stop'), {})

    def test_plain_chat_edits_and_shell_are_not_gated(self):
        self.assertEqual(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'}), {})
        self.assertEqual(self.event('PreToolUse', tool_name='Bash', tool_input={'command': 'git commit -m x'}), {})

    def test_board_tools_denied_until_user_types_board(self):
        reply = self.board_tool()
        self.assertTrue(self.denied(reply))
        self.assertIn('/board', reply['hookSpecificOutput']['permissionDecisionReason'])

    def test_slash_board_expansion_activates_board_tools(self):
        notice = self.activate()
        self.assertIn('activated', notice['hookSpecificOutput']['additionalContext'])
        self.assertFalse(self.denied(self.board_tool()))

    def test_typed_prompt_activates_with_arguments_and_codex_prefix(self):
        self.event('UserPromptSubmit', prompt='/board release')
        self.assertFalse(self.denied(self.board_tool()))
        self.adapter.harness = 'codex'
        self.event('UserPromptSubmit', 'codex-chat', prompt='  $board')
        self.assertFalse(self.denied(self.board_tool('codex-chat')))

    def test_activation_is_per_chat(self):
        self.activate('first')
        self.assertTrue(self.denied(self.board_tool('second')))

    def test_activation_survives_session_resume(self):
        self.activate()
        self.event('SessionStart', source='resume')
        self.assertFalse(self.denied(self.board_tool()))

    def test_chat_linked_before_upgrade_stays_active(self):
        self.bind()  # Legacy state: linked by an earlier board call, no activation flag.
        self.assertFalse(self.denied(self.board_tool()))

    # --- potential failures ---

    def test_lookalikes_do_not_activate(self):
        for prompt in ('/boardgame', 'please /board', 'what does /board do?', 'board', '/Board-ish'):
            self.event('UserPromptSubmit', prompt=prompt)
        self.event('UserPromptExpansion', expansion_type='slash_command', command_name='boardgame', command_args='', prompt='/boardgame')
        self.event('UserPromptExpansion', expansion_type='mcp_prompt', command_name='board', command_args='', prompt='/board')
        self.assertTrue(self.denied(self.board_tool()))

    def test_plugin_namespaced_board_command_activates(self):
        self.event('UserPromptExpansion', expansion_type='slash_command', command_name='swarm:board', command_args='', prompt='/swarm:board')
        self.assertFalse(self.denied(self.board_tool()))

    def test_activated_chat_still_enforces_claims_once_linked(self):
        self.activate()
        self.bind()
        self.assertTrue(self.denied(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'})))

    def test_unavailable_hub_is_silent_for_plain_chats(self):
        from swarmboard_hook import unavailable
        payload = {'session_id': 'plain', 'hook_event_name': 'UserPromptSubmit'}
        self.assertEqual(unavailable(payload, str(self.db), 'claude', 'Hub unavailable'), {})
        self.activate('joined')
        joined = unavailable({'session_id': 'joined', 'hook_event_name': 'UserPromptSubmit'}, str(self.db), 'claude', 'Hub unavailable')
        self.assertIn('disconnected', joined['hookSpecificOutput']['additionalContext'])

    def test_installer_routes_board_tools_and_slash_expansion_through_hooks(self):
        path = self.root / 'settings.json'
        install(path, 'python', ['/repo/swarmboard_hook.py', '--harness', 'claude'], expansion=True)
        hooks = json.loads(path.read_text())['hooks']
        self.assertIn('mcp__board__', hooks['PreToolUse'][0]['matcher'])
        self.assertEqual(len(hooks['UserPromptExpansion']), 1)
        codex = self.root / 'hooks.json'
        install(codex, 'python /repo/swarmboard_hook.py --harness codex')
        self.assertNotIn('UserPromptExpansion', json.loads(codex.read_text())['hooks'])


class NativeLinkTests(HookCase):
    def test_native_registered_identity_links_without_reconnect_loop(self):
        import hashlib
        key = hashlib.sha256(b'claude:native-session').hexdigest()
        self.agent = self.hub.call('register', {'name': 'claude-code', 'native_harness': 'claude', 'native_session_key': key})
        self.activate()
        first = self.bind()['hookSpecificOutput']['additionalContext']
        self.assertNotIn('reconnect', first)
        self.assertIn('linked', first)
        self.assertFalse(list((self.root / 'native-reconnect').glob('*.json')))
        self.assertTrue(self.denied(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': 'a.py'})))
        self.call('board_activity', action='update', title='Linked natively')
        self.assertEqual(self.event('UserPromptSubmit', prompt='go')['hookSpecificOutput']['sessionTitle'], 'Linked natively')


class TitleSyncTests(HookCase):
    def linked(self):
        self.activate()
        self.bind()

    def title(self, reply):
        return reply.get('hookSpecificOutput', {}).get('sessionTitle')

    def test_job_title_becomes_chat_title(self):
        self.linked()
        self.call('job_post_and_claim', title='Opt-in board activation', description='d', acceptance='a', kind='code')
        reply = self.event('UserPromptSubmit', prompt='continue')
        self.assertEqual(self.title(reply), 'Opt-in board activation')
        self.assertEqual(reply['hookSpecificOutput']['hookEventName'], 'UserPromptSubmit')

    def test_activity_title_used_without_job_and_job_wins_over_activity(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Reading hooks')
        self.call('job_post_and_claim', title='Ship title sync', description='d', acceptance='a', kind='code')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Ship title sync')

    def test_unchanged_source_does_not_stomp_manual_rename(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Reading hooks')
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go', session_title='My own name')))
        self.call('board_activity', action='update', title='Writing tests')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go', session_title='My own name')), 'Writing tests')

    def test_current_title_already_matching_is_not_resent(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go', session_title='Reading hooks')))

    def test_title_is_sanitized_and_bounded(self):
        self.linked()
        self.call('job_post_and_claim', title='Line one\nline\ttwo ' + 'x' * 150, description='d', acceptance='a', kind='code')
        title = self.title(self.event('UserPromptSubmit', prompt='go'))
        self.assertTrue(title.startswith('Line one line two'))
        self.assertLessEqual(len(title), 80)
        self.assertFalse(any(ord(c) < 32 for c in title))

    def test_title_combines_with_inbox_context(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        sender = self.hub.call('register', {'name': 'Peer'})
        self.hub.call('inbox_send', {**sender, 'recipient_agent_id': self.agent['agent_id'], 'body': 'ping'})
        output = self.event('UserPromptSubmit', prompt='go')['hookSpecificOutput']
        self.assertEqual(output['sessionTitle'], 'Reading hooks')
        self.assertIn('ping', output['additionalContext'])

    def test_no_title_without_job_or_activity_or_link(self):
        self.activate()
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go')))
        self.bind()
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go')))

    def test_codex_never_receives_session_title(self):
        self.adapter.harness = 'codex'
        self.adapter.ensure_desktop_wake = lambda *args, **kwargs: None
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go')))

    def test_finished_job_falls_back_to_activity(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        job = self.call('job_post_and_claim', title='Ship title sync', description='d', acceptance='a', kind='noncode')['job']
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Ship title sync')
        self.call('job_finish', job_id=job['id'], lease_token=job['lease_token'], summary='done',
                  checks=[{'command': 'none', 'result': 'not_applicable', 'evidence': 'noncode'}])
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Reading hooks')


class RenameCommandTests(HookCase):
    """/board rename [TITLE] renames the chat now instead of waiting for the work title to change.

    Potential failures: lookalikes (/board renamed, model text) trigger it; a forced sync is
    swallowed by the unchanged-source guard; explicit text is stomped on the next prompt; an
    unlinked chat or empty source returns a blank title; Codex gets a field it cannot use;
    inbox notes are lost; the command breaks reset gating.
    """

    def linked(self):
        self.activate()
        self.bind()

    def rename(self, prompt='/board rename', **params):
        return self.event('UserPromptSubmit', prompt=prompt, **params)

    def title(self, reply):
        return reply.get('hookSpecificOutput', {}).get('sessionTitle')

    def note(self, reply):
        return reply.get('hookSpecificOutput', {}).get('additionalContext', '')

    # --- basics ---

    def test_rename_overrides_manual_title_with_current_work(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go')), 'Reading hooks')
        # The user renamed the chat by hand; a plain prompt keeps it, the command restores the work title.
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go', session_title='Mine')))
        self.assertEqual(self.title(self.rename(session_title='Mine')), 'Reading hooks')

    def test_rename_prefers_job_title(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.call('job_post_and_claim', title='Ship rename command', description='d', acceptance='a', kind='code')
        self.assertEqual(self.title(self.rename()), 'Ship rename command')

    def test_title_alias_works(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.rename('/board title')), 'Reading hooks')

    def test_explicit_text_sets_title_and_survives_next_prompt(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.rename('/board rename  Parser   rewrite ')), 'Parser rewrite')
        self.assertIsNone(self.title(self.event('UserPromptSubmit', prompt='go', session_title='Parser rewrite')))
        self.call('board_activity', action='update', title='Writing tests')
        self.assertEqual(self.title(self.event('UserPromptSubmit', prompt='go', session_title='Parser rewrite')), 'Writing tests')

    def test_explicit_text_works_before_the_chat_is_linked(self):
        self.assertEqual(self.title(self.rename('/board rename Scratch notes')), 'Scratch notes')

    def test_rename_also_activates_the_chat(self):
        self.rename('/board rename Scratch notes')
        self.assertFalse(self.denied(self.board_tool()))

    # --- potential failures ---

    def test_lookalikes_do_not_rename(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.event('UserPromptSubmit', prompt='go')
        for prompt in ('/board renamed', 'please /board rename X', '/board renaming', 'rename it'):
            self.assertIsNone(self.title(self.rename(prompt, session_title='Mine')), prompt)

    def test_nothing_to_name_after_gives_a_note_not_a_blank_title(self):
        reply = self.rename()
        self.assertIsNone(self.title(reply))
        self.assertIn('rename', self.note(reply).lower())
        self.linked()
        reply = self.rename()
        self.assertIsNone(self.title(reply))
        self.assertIn('job', self.note(reply).lower())

    def test_control_only_text_falls_back_to_work_title(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        self.assertEqual(self.title(self.rename('/board rename \x07\x1b')), 'Reading hooks')

    def test_explicit_text_is_sanitized_and_bounded(self):
        title = self.title(self.rename('/board rename ' + 'y' * 200))
        self.assertEqual(len(title), 80)

    def test_codex_gets_a_note_and_no_title(self):
        self.adapter.harness = 'codex'
        self.adapter.ensure_desktop_wake = lambda *args, **kwargs: None
        reply = self.event('UserPromptSubmit', 'codex-chat', prompt='$board rename Something')
        self.assertIsNone(self.title(reply))
        self.assertIn('Codex', self.note(reply))

    def test_rename_keeps_inbox_context(self):
        self.linked()
        self.call('board_activity', action='update', title='Reading hooks')
        sender = self.hub.call('register', {'name': 'Peer'})
        self.hub.call('inbox_send', {**sender, 'recipient_agent_id': self.agent['agent_id'], 'body': 'ping'})
        reply = self.rename()
        self.assertEqual(self.title(reply), 'Reading hooks')
        self.assertIn('ping', self.note(reply))

    def test_rename_does_not_authorize_reset(self):
        self.linked()
        self.rename('/board rename reset')
        reply = self.event('PreToolUse', tool_name='mcp__board__board_reset', tool_input={})
        self.assertTrue(self.denied(reply))


class LazyBridgeTests(unittest.TestCase):
    def test_bridge_registers_nobody_until_a_board_tool_is_used(self):
        from board.server import serve
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            hub = Hub(root / 'hub.sqlite', root)
            server = serve(hub, 0)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                url = f'http://127.0.0.1:{server.server_port}'
                argv = [sys.executable, str(ROOT / 'run_board.py'), 'mcp', '--url', url, '--name', 'claude-code',
                        '--workspace', str(root), '--database', str(hub.database)]
                quiet = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
                         {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'}]
                run = subprocess.run(argv, input=''.join(json.dumps(r) + '\n' for r in quiet), cwd=root,
                                     capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                replies = [json.loads(line)['result'] for line in run.stdout.splitlines()]
                self.assertIn('/board', replies[0]['instructions'])
                self.assertTrue(replies[1]['tools'])
                with hub.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 0)
                used = quiet + [{'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
                                 'params': {'name': 'board_status', 'arguments': {'target_agent_id': 'self', 'include_rules': False}}}]
                run = subprocess.run(argv, input=''.join(json.dumps(r) + '\n' for r in used), cwd=root,
                                     capture_output=True, text=True, encoding='utf-8', timeout=20)
                self.assertEqual(run.returncode, 0, run.stderr)
                status = json.loads(run.stdout.splitlines()[2])['result']
                self.assertFalse(status['isError'], status)
                self.assertRegex(json.loads(status['content'][0]['text'])['your_agent_id'], '^[0-9a-f]{16}$')
                with hub.connect() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 1)
            finally:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
