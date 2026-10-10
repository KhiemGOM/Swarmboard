import concurrent.futures
import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.inheritance import OWNER_OVERRIDE
from board.version import VERSION


class InheritanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.old, self.new, self.peer = [self.hub.call('register', {'name': name}) for name in ('old', 'new', 'peer')]
        self.claim = self.call(self.old, 'claim', paths=['a.py'], resources=['git:index'], reason='Work in progress')
        self.job = self.call(self.old, 'job_post', title='Fix A', description='Fix A bug', acceptance='A passes')['job']
        self.job = self.call(self.old, 'job_claim', job_id=self.job['id'])['job']
        self.call(self.old, 'job_update', job_id=self.job['id'], lease_token=self.job['lease_token'], progress='Regression added')
        self.message = self.call(self.peer, 'inbox_send', recipient_agent_id=self.old['agent_id'], body='Waiting for A')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **p):
        return self.hub.call(method, {**p, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def inherit(self, **p):
        return self.call(self.new, 'inherit', target_agent_id=self.old['agent_id'], reason='Continue predecessor session', **p)

    def test_proof_transfers_work_fences_old_session_and_routes_late_replies(self):
        before = self.call(self.old, 'board_status')['claims']
        result = self.inherit(inheritance_secret=self.old['inheritance_secret'])
        self.assertEqual(result['claim_ids'], self.claim['claim_ids'])
        self.assertEqual(result['custody_paths'], ['a.py'])
        inherited = result['jobs'][0]
        self.assertEqual(inherited['progress'], 'Regression added')
        self.assertNotEqual(inherited['lease_token'], self.job['lease_token'])
        status = self.call(self.new, 'board_status', target_agent_id='self')
        self.assertEqual([row['expires'] for row in status['claims']], [row['expires'] for row in before])
        self.assertEqual(status['unread_inbox'], 1)
        self.assertEqual(status['agents'][0]['inherited_from'], self.old['agent_id'])
        with self.assertRaisesRegex(BoardError, 'retired'):
            self.call(self.old, 'board_status')
        self.call(self.peer, 'inbox_send', recipient_agent_id=self.old['agent_id'], body='Late reply')
        self.assertEqual(self.call(self.new, 'inbox_read')['unread_inbox'], 2)
        with self.assertRaisesRegex(BoardError, 'already inherited'):
            self.inherit(inheritance_secret=self.old['inheritance_secret'])

    def test_wrong_proof_rolls_back_and_rotation_invalidates_previous_key(self):
        before = self.call(self.peer, 'board_status')['claims']
        for p in ({}, {'inheritance_secret': 'wrong'}, {'owner_override': 'inherit'},
                  {'inheritance_secret': self.old['inheritance_secret'], 'owner_override': OWNER_OVERRIDE}):
            with self.subTest(p=p), self.assertRaises(BoardError):
                self.inherit(**p)
        self.assertEqual(self.call(self.peer, 'board_status')['claims'], before)
        key = self.call(self.old, 'inheritance_key')['inheritance_secret']
        self.assertEqual(len(key), 16)
        with self.assertRaisesRegex(BoardError, 'Invalid inheritance secret'):
            self.inherit(inheritance_secret=self.old['inheritance_secret'])
        self.assertEqual(self.inherit(inheritance_secret=key)['proof_mode'], 'secret')
        with self.hub.connect() as db:
            serialized = json.dumps([dict(row) for row in db.execute('SELECT * FROM agents')])
            events = json.dumps([dict(row) for row in db.execute('SELECT * FROM events')])
        self.assertNotIn(key, serialized)
        self.assertNotIn(key, events)
        self.assertNotIn('inheritance_hash', json.dumps(self.call(self.new, 'board_status')))

    def test_owner_override_is_audited_and_retired_recovery_jobs_are_inherited(self):
        self.call(self.peer, 'retire', target_agent_id=self.old['agent_id'], reason='Owner retired old session')
        result = self.inherit(owner_override=OWNER_OVERRIDE)
        self.assertEqual(result['claim_ids'], [])
        self.assertEqual(result['custody_paths'], ['a.py'])
        self.assertEqual(result['jobs'][0]['state'], 'CLAIMED')
        self.assertEqual(result['jobs'][0]['owner'], self.new['agent_id'])
        with self.hub.connect() as db:
            audit = json.loads(db.execute("SELECT payload FROM events WHERE kind='agent_inherited'").fetchone()[0])
        self.assertTrue(audit['owner_override'])

    def test_disconnect_recovery_preserves_work_already_adopted_by_peer(self):
        self.call(self.old, 'disconnect', reason='Previous bridge closed')
        self.call(self.peer, 'job_claim', job_id=self.job['id'])
        result = self.inherit(inheritance_secret=self.old['inheritance_secret'])
        self.assertEqual(result['jobs'], [])
        jobs = self.call(self.peer, 'job_list')['jobs']
        self.assertEqual(jobs[0]['owner'], self.peer['agent_id'])

    def test_expired_claims_are_not_revived(self):
        with self.hub.connect() as db:
            db.execute('UPDATE claims SET expires=0')
        result = self.inherit(inheritance_secret=self.old['inheritance_secret'])
        self.assertEqual(result['claim_ids'], [])
        self.assertEqual(self.call(self.new, 'board_status')['claims'], [])
        self.assertEqual(self.call(self.new, 'board_status')['custody'][0]['state'], 'HELD')

    def test_concurrent_successors_have_one_winner(self):
        def compete(who):
            try:
                self.call(who, 'inherit', target_agent_id=self.old['agent_id'], inheritance_secret=self.old['inheritance_secret'], reason='Successor')
                return True
            except BoardError:
                return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(compete, [self.new, self.peer])), [False, True])

    def test_info_has_version_uptime_features_and_no_secrets(self):
        info = self.call(self.new, 'board_info')
        self.assertEqual(info['version'], VERSION)
        self.assertGreaterEqual(info['uptime_seconds'], 0)
        self.assertIn('session_inheritance', info['features'])
        self.assertNotIn(self.new['inheritance_secret'], json.dumps(info))

