import argparse
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.workspace import switch


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.old, self.new = self.root / 'old', self.root / 'new'
        self.old.mkdir()
        self.new.mkdir()
        self.a = Hub(self.old / 'hub.sqlite', self.old)
        self.b = Hub(self.new / 'hub.sqlite', self.new)
        self.identity = self.a.call('register', {'name': 'caller'})
        self.args = argparse.Namespace(workspace=str(self.old), database=str(self.old / 'hub.sqlite'),
                                       url='old-url', name='caller')

    def call(self, method, **params):
        return self.a.call(method, {**params, 'agent_id': self.identity['agent_id'],
                                   'session_token': self.identity['session_token']})

    def run_switch(self, **extra):
        def routed(url, method, params):
            return (self.a if url == 'old-url' else self.b).call(method, params)
        with patch('board.workspace.workspace_connection', return_value=(str(self.new), str(self.new / 'hub.sqlite'), 'new-url')), \
             patch('board.workspace.ensure_hub'), patch('board.workspace.call', side_effect=routed):
            return switch(self.args, self.identity, {'workspace': str(self.new), 'reason': 'Wrong project', **extra})

    def test_switch_uses_new_identity_and_preserves_history(self):
        claims = self.call('claim', paths=['unfinished.txt'], reason='unfinished')['claim_ids']
        self.call('release', claim_ids=claims, held_reason='Work stays here')
        peer = self.a.call('register', {'name': 'peer'})
        self.a.call('inbox_send', {**peer, 'recipient_agent_id': self.identity['agent_id'], 'body': 'old inbox'})
        new_identity, result = self.run_switch()
        self.assertNotEqual(new_identity['agent_id'], self.identity['agent_id'])
        self.assertEqual(self.args.url, 'new-url')
        self.assertEqual(result['held_paths_retained'], 1)
        self.assertEqual(result['unread_inbox_retained'], 1)
        with self.a.connect() as db:
            self.assertEqual(db.execute('SELECT owner FROM custody').fetchone()[0], self.identity['agent_id'])
            self.assertIsNotNone(db.execute('SELECT disconnected FROM agents WHERE id=?', (self.identity['agent_id'],)).fetchone()[0])
        self.assertNotIn('previous_inheritance_secret', result)
        self.assertNotIn('inheritance_secret', result)
        self.assertNotIn('session_token', result)

    def test_active_mutex_refuses_and_keeps_old_connection(self):
        self.call('mutex_claim', resources=['git:index'], reason='commit')
        with self.assertRaisesRegex(BoardError, 'active work'):
            self.run_switch()
        self.assertEqual(self.args.url, 'old-url')
        self.assertEqual(len(self.call('board_status', target_agent_id='self', include_rules=False)['claims']), 1)
        with self.b.connect() as db:
            self.assertIsNotNone(db.execute('SELECT disconnected FROM agents').fetchone()[0])

    def test_claimed_job_and_live_patch_batch_refuse(self):
        job = self.call('job_post', title='Work', description='Do work', acceptance='Done')['job']['id']
        self.call('job_claim', job_id=job)
        with self.assertRaisesRegex(BoardError, 'active work'):
            self.call('workspace_leave', reason='switch')
        self.call('job_yield', job_id=job, lease_token=self.call('job_list')['jobs'][0]['lease_token'], reason='paused')
        (self.old / 'shared.txt').write_text('base\n')
        self.call('patch_queue', action='join', path='shared.txt', reason='patch')
        with self.assertRaisesRegex(BoardError, 'active work'):
            self.call('workspace_leave', reason='switch')

    def test_target_failure_leaves_old_identity_connected(self):
        with patch('board.workspace.ensure_hub', side_effect=BoardError('wrong port')):
            with self.assertRaisesRegex(BoardError, 'wrong port'):
                switch(self.args, self.identity, {'workspace': str(self.new), 'reason': 'correct project'})
        self.assertEqual(self.args.url, 'old-url')
        with self.a.connect() as db:
            self.assertIsNone(db.execute('SELECT disconnected FROM agents').fetchone()[0])

    def test_identity_injection_and_relative_paths_refused(self):
        with self.assertRaisesRegex(BoardError, 'only workspace and reason'):
            self.run_switch(agent_id='someone else')
        with self.assertRaisesRegex(BoardError, 'absolute'):
            switch(self.args, self.identity, {'workspace': 'relative', 'reason': 'switch'})

    def test_departure_cancels_wait_and_disables_wake(self):
        peer = self.a.call('register', {'name': 'holder'})
        self.a.call('mutex_claim', {**peer, 'resources': ['misc:test'], 'reason': 'busy'})
        self.call('board_wait', action='subscribe', kind='mutex_free', resource='misc:test')
        self.call('board_desktop_wake', action='enable', thread_id='test-chat', workspace=str(self.old),
                  reason='owner requested', owner_authorization='OWNER_REQUESTED_DESKTOP_WAKE')
        identity, result = self.run_switch()
        self.assertTrue(result['desktop_wake_preserved'])
        new_status = self.b.call('board_desktop_wake', {**identity, 'action': 'status'})
        self.assertTrue(new_status['enabled'])
        self.assertEqual(new_status['thread_id'], 'test-chat')
        with self.a.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM board_waits').fetchone()[0], 'CANCELLED')
            self.assertEqual(db.execute('SELECT enabled FROM desktop_wake_bindings').fetchone()[0], 0)

    def test_unbound_workspace_switch_requires_automatic_registration(self):
        identity, result = self.run_switch()
        status = self.b.call('board_desktop_wake', {**identity, 'action': 'status'})
        self.assertTrue(status['auto_enable_required'])
        self.assertFalse(result['desktop_wake_preserved'])

    def test_schema_and_hook_do_not_bind_foreign_identity(self):
        from board.mcp import TOOLS
        from board.hooks import Adapter
        tool = next(t for t in TOOLS if t['name'] == 'board_workspace')
        self.assertEqual(set(tool['inputSchema']['properties']), {'workspace', 'reason'})
        adapter = Adapter(self.old, self.old / 'hub.sqlite', 'codex')
        response = adapter.handle({'hook_event_name': 'PostToolUse', 'cwd': str(self.old),
            'session_id': 'session', 'tool_name': 'mcp__board__board_workspace',
            'tool_response': {'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': 'a' * 16})}]}})
        self.assertIn('hooks/watchers remain scoped', response['hookSpecificOutput']['additionalContext'])

    def test_deferred_desktop_stdio_does_not_use_launch_directory(self):
        from board.server import serve
        server = serve(self.b, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        route = self.new / '.codex/board-operator.json'
        route.parent.mkdir()
        route.write_text(json.dumps({'workspace': str(self.new), 'database': str(self.new / 'hub.sqlite'),
                                     'port': server.server_port}))
        entry = Path(__file__).resolve().parents[1] / 'run_board.py'
        requests = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'board_status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'board_workspace', 'arguments': {'workspace': str(self.new), 'reason': 'Actual project'}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'board_status', 'arguments': {'target_agent_id': 'self', 'include_rules': False}}},
        ]
        try:
            run = subprocess.run([sys.executable, str(entry), 'mcp', '--autostart', '--defer-workspace', '--name', 'claude-desktop'],
                                 input=''.join(json.dumps(r)+'\n' for r in requests), cwd=self.root,
                                 capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            replies = [json.loads(line)['result'] for line in run.stdout.splitlines()]
            self.assertIn('no project is selected', replies[0]['instructions'])
            self.assertTrue(replies[1]['tools'])
            self.assertTrue(replies[2]['isError'])
            self.assertIn('WORKSPACE_REQUIRED', replies[2]['content'][0]['text'])
            self.assertFalse(replies[3]['isError'])
            self.assertNotIn('inheritance_secret', json.loads(replies[3]['content'][0]['text']))
            self.assertFalse(replies[4]['isError'])
            selected = json.loads(replies[4]['content'][0]['text'])
            self.assertEqual(selected['workspace'], str(self.new))
            self.assertFalse((self.root / '.board').exists())
            with self.b.connect() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM agents').fetchone()[0], 1)
        finally:
            server.shutdown()
            server.server_close()

    def test_live_stdio_switch_routes_following_calls_and_leaves_peer_unchanged(self):
        from board.server import serve
        servers = [serve(hub, 0) for hub in (self.a, self.b)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        route = self.new / '.board/operator.json'
        route.parent.mkdir()
        route.write_text(json.dumps({'workspace': str(self.new), 'database': str(self.new / 'hub.sqlite'),
                                     'port': servers[1].server_port}))
        entry = Path(__file__).resolve().parents[1] / 'run_board.py'
        process = subprocess.Popen([sys.executable, str(entry), 'mcp', '--workspace', str(self.old),
            '--database', str(self.old / 'hub.sqlite'), '--url', f'http://127.0.0.1:{servers[0].server_port}'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
        def rpc(method, params):
            process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}) + '\n')
            process.stdin.flush()
            result = json.loads(process.stdout.readline())['result']
            if method == 'tools/call':
                self.assertFalse(result['isError'], result)
                return json.loads(result['content'][0]['text'])
            return result
        try:
            rpc('initialize', {'protocolVersion': '2025-11-25'})
            before = rpc('tools/call', {'name': 'board_status', 'arguments': {'target_agent_id': 'self', 'include_rules': False}})
            result = rpc('tools/call', {'name': 'board_workspace', 'arguments': {'workspace': str(self.new), 'reason': 'Wrong board'}})
            after = rpc('tools/call', {'name': 'board_status', 'arguments': {'target_agent_id': 'self', 'include_rules': False}})
            self.assertEqual(after['workspace'], str(self.new))
            self.assertEqual(after['your_agent_id'], result['your_agent_id'])
            self.assertNotEqual(before['your_agent_id'], after['your_agent_id'])
            self.assertEqual(self.call('board_status', target_agent_id='self', include_rules=False)['workspace'], str(self.old))
        finally:
            process.stdin.close()
            process.wait(timeout=5)
            process.stdout.close()
            process.stderr.close()
            for server in servers:
                server.shutdown()
                server.server_close()


if __name__ == '__main__':
    unittest.main()
