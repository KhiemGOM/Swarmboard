import tempfile
import time
import unittest
from pathlib import Path

from board.core import Hub
from board.desktop_wake import initialize as initialize_wake
from board.sessions import bind


class AgentStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.caller = self.hub.call('register', {'name': 'caller'})
        self.peer = self.hub.call('register', {'name': 'peer'})

    def status(self, **params):
        return self.hub.call('board_status', {**self.caller, 'include_rules': False, **params})

    def test_unbound_agent_reports_unknown_metadata(self):
        row = self.status(target_agent_id='self')['agents'][0]
        self.assertEqual(row['harnesses'], [])
        for field in ('harness', 'native_chat_id', 'desktop_wake', 'last_wake_delivery', 'model', 'running'):
            self.assertIsNone(row[field])
        self.assertEqual(row['native_sessions'], [])
        self.assertEqual(row['hook_bindings'], [])
        self.assertEqual(row['current_jobs'], [])

    def test_peer_metadata_is_visible_without_exposing_mutation_credentials(self):
        ident = self.peer['agent_id']
        now = time.time()
        with self.hub.connect() as db:
            bind(db, 'codex', 'session-hash', ident)
            db.execute('CREATE TABLE hook_inbox_bindings (harness TEXT,agent TEXT,session_key TEXT)')
            db.execute('INSERT INTO hook_inbox_bindings VALUES (?,?,?)', ('codex', ident, 'session-hash'))
            initialize_wake(db)
            db.execute('INSERT INTO desktop_wake_bindings(agent,thread_id,enabled,cursor,reason) VALUES (?,?,1,0,?)', (ident, 'native-chat-id', 'native binding'))
            db.execute('INSERT INTO desktop_wake_deliveries(token,agent,thread_id,relay,through_id,state,created,detail) VALUES (?,?,?,?,?,?,?,?)',
                       ('delivery-secret', ident, 'native-chat-id', 'hub', 3, 'DELIVERED', now, 'queued'))
            db.execute('INSERT INTO messages(sender,recipient,body,created) VALUES (?,?,?,?)', (self.caller['agent_id'], ident, 'hello', now))
            db.execute("INSERT INTO jobs(title,kind,state,owner,lease_token,expires,progress,created,updated) VALUES (?,'code','CLAIMED',?,?,?,?,?,?)",
                       ('Build agent list', ident, 'lease-secret', now + 1000, 'reviewing', now, now))
        row = self.status(target_agent_id=ident)['agents'][0]
        self.assertEqual(row['harness'], 'codex')
        self.assertEqual(row['native_sessions'], [{'harness': 'codex', 'session_key': 'session-hash'}])
        self.assertEqual(row['hook_bindings'], row['native_sessions'])
        self.assertEqual(row['native_chat_id'], 'native-chat-id')
        self.assertTrue(row['desktop_wake']['enabled'])
        self.assertEqual(row['last_wake_delivery']['state'], 'DELIVERED')
        self.assertEqual(row['unread_inbox'], 1)
        self.assertEqual(row['job_counts'], {'CLAIMED': 1})
        self.assertEqual(row['current_jobs'][0]['progress'], 'reviewing')
        rendered = str(row)
        for secret in (self.peer['session_token'], 'delivery-secret', 'lease-secret'):
            self.assertNotIn(secret, rendered)
        self.assertEqual(self.status(target_agent_id='self')['agents'][0]['harnesses'], [])

    def test_claude_binding_and_path_filter(self):
        ident = self.peer['agent_id']
        with self.hub.connect() as db:
            bind(db, 'claude', 'claude-session-hash', ident)
        self.hub.call('claim', {**self.peer, 'paths': ['a.py'], 'reason': 'work'})
        rows = self.status(paths=['a.py'])['agents']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], ident)
        self.assertEqual(rows[0]['harness'], 'claude')
        self.assertIsNone(rows[0]['native_chat_id'])
