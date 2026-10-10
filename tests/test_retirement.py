import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.server import serve


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **params):
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def work(self):
        claim = self.call(self.a, 'claim', paths=['a.py'], resources=['git:index', 'port:3000'], reason='Work')
        job = self.call(self.a, 'job_post', title='Fix', description='Authorized repair', acceptance='Checks pass')['job']
        job = self.call(self.a, 'job_claim', job_id=job['id'])['job']
        self.call(self.a, 'job_update', job_id=job['id'], lease_token=job['lease_token'], progress='Regression written')
        return claim, job

    def test_self_retirement_releases_every_mutex_and_preserves_progress(self):
        claim, job = self.work()
        result = self.call(self.a, 'retire', reason='Owner requested self retirement')
        self.assertTrue(result['stop_session'])
        self.assertNotIn('custody_reminder', result)
        self.assertEqual(result['released_claim_ids'], claim['claim_ids'])
        with self.assertRaisesRegex(BoardError, 'retired'):
            self.call(self.a, 'board_status')
        status = self.call(self.b, 'board_status')
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['state'], 'HELD')
        recovered = self.call(self.b, 'job_list')['jobs'][0]
        self.assertEqual(recovered['state'], 'OPEN')
        self.assertEqual(recovered['progress'], 'Regression written')
        self.call(self.b, 'job_claim', job_id=job['id'])
        self.assertTrue(self.call(self.b, 'claim', paths=['a.py'], reason='Continue work')['accepted'])
        self.assertEqual(self.call(self.b, 'board_status')['custody'][0]['owner'], self.b['agent_id'])

    def test_local_retirement_works_without_daemon_or_credentials_and_is_idempotent(self):
        self.work()
        entry = Path(__file__).resolve().parents[1] / 'boardctl.py'
        args = [sys.executable, str(entry), 'retire', self.a['agent_id'], '--workspace', str(self.root), '--database', str(self.root / 'hub.sqlite')]
        first = subprocess.run(args, capture_output=True, text=True, check=True)
        self.assertFalse(json.loads(first.stdout)['already_retired'])
        second = subprocess.run(args, capture_output=True, text=True, check=True)
        self.assertTrue(json.loads(second.stdout)['already_retired'])
        self.assertEqual(len(self.call(self.b, 'job_list')['jobs']), 1)
        self.assertEqual(len(self.call(self.b, 'board_status')['agents']), 2)

    def test_targeted_retirement_does_not_transfer_locks_or_jobs_to_caller(self):
        self.work()
        result = self.call(self.b, 'retire', target_agent_id=self.a['agent_id'], reason='Owner declared A unavailable')
        self.assertFalse(result['stop_session'])
        self.assertEqual(self.call(self.b, 'board_status')['claims'], [])
        self.assertEqual(self.call(self.b, 'job_list')['jobs'][0]['state'], 'OPEN')

    def test_untracked_work_gets_recovery_job_and_bad_target_changes_nothing(self):
        self.call(self.a, 'claim', paths=['a.py'], reason='Untracked work')
        with self.assertRaises(BoardError):
            self.hub.retire_local('unknown', 'Owner request')
        self.assertEqual(len(self.call(self.b, 'board_status')['claims']), 1)
        self.hub.retire_local(self.a['agent_id'], 'Owner request')
        self.assertEqual(len(self.call(self.b, 'job_list')['jobs']), 1)

    def test_wrong_workspace_and_missing_database_refused(self):
        self.work()
        entry = Path(__file__).resolve().parents[1] / 'boardctl.py'
        for database, workspace in [(self.root / 'hub.sqlite', self.root / 'wrong'), (self.root / 'missing.sqlite', self.root)]:
            result = subprocess.run([sys.executable, str(entry), 'retire', self.a['agent_id'], '--database', str(database), '--workspace', str(workspace)], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'missing.sqlite').exists())
        self.assertEqual(len(self.call(self.b, 'board_status')['claims']), 3)

    def test_mcp_self_retirement_returns_result_then_rejects_old_identity(self):
        server = serve(self.hub, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        entry = Path(__file__).resolve().parents[1] / 'run_board.py'
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25'}},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'retire', 'arguments': {'reason': 'User requested self retirement'}}},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'board_status', 'arguments': {}}}]
        try:
            result = subprocess.run([sys.executable, str(entry), 'mcp', '--url', f'http://127.0.0.1:{server.server_port}', '--name', 'retire-probe'],
                                    input=''.join(json.dumps(r)+'\n' for r in requests), capture_output=True, text=True, check=True, timeout=15)
            replies = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertFalse(replies[1]['result']['isError'])
            retired = json.loads(replies[1]['result']['content'][0]['text'])
            self.assertTrue(retired['stop_session'])
            self.assertEqual(retired['retired_agent'], retired['your_agent_id'])
            self.assertTrue(replies[2]['result']['isError'])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
