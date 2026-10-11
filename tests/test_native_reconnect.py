import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from board.core import Hub, BoardError
from board.hooks import Adapter
from board.mcp import reconnect_identity


class NativeReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        self.old = self.hub.call('register', {'name':'codex'})
        self.args = SimpleNamespace(database=self.hub.database, workspace=str(self.root), url='test')
        self.caller = lambda url, method, params: self.hub.call(method, params)
        self.adapter = Adapter(self.root, self.hub.database, 'codex', caller=self.caller)
        self.session = 'native-chat-one'
        self.hub.call('board_desktop_wake', {**self.old, 'action':'enable', 'thread_id':self.session,
                                            'workspace':str(self.root), 'reason':'verified native context'})
        self.bind(self.old)

    def bind(self, who, session=None):
        return self.adapter.handle({'session_id':session or self.session, 'cwd':str(self.root),
                                    'hook_event_name':'PostToolUse', 'tool_name':'mcp__board__board_status',
                                    'tool_response':{'your_agent_id':who['agent_id']}})

    def test_repeated_reconnect_keeps_original_identity_inbox_binding_and_custody(self):
        self.hub.call('claim', {**self.old, 'paths':['a.py'], 'reason':'existing work'})
        for number in range(3):
            fresh = self.hub.call('register', {'name':'codex'})
            msg = self.hub.call('inbox_send', {**fresh, 'recipient_agent_id':self.old['agent_id'], 'body':f'event {number}'})['id']
            self.assertIn('same native chat', str(self.bind(fresh)))
            reused = reconnect_identity(self.args, fresh, self.caller)
            self.assertEqual(reused['agent_id'], self.old['agent_id'])
            with self.assertRaisesRegex(BoardError, 'retired'):
                self.hub.call('board_status', fresh)
            inbox = self.hub.call('inbox_read', reused)
            self.assertIn(msg, [r['id'] for r in inbox['messages']])
            self.hub.call('inbox_ack', {**reused, 'message_ids':[msg]})
            self.assertEqual(self.hub.call('board_desktop_wake', {**reused, 'action':'status'})['thread_id'], self.session)
            self.assertEqual(self.hub.call('board_status', {**reused, 'target_agent_id':'self'})['claims'][0]['agent'], self.old['agent_id'])

    def test_other_native_chat_never_reuses_this_identity(self):
        fresh = self.hub.call('register', {'name':'codex'})
        self.bind(fresh, session='native-chat-two')
        self.assertEqual(reconnect_identity(self.args, fresh, self.caller)['agent_id'], fresh['agent_id'])

    def test_invalidated_predecessor_is_not_revived(self):
        fresh = self.hub.call('register', {'name':'codex'})
        self.bind(fresh)
        self.hub.call('retire', {**fresh, 'target_agent_id':self.old['agent_id'], 'reason':'owner retired'})
        with self.assertRaisesRegex(BoardError, 'no longer valid'):
            reconnect_identity(self.args, fresh, self.caller)

    def test_provisional_work_prevents_silent_identity_change(self):
        fresh = self.hub.call('register', {'name':'codex'})
        self.bind(fresh)
        self.hub.call('claim', {**fresh, 'paths':['new.py'], 'reason':'already edited'})
        with self.assertRaisesRegex(BoardError, 'already owns work'):
            reconnect_identity(self.args, fresh, self.caller)

    def test_claude_reconnect_uses_exclusive_native_hook_binding(self):
        adapter = Adapter(self.root, self.hub.database, 'claude', caller=self.caller)
        who = self.hub.call('register', {'name':'claude'})
        def link(identity):
            return adapter.handle({'session_id':'claude-chat','cwd':str(self.root),'hook_event_name':'PostToolUse',
                                   'tool_name':'mcp__board__board_status','tool_response':{'your_agent_id':identity['agent_id']}})
        link(who)
        fresh = self.hub.call('register', {'name':'claude'})
        self.assertIn('same native chat', str(link(fresh)))
        self.assertEqual(reconnect_identity(self.args, fresh, self.caller)['agent_id'], who['agent_id'])
