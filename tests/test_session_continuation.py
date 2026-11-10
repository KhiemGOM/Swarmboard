import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import Hub
from board.hooks import Adapter

HOST = 'local_98a6d872-b8e1-44db-bc44-4fa430bb4aea'


def key(session, harness='claude'):
    return hashlib.sha256((harness + ':' + session).encode()).hexdigest()


class SessionContinuationTests(unittest.TestCase):
    """Claude Desktop can switch a chat to a new CLI session ID mid-conversation."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / '.board' / 'hub.sqlite'
        self.database.parent.mkdir()
        self.hub = Hub(self.database, self.root)
        self.agent = self.hub.call('register', {'name': 'Plot Twist', 'native_harness': 'claude', 'native_session_key': key('old-cli')})
        self.state('old-cli', {'activated': True, 'agent_id': self.agent['agent_id'], 'linked': self.agent['agent_id']})

    def state_path(self, session):
        return self.database.parent / 'hook-sessions' / (key(session) + '.json')

    def state(self, session, value=None):
        path = self.state_path(session)
        if value is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value))
        return json.loads(path.read_text()) if path.exists() else None

    def hook(self, session, event='PreToolUse', tool='mcp__board__board_status', host=HOST, harness='claude'):
        payload = {'cwd': str(self.root), 'session_id': session, 'hook_event_name': event, 'tool_name': tool, 'tool_input': {}}
        if event == 'UserPromptSubmit':
            payload = {**payload, 'prompt': 'hello'}
        environ = {name: value for name, value in os.environ.items() if name != 'CLAUDE_CODE_HOST_SESSION_ID'}
        if host is not None:
            environ['CLAUDE_CODE_HOST_SESSION_ID'] = host
        adapter = Adapter(self.root, self.database, harness, caller=lambda *a, **k: {'workspace': str(self.root)})
        with patch.dict('os.environ', environ, clear=True):
            return adapter.handle(payload)

    def bound_key(self):
        with self.hub.connect() as db:
            return db.execute("SELECT session_key FROM native_sessions WHERE agent=?", (self.agent['agent_id'],)).fetchone()[0]

    def remember_host(self):
        self.hook('old-cli', event='UserPromptSubmit')

    def denied(self, result):
        return result.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'

    # Basics
    def test_activated_chat_remembers_its_desktop_chat(self):
        self.remember_host()
        self.assertEqual(self.state('old-cli')['host_session'], HOST)

    def test_continued_session_keeps_the_board_and_identity(self):
        self.remember_host()
        result = self.hook('new-cli')
        self.assertFalse(self.denied(result), result)
        self.assertEqual(result['hookSpecificOutput']['updatedInput']['_route']['session_id'], 'new-cli')
        self.assertEqual(self.state('new-cli')['agent_id'], self.agent['agent_id'])
        self.assertTrue(self.state('new-cli')['activated'])
        self.assertEqual(self.bound_key(), key('new-cli'))
        again = self.hub.call('register', {'name': 'x', 'native_harness': 'claude', 'native_session_key': key('new-cli')})
        self.assertEqual(again['agent_id'], self.agent['agent_id'])

    def test_unchanged_session_is_untouched(self):
        self.remember_host()
        self.assertFalse(self.denied(self.hook('old-cli')))
        self.assertEqual(self.bound_key(), key('old-cli'))
        self.assertIsNone(self.state('new-cli'))

    # Must not carry over
    def test_forked_chat_has_another_desktop_id_and_stays_opted_out(self):
        self.remember_host()
        self.assertTrue(self.denied(self.hook('fork-cli', host='local_aaaaaaaa-0000-0000-0000-000000000000')))
        self.assertEqual(self.bound_key(), key('old-cli'))

    def test_without_a_desktop_id_nothing_carries_over(self):
        self.remember_host()
        self.assertTrue(self.denied(self.hook('new-cli', host=None)))
        self.assertEqual(self.bound_key(), key('old-cli'))

    def test_state_from_before_this_fix_has_no_desktop_id(self):
        self.assertTrue(self.denied(self.hook('new-cli')))

    def test_retired_identity_does_not_carry_over(self):
        self.remember_host()
        with self.hub.connect() as db:
            db.execute("UPDATE agents SET state='retired' WHERE id=?", (self.agent['agent_id'],))
        self.assertTrue(self.denied(self.hook('new-cli')))

    def test_identity_already_rebound_elsewhere_does_not_carry_over(self):
        self.remember_host()
        with self.hub.connect() as db:
            db.execute('UPDATE native_sessions SET session_key=? WHERE agent=?', (key('someone-else'), self.agent['agent_id']))
        self.assertTrue(self.denied(self.hook('new-cli')))
        self.assertEqual(self.bound_key(), key('someone-else'))

    def test_new_session_with_its_own_state_is_left_alone(self):
        self.remember_host()
        self.state('new-cli', {'activated': False})
        self.assertTrue(self.denied(self.hook('new-cli')))
        self.assertEqual(self.bound_key(), key('old-cli'))

    def test_malformed_desktop_ids_are_ignored(self):
        for host in ('../../x', 'local_' + 'a' * 300, 'local_bad id'):
            with self.subTest(host=host[:20]):
                self.hook('old-cli', event='UserPromptSubmit', host=host)
                self.assertNotEqual(self.state('old-cli').get('host_session'), host)

    def test_codex_hooks_ignore_the_desktop_id(self):
        self.hook('old-cli', event='UserPromptSubmit', harness='codex')
        self.assertNotIn('host_session', self.state('old-cli'))


if __name__ == '__main__':
    unittest.main()
