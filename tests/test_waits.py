import json
import tempfile
import time
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.hooks import Adapter
from board.inbox_watch import watch_inbox
from board.waits import poll


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **params):
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def messages(self, who):
        return self.call(who, 'inbox_read')['messages']

    def subscribe(self, **params):
        return self.call(self.a, 'board_wait', action='subscribe', **params)

    def test_ack_wait_is_opt_in_and_catches_already_acknowledged(self):
        first = self.call(self.a, 'inbox_send', recipient_agent_id=self.b['agent_id'], body='First')['id']
        self.call(self.b, 'inbox_ack', message_ids=[first])
        self.assertEqual(self.messages(self.a), [])
        self.subscribe(kind='message_ack', message_id=first)
        self.assertEqual(len(self.messages(self.a)), 1)
        self.assertEqual(len(self.messages(self.a)), 1)
        with self.assertRaises(BoardError):
            self.call(self.b, 'board_wait', action='subscribe', kind='message_ack', message_id=first)

    def test_later_ack_fires_once(self):
        message = self.call(self.a, 'inbox_send', recipient_agent_id=self.b['agent_id'], body='Question')['id']
        one = self.subscribe(kind='message_ack', message_id=message)
        two = self.subscribe(kind='message_ack', message_id=message)
        self.assertEqual(one['wait']['id'], two['wait']['id'])
        self.assertEqual(self.messages(self.a), [])
        self.call(self.b, 'inbox_ack', message_ids=[message])
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_mutex_release_and_expiry_fire_without_reserving_it(self):
        claim = self.call(self.b, 'mutex_claim', resources=['db:test'], reason='using database')
        self.subscribe(kind='mutex_free', resource='db:test')
        self.call(self.b, 'release', claim_ids=claim['claim_ids'], reason='done')
        self.assertEqual(len(self.messages(self.a)), 1)
        self.call(self.b, 'mutex_claim', resources=['db:test'], reason='using again')
        self.subscribe(kind='mutex_free', resource='db:test')
        with self.hub.connect() as db:
            db.execute('UPDATE claims SET expires=0')
        poll(str(self.db), self.root)
        self.assertEqual(len(self.messages(self.a)), 2)

    def test_cancel_and_timeout(self):
        self.call(self.b, 'mutex_claim', resources=['db:test'], reason='occupied')
        wait = self.subscribe(kind='mutex_free', resource='db:test')['wait']
        self.call(self.a, 'board_wait', action='cancel', wait_id=wait['id'])
        with self.assertRaises(BoardError):
            self.call(self.b, 'board_wait', action='cancel', wait_id=wait['id'])
        timed = self.subscribe(kind='mutex_free', resource='db:test')['wait']
        with self.hub.connect() as db:
            db.execute('UPDATE board_waits SET expires=0 WHERE id=?', (timed['id'],))
        poll(str(self.db), self.root)
        self.assertIn('timed out', self.messages(self.a)[0]['body'])
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_file_wait_includes_queue_locks(self):
        path = self.root / 'common.txt'
        path.write_text('base\n')
        batch = self.call(self.b, 'patch_queue', action='join', path=str(path), reason='shared')['batch']['id']
        self.subscribe(kind='mutex_free', path=str(path))
        self.assertEqual(self.messages(self.a), [])
        self.call(self.b, 'patch_queue', action='release', batch_id=batch)
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_final_batch_does_not_duplicate_participant_message(self):
        path = self.root / 'common.txt'
        path.write_text('base\n')
        batch = self.call(self.a, 'patch_queue', action='join', path=str(path), reason='queue')['batch']['id']
        self.subscribe(kind='patch_finalized', batch_id=batch)
        self.call(self.b, 'board_wait', action='subscribe', kind='patch_finalized', batch_id=batch)
        self.call(self.a, 'patch_queue', action='release', batch_id=batch)
        self.assertEqual(len(self.messages(self.a)), 1)
        self.assertEqual(len(self.messages(self.b)), 1)
        self.assertIn('finalized', self.messages(self.a)[0]['body'])

    def test_claude_watcher_wakes_for_opt_in_mutex_expiration(self):
        payload = {'session_id': 'wait-watch', 'cwd': str(self.root), 'hook_event_name': 'Stop'}
        adapter = Adapter(self.root, self.db, 'claude', caller=lambda url, method, args: self.hub.call(method, args))
        adapter.handle({**payload, 'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__board__board_status', 'tool_response': {'your_agent_id': self.a['agent_id']}})
        self.call(self.b, 'mutex_claim', resources=['db:test'], reason='busy')
        self.subscribe(kind='mutex_free', resource='db:test')
        with self.hub.connect() as db:
            db.execute('UPDATE claims SET expires=0')
        result = watch_inbox(self.root, self.db, payload, seconds=.2, poll_seconds=.01)
        self.assertIn('mutex_free', result)
        self.assertIsNone(watch_inbox(self.root, self.db, payload, seconds=.02, poll_seconds=.01))

    def test_inheritance_preserves_pending_wait_and_retirement_cancels(self):
        message = self.call(self.a, 'inbox_send', recipient_agent_id=self.b['agent_id'], body='Question')['id']
        self.subscribe(kind='message_ack', message_id=message)
        successor = self.hub.call('register', {'name': 'new session'})
        self.call(successor, 'inherit', target_agent_id=self.a['agent_id'], inheritance_secret=self.a['inheritance_secret'], reason='continue')
        self.call(self.b, 'inbox_ack', message_ids=[message])
        self.assertEqual(len(self.messages(successor)), 1)
        self.call(self.b, 'mutex_claim', resources=['db:test'], reason='busy')
        self.call(successor, 'board_wait', action='subscribe', kind='mutex_free', resource='db:test')
        self.call(self.b, 'retire', target_agent_id=successor['agent_id'], reason='Owner requested')
        with self.hub.connect() as db:
            self.assertFalse(db.execute("SELECT 1 FROM board_waits WHERE agent=? AND state='WAITING'", (successor['agent_id'],)).fetchone())
