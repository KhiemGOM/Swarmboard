import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from board.core import BoardError, Hub
from board.hooks import Adapter
from board.mcp import identify


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root/'hub.sqlite', self.root)
        self.args = SimpleNamespace(name='claude', url='test')
        self.caller = lambda url, method, params: self.hub.call(method, params)
        self.owner = self.hub.call('register', {'name': 'Original'})
        self.peer = self.hub.call('register', {'name': 'Peer'})

    def select(self, arguments):
        with patch.dict('os.environ', {}, clear=True):
            return identify(self.args, arguments, self.caller)

    def test_agent_id_alone_resumes_existing_work_and_inbox_without_secret(self):
        claims = self.hub.call('claim', {**self.owner, 'paths': ['owned.py'], 'reason': 'work'})
        job = self.hub.call('job_post_and_claim', {**self.owner, 'title': 'work', 'description': 'work', 'acceptance': 'done'})['job']
        msg = self.hub.call('inbox_send', {**self.peer, 'to': self.owner['agent_id'], 'body': 'pending'})
        resumed = self.select({'agent_id': self.owner['agent_id']})
        self.assertEqual(resumed['agent_id'], self.owner['agent_id'])
        status = self.hub.call('board_status', {**resumed, 'target_agent_id': 'self'})
        self.assertEqual([r['id'] for r in status['claims']], claims['claim_ids'])
        self.assertEqual(status['your_name'], 'Original')
        self.assertEqual(self.hub.call('inbox_read', resumed)['messages'][0]['id'], msg['id'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT lease_token FROM jobs WHERE id=?', (job['id'],)).fetchone()[0], job['lease_token'])

    def test_retired_agent_can_resume_by_id_without_restoring_released_work(self):
        self.hub.call('claim', {**self.owner, 'paths': ['released.py'], 'reason': 'work'})
        self.hub.call('retire', {**self.owner, 'reason': 'done'})
        resumed = self.select({'agent_id': self.owner['agent_id']})
        self.assertEqual(resumed['agent_id'], self.owner['agent_id'])
        status = self.hub.call('board_status', {**resumed, 'target_agent_id': 'self'})
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['state'], 'HELD')

    def test_native_chat_id_alone_reuses_binding_without_other_credentials(self):
        args = {'chat_id': 'native-chat', 'harness': 'claude'}
        first = self.select(args)
        second = self.select(args)
        self.assertEqual(first['agent_id'], second['agent_id'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 3)

    def test_environment_native_id_is_automatic(self):
        with patch.dict('os.environ', {'CLAUDE_CODE_SESSION_ID': 'native-env'}, clear=True):
            first = identify(self.args, {}, self.caller)
            second = identify(self.args, {}, self.caller)
        self.assertEqual(first['agent_id'], second['agent_id'])

    def test_declaration_updates_stale_binding_without_transferring_peer_work(self):
        old = self.select({'chat_id': 'native-chat'})
        self.hub.call('claim', {**old, 'paths': ['peer.py'], 'reason': 'peer work'})
        selected = self.select({'agent_id': self.owner['agent_id'], 'chat_id': 'native-chat'})
        self.assertEqual(selected['agent_id'], self.owner['agent_id'])
        self.assertEqual(self.select({'chat_id': 'native-chat'})['agent_id'], self.owner['agent_id'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT agent FROM claims WHERE path='peer.py'").fetchone()[0], old['agent_id'])
            self.assertEqual(db.execute('SELECT state FROM agents WHERE id=?', (old['agent_id'],)).fetchone()[0], 'active')

    def test_unknown_and_invalid_ids_do_not_create_unrelated_identity(self):
        for arguments in ({'agent_id': 'missing'}, {'agent_id': None}, {'agent_id': ''}, {}, {'harness': 'claude'}, {'chat_id': 'bad\nid'}):
            with self.subTest(arguments=arguments), self.assertRaises(BoardError):
                self.select(arguments)
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 2)

    def test_hook_accepts_declared_identity_without_redirecting_to_stale_binding(self):
        adapter = Adapter(self.root, self.hub.database, 'claude', caller=self.caller)
        session = 'native-hook'
        def link(agent, tool):
            return adapter.handle({'cwd': str(self.root), 'session_id': session, 'hook_event_name': 'PostToolUse',
                                   'tool_name': 'mcp__board__'+tool, 'tool_response': {'your_agent_id': agent}})
        link(self.peer['agent_id'], 'board_status')
        resumed = self.select({'agent_id': self.owner['agent_id']})
        response = link(resumed['agent_id'], 'board_identity')
        self.assertNotIn('next board tool call', str(response))
        key = hashlib.sha256(('claude:'+session).encode()).hexdigest()
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT agent FROM native_sessions WHERE session_key=?', (key,)).fetchone()[0], resumed['agent_id'])
        status = self.hub.call('board_status', {**resumed, 'target_agent_id': 'self'})
        self.assertEqual(status['agents'][0]['id'], resumed['agent_id'])

    def test_resume_keeps_other_agents_file_conflicts(self):
        self.hub.call('claim', {**self.peer, 'paths': ['contended.py'], 'reason': 'peer'})
        resumed = self.select({'agent_id': self.owner['agent_id']})
        self.assertFalse(self.hub.call('claim', {**resumed, 'paths': ['contended.py'], 'reason': 'mine'})['accepted'])

    def test_codex_declaration_replaces_stale_wake_binding_without_moving_work(self):
        self.hub.call('board_desktop_wake', {**self.owner, 'action': 'enable', 'thread_id': 'old-codex-chat',
            'workspace': str(self.root), 'reason': 'previous chat'})
        adapter = Adapter(self.root, self.hub.database, 'codex', caller=self.caller)
        def link(agent, tool):
            return adapter.handle({'cwd': str(self.root), 'session_id': 'current-codex-chat',
                'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__board__'+tool,
                'tool_response': {'your_agent_id': agent}})
        link(self.peer['agent_id'], 'board_status')
        self.hub.call('claim', {**self.peer, 'paths': ['peer.py'], 'reason': 'peer'})
        selected = self.select({'agent_id': self.owner['agent_id']})
        link(selected['agent_id'], 'board_identity')
        wake = self.hub.call('board_desktop_wake', {**selected, 'action': 'status'})
        self.assertTrue(wake['enabled'])
        self.assertEqual(wake['thread_id'], 'current-codex-chat')
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT agent FROM claims WHERE path='peer.py'").fetchone()[0], self.peer['agent_id'])
