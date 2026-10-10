import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.codex_wake import Proxy, Rejected, Watcher


class FakeRPC:
    def __init__(self, root, status='idle', error=None):
        self.root, self.status, self.error = root, status, error
        self.starts = []

    def call(self, method, params):
        if method == 'thread/read':
            return {'thread': {'id': 'chat', 'cwd': str(self.root), 'status': {'type': self.status}}}
        self.starts.append(params)
        if self.error:
            raise self.error
        return {'turn': {'id': 'turn'}}


class WakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'hub.sqlite'
        self.hub = Hub(self.database, self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})
        binding = self.root / 'hook-sessions' / (hashlib.sha256(b'codex:chat').hexdigest() + '.json')
        binding.parent.mkdir()
        binding.write_text(json.dumps({'agent_id': self.a['agent_id']}))
        self.watcher = Watcher(self.database, self.root, self.a['agent_id'], 'chat')
        self.rpc = FakeRPC(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def message(self):
        return self.hub.call('inbox_send', {**self.b, 'recipient_agent_id': self.a['agent_id'], 'body': 'Peer event'})

    def test_confirmed_delivery_batches_without_ack_or_human_input(self):
        self.watcher.step(self.rpc)
        self.message()
        self.message()
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'delivered')
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'waiting')
        self.assertEqual(len(self.rpc.starts), 1)
        event = self.rpc.starts[0]
        self.assertEqual(event['input'], [])
        self.assertEqual(event['toolOutput']['namespace'], 'swarmboard')
        self.assertNotIn('Peer event', event['toolOutput']['output'])
        with sqlite3.connect(self.database) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages WHERE acknowledged IS NULL').fetchone()[0], 2)
        restarted = Watcher(self.database, self.root, self.a['agent_id'], 'chat', True)
        self.assertEqual(restarted.step(self.rpc)['status'], 'waiting')

    def test_backlog_is_opt_in(self):
        self.message()
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'waiting')
        self.watcher.path.unlink()
        self.watcher = Watcher(self.database, self.root, self.a['agent_id'], 'chat', True)
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'delivered')

    def test_active_thread_waits_and_delivers_when_idle(self):
        self.watcher.step(self.rpc)
        self.message()
        self.rpc.status = 'active'
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'waiting')
        self.rpc.status = 'idle'
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'delivered')

    def test_uncertain_delivery_survives_restart_without_retry(self):
        self.watcher.step(self.rpc)
        self.message()
        self.rpc.error = OSError('Lost response')
        with self.assertRaisesRegex(BoardError, 'uncertain'):
            self.watcher.step(self.rpc)
        restarted = Watcher(self.database, self.root, self.a['agent_id'], 'chat')
        with self.assertRaisesRegex(BoardError, 'uncertain'):
            restarted.step(self.rpc)
        self.assertEqual(len(self.rpc.starts), 1)

    def test_explicit_rejection_keeps_cursor_and_clears_pending(self):
        self.watcher.step(self.rpc)
        self.message()
        self.rpc.error = Rejected('unsupported')
        with self.assertRaises(Rejected):
            self.watcher.step(self.rpc)
        self.assertNotIn('pending', json.loads(self.watcher.path.read_text()))
        self.rpc.error = None
        self.assertEqual(self.watcher.step(self.rpc)['status'], 'delivered')

    def test_wrong_binding_workspace_unloaded_and_retired_fail_closed(self):
        self.watcher.binding.write_text(json.dumps({'agent_id': self.b['agent_id']}))
        with self.assertRaisesRegex(BoardError, 'binding'):
            self.watcher.step(self.rpc)
        self.watcher.binding.write_text(json.dumps({'agent_id': self.a['agent_id']}))
        self.rpc.root = self.root / 'other'
        with self.assertRaisesRegex(BoardError, 'workspace mismatch'):
            self.watcher.step(self.rpc)
        self.rpc.root, self.rpc.status = self.root, 'notLoaded'
        with self.assertRaisesRegex(BoardError, 'not loaded'):
            self.watcher.step(self.rpc)
        self.hub.retire_local(self.a['agent_id'], 'Owner declared unavailable')
        with self.assertRaisesRegex(BoardError, 'unavailable'):
            self.watcher.step(self.rpc)
        self.assertEqual(self.rpc.starts, [])

    def test_duplicate_worker_refused_and_normal_exit_cleans_lock(self):
        self.watcher.lock.touch()
        with self.assertRaisesRegex(BoardError, 'lock exists'):
            self.watcher.run(self.rpc, once=True)
        self.watcher.lock.unlink()
        self.assertEqual(self.watcher.run(self.rpc, once=True)['status'], 'waiting')
        self.assertFalse(self.watcher.lock.exists())

    def test_proxy_protocol_roundtrip_and_interactive_requests_are_rejected(self):
        script = '''import sys,json
for line in sys.stdin:
    message=json.loads(line)
    if message.get('method')=='initialize':
        print(json.dumps({'id':message['id'],'result':{}}),flush=True)
    elif message.get('method')=='thread/read':
        print(json.dumps({'method':'approval/request','id':'approval'}),flush=True)
        answer=json.loads(sys.stdin.readline())
        print(json.dumps({'id':message['id'],'result':{'denied': 'error' in answer}}),flush=True)
'''
        real_popen = subprocess.Popen
        with patch('board.codex_wake.subprocess.Popen', side_effect=lambda command, **kw:
                   real_popen([sys.executable, '-u', '-c', script], **kw)) as launched:
            rpc = Proxy('exact-codex', 'exact-socket')
            try:
                self.assertEqual(rpc.call('thread/read', {}), {'denied': True})
                self.assertEqual(launched.call_args.args[0], ['exact-codex', 'app-server', 'proxy', '--sock', 'exact-socket'])
            finally:
                rpc.close()


if __name__ == '__main__':
    unittest.main()
