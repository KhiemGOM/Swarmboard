import concurrent.futures
import tempfile
import threading
import unittest
from pathlib import Path

from board.core import BoardError, Hub


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **params):
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def post(self, **params):
        return self.call(self.a, 'job_post', title='Fix a bug', description='Authorized bug repair', acceptance='Reproduction passes', **params)['job']

    def claim(self, who, job):
        return self.call(who, 'job_claim', job_id=job['id'])['job']

    def finish(self, who, job, **params):
        return self.call(who, 'job_finish', job_id=job['id'], lease_token=job['lease_token'], summary='Reproduction fixed', **params)

    def evidence(self):
        return [{'command': 'python -m unittest tests.test_bug', 'result': 'passed', 'evidence': '1 regression test passed for abcdef0'}]

    def test_claim_is_atomic_and_does_not_acquire_file_locks(self):
        job = self.post()
        gate = threading.Barrier(2)
        def compete(who):
            gate.wait()
            return self.call(who, 'job_claim', job_id=job['id'])['accepted']
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(compete, [self.a, self.b])), [False, True])
        self.assertEqual(self.call(self.a, 'board_status')['claims'], [])

    def test_completion_requires_hash_and_passing_or_explained_checks(self):
        job = self.claim(self.a, self.post())
        for values in ({'checks': self.evidence()}, {'commit_hash': 'abcdef0', 'checks': []},
                       {'commit_hash': 'abcdef0', 'checks': [{'command': 'test', 'result': 'failed', 'evidence': 'oops'}]},
                       {'commit_hash': 'abcdef0', 'checks': [{'command': 'test', 'result': 'not_applicable', 'evidence': ''}]}):
            with self.assertRaises(BoardError):
                self.finish(self.a, job, **values)
        done = self.finish(self.a, job, commit_hash='abcdef0', checks=self.evidence())
        self.assertEqual(done['job']['state'], 'DONE')
        self.assertFalse(done['evidence_verified'])
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.assertEqual(self.call(self.b, 'job_list')['jobs'][0]['checks'], self.evidence())
        self.assertFalse(self.call(self.b, 'job_claim', job_id=job['id'])['accepted'])

    def test_expiry_preserves_progress_and_fences_old_worker(self):
        job = self.claim(self.a, self.post())
        self.call(self.a, 'job_update', job_id=job['id'], lease_token=job['lease_token'], progress='Regression added; implementation missing')
        with self.hub.connect() as db:
            db.execute('UPDATE jobs SET expires=0')
        reopened = self.call(self.b, 'job_list', state='OPEN')['jobs'][0]
        self.assertEqual(reopened['progress'], 'Regression added; implementation missing')
        self.assertEqual(reopened['previous_owner'], self.a['agent_id'])
        with self.assertRaises(BoardError):
            self.finish(self.a, job, commit_hash='abcdef0', checks=self.evidence())
        newer = self.claim(self.a, reopened)
        self.assertNotEqual(newer['lease_token'], job['lease_token'])
        with self.assertRaises(BoardError):
            self.finish(self.a, job, commit_hash='abcdef0', checks=self.evidence())

    def test_disconnect_reopens_jobs_but_preserves_locks_and_custody(self):
        job = self.claim(self.a, self.post())
        self.call(self.a, 'claim', paths=['a.py'], reason='Fix bug')
        self.call(self.a, 'disconnect', reason='Bridge closed')
        status = self.call(self.b, 'board_status')
        self.assertEqual(status['claims'][0]['agent'], self.a['agent_id'])
        self.assertEqual(status['custody'][0]['owner'], self.a['agent_id'])
        old = next(r for r in status['agents'] if r['id'] == self.a['agent_id'])
        self.assertEqual(old['connection'], 'disconnected')
        self.assertEqual(old['state'], 'active')
        self.assertEqual(self.call(self.b, 'job_list')['jobs'][0]['state'], 'OPEN')

    def test_takeover_transfers_job_and_fences_predecessor(self):
        job = self.claim(self.a, self.post())
        result = self.call(self.b, 'takeover', target_agent_id=self.a['agent_id'], reason='Continuing unavailable A')
        new = result['jobs'][0]
        self.assertEqual(new['owner'], self.b['agent_id'])
        self.assertNotEqual(new['lease_token'], job['lease_token'])
        with self.assertRaises(BoardError):
            self.finish(self.a, job, commit_hash='abcdef0', checks=self.evidence())
        self.finish(self.b, new, commit_hash='abcdef0', checks=self.evidence())

    def test_yield_followups_and_noncode_completion(self):
        parent = self.claim(self.a, self.post(kind='noncode'))
        self.call(self.a, 'job_yield', job_id=parent['id'], lease_token=parent['lease_token'], reason='Research links saved in docs')
        resumed = self.claim(self.b, parent)
        done = self.finish(self.b, resumed, checks=self.evidence())
        self.assertIsNone(done['job']['commit_hash'])
        child = self.post(parent_job_id=parent['id'])
        self.assertEqual(child['parent_job_id'], parent['id'])

    def test_other_agents_cannot_renew_finish_or_read_lease_token(self):
        job = self.claim(self.a, self.post())
        self.assertNotIn('lease_token', self.call(self.b, 'job_list')['jobs'][0])
        with self.assertRaises(BoardError):
            self.finish(self.b, job, commit_hash='abcdef0', checks=self.evidence())
        self.assertIn('job_reminder', self.call(self.a, 'noticeboard_read'))
        self.assertNotIn('job_reminder', self.call(self.b, 'board_status'))

    def test_rejects_invalid_post_and_ttl_without_mutation(self):
        with self.assertRaises(BoardError):
            self.post(parent_job_id=999)
        job = self.post()
        for value in (True, 0, 7201, 1.5):
            with self.assertRaises(BoardError):
                self.call(self.a, 'job_claim', job_id=job['id'], ttl_seconds=value)
        self.assertEqual(self.call(self.a, 'job_list')['jobs'][0]['state'], 'OPEN')

    def test_untracked_custody_becomes_recovery_job_once(self):
        self.call(self.a, 'claim', paths=['src'], reason='Prior authorized feature')
        self.call(self.a, 'disconnect', reason='Harness exited')
        self.call(self.a, 'disconnect', reason='Bridge exited too')
        jobs = self.call(self.b, 'job_list')['jobs']
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['state'], 'OPEN')
        self.assertIn('Prior authorized feature', jobs[0]['description'])
        self.assertIn('scope is unclear', jobs[0]['acceptance'])
