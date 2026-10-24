import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.hooks import Adapter
from board.mcp import TOOLS


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, who=None, **params):
        who = who or self.a
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def test_label_is_capped_persistent_and_independent_of_awake(self):
        self.call('board_activity', action='update', title='Running tests')
        status = self.call('board_activity', action='status')
        self.assertIsNone(status['awake'])
        self.assertEqual(status['activity']['title'], 'Running tests')
        for values in ({'title': 'x' * 61}, {'title': 'x', 'description': 'x' * 121}, {'title': 'x', 'awake': True}):
            with self.assertRaises(BoardError):
                self.call('board_activity', action='update', **values)
        other = self.call('board_activity', who=self.b, action='status')
        self.assertIsNone(other['activity'])
        self.hub = Hub(self.db, self.root)
        self.assertEqual(self.call('board_activity', action='status')['activity']['title'], 'Running tests')
        self.assertIsNone(self.call('board_activity', action='clear')['activity'])
        tool = next(t for t in TOOLS if t['name'] == 'board_activity')
        self.assertNotIn('awake', tool['inputSchema']['properties'])
        self.assertEqual(tool['inputSchema']['properties']['title']['maxLength'], 60)

    def test_native_hook_updates_awake_keeps_label_and_disconnect_is_idle(self):
        adapter = Adapter(self.root, self.db, 'claude', caller=lambda url, method, params: self.hub.call(method, params))
        def event(name, **params):
            return adapter.handle({'session_id': 'session-a', 'cwd': str(self.root), 'hook_event_name': name, **params})
        event('PostToolUse', tool_name='mcp__board__board_status', tool_response={'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': self.a['agent_id']})}]})
        self.call('board_activity', action='update', title='Fixing hooks')
        self.assertTrue(self.call('board_activity', action='status')['awake'])
        event('Stop')
        idle = self.call('board_activity', action='status')
        self.assertFalse(idle['awake'])
        self.assertEqual(idle['activity']['title'], 'Fixing hooks')
        event('UserPromptSubmit')
        self.assertTrue(self.call('board_activity', action='status')['awake'])
        event('SessionEnd', reason='closed')
        row = next(a for a in self.call('board_status', who=self.b)['agents'] if a['id'] == self.a['agent_id'])
        self.assertFalse(row['awake'])

    def test_wrong_native_session_or_token_cannot_set_awake(self):
        from board.activity import record
        from board.sessions import bind
        key = hashlib.sha256(b'codex:chat-a').hexdigest()
        auth = {'agent_id': self.a['agent_id'], 'session_token': self.a['session_token']}
        with self.hub.connect() as db:
            bind(db, 'codex', key, self.a['agent_id'])
            self.assertFalse(record(db, auth, 'codex', 'wrong', 'PostToolUse'))
            self.assertFalse(record(db, {**auth, 'session_token': 'wrong'}, 'codex', key, 'PostToolUse'))
            self.assertTrue(record(db, auth, 'codex', key, 'PostToolUse'))
        self.assertTrue(self.call('board_activity', action='status')['awake'])
