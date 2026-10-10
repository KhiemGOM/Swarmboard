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
from board.inbox_watch import watch_inbox
from install_hooks import install


class InboxWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'hub.sqlite'
        self.hub = Hub(self.database, self.root)
        self.recipient = self.hub.call('register', {'name': 'Recipient'})
        self.sender = self.hub.call('register', {'name': 'Sender'})
        self.payload = {'session_id': 'watch-test', 'cwd': str(self.root), 'hook_event_name': 'Stop'}
        adapter = Adapter(self.root, self.database, 'claude', caller=lambda url, method, params: self.hub.call(method, params))
        adapter.handle({**self.payload, 'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__board__board_status',
                        'tool_response': {'your_agent_id': self.recipient['agent_id']}})

    def tearDown(self):
        self.temp.cleanup()

    def send(self, body='New message'):
        return self.hub.call('inbox_send', {**self.sender, 'recipient_agent_id': self.recipient['agent_id'], 'body': body})

    def watch(self, seconds=.05):
        return watch_inbox(self.root, self.database, self.payload, seconds=seconds, poll_seconds=.01)

    def test_wakes_once_per_new_message_without_acknowledging(self):
        self.send()
        self.assertIn('New message', self.watch())
        self.assertIsNone(self.watch())
        self.assertEqual(self.hub.call('inbox_read', self.recipient)['unread_inbox'], 1)
        self.send('Second message')
        self.assertIn('Second message', self.watch())
        self.assertEqual(self.hub.call('inbox_read', self.recipient)['unread_inbox'], 2)

    def test_waits_for_message_arriving_after_start(self):
        sender = threading.Timer(.05, self.send)
        sender.start()
        try:
            self.assertIn('New message', self.watch(seconds=1))
        finally:
            sender.join()

    def test_active_watcher_is_not_duplicated_and_stale_lease_recovers(self):
        lock = next((self.root / 'hook-sessions').glob('*.json')).with_suffix('.watch.lock')
        lock.write_text(str(time.time() + 60))
        self.send()
        self.assertIsNone(self.watch())
        lock.write_text('0')
        self.assertIn('New message', self.watch())
        self.assertFalse(lock.exists())

    def test_other_recipients_and_acknowledged_messages_do_not_wake(self):
        self.hub.call('inbox_send', {**self.recipient, 'recipient_agent_id': self.sender['agent_id'], 'body': 'Other inbox'})
        message = self.send()
        self.hub.call('inbox_ack', {**self.recipient, 'message_ids': [message['id']]})
        self.assertIsNone(self.watch())

    def test_retired_disconnected_and_unbound_sessions_do_not_wake(self):
        self.send()
        self.hub.call('disconnect', {**self.recipient, 'reason': 'Session closed'})
        self.assertIsNone(self.watch())
        self.hub.call('board_status', self.recipient)
        self.hub.call('retire', {**self.sender, 'target_agent_id': self.recipient['agent_id'], 'reason': 'Owner requested retirement'})
        self.assertIsNone(self.watch())
        for path in (self.root / 'hook-sessions').glob('*.json'):
            path.unlink()
        self.assertIsNone(self.watch())

    def test_installer_adds_claude_async_rewake_and_preserves_other_handlers(self):
        config = self.root / 'settings.json'
        config.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'other'}]}]}}))
        for _ in range(2):
            install(config, sys.executable, ['swarmboard_hook.py', '--harness', 'claude'], watch=True)
        handlers = [handler for entry in json.loads(config.read_text())['hooks']['Stop'] for handler in entry['hooks']]
        self.assertEqual(len(handlers), 3)
        watcher = next(handler for handler in handlers if handler.get('asyncRewake'))
        self.assertIn('--watch-inbox', watcher['args'])
        self.assertEqual(watcher['timeout'], 3605)

    def test_entrypoint_emits_documented_exit_two_only_for_new_delivery(self):
        self.send()
        command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'swarmboard_hook.py'),
                   '--workspace', str(self.root), '--database', str(self.database), '--harness', 'claude', '--watch-inbox', '--watch-seconds', '.1']
        first = subprocess.run(command, input=json.dumps(self.payload), text=True, capture_output=True, timeout=5)
        self.assertEqual(first.returncode, 2, first.stderr)
        self.assertIn('New message', first.stderr)
        repeated = subprocess.run(command, input=json.dumps(self.payload), text=True, capture_output=True, timeout=5)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, '')
