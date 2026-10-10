import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a, self.b, self.c = [self.hub.call('register', {'name': name}) for name in 'ABC']
        self.file = self.root / 'common.txt'
        self.file.write_bytes(b'original\n')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **p):
        return self.hub.call(method, {**who, **p})

    def coord(self, who, action, path='common.txt', reason='Shared work needs coordination'):
        return self.call(who, 'board_coordination', action=action, path=path, reason=reason)

    def claim(self, who, path='common.txt'):
        return self.call(who, 'claim', paths=[path], reason='Direct work')

    def messages(self, who):
        return self.call(who, 'inbox_read')['messages']

    def test_request_and_atomic_relax_retains_work_and_invites_requester(self):
        claim = self.claim(self.a)
        self.assertFalse(self.claim(self.b)['accepted'])
        self.assertTrue(self.coord(self.b, 'request_relax')['requested'])
        self.assertTrue(self.coord(self.b, 'request_relax')['duplicate'])
        self.assertEqual(len(self.messages(self.a)), 1)
        self.file.write_bytes(b'owner already edited\n')
        with self.assertRaisesRegex(BoardError, 'Only the claim holder'):
            self.coord(self.b, 'relax')
        result = self.coord(self.a, 'relax')
        self.assertEqual(result['relaxed_claim_id'], claim['claim_ids'][0])
        batch = result['batch']
        base = self.call(self.b, 'patch_queue', action='read', batch_id=batch['id'], content='base')['content']
        self.assertEqual(base, 'owner already edited\n')
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM claims WHERE id=?', (claim['claim_ids'][0],)).fetchone()[0], 0)
            custody = db.execute('SELECT owner,state FROM custody').fetchone()
            self.assertEqual(tuple(custody), (self.a['agent_id'], 'HELD'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM patch_members').fetchone()[0], 1)
        self.assertEqual(len(self.messages(self.b)), 1)
        joined = self.call(self.b, 'patch_queue', action='join', path='common.txt', reason='Joining')
        self.assertEqual(joined['batch']['id'], batch['id'])
        self.assertFalse(self.claim(self.c)['accepted'])

    def test_conversion_failure_rolls_back_lock_and_custody(self):
        self.claim(self.a)
        self.file.write_bytes(b'\x00binary')
        with self.assertRaises(BoardError):
            self.coord(self.a, 'relax')
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT state FROM custody').fetchone()[0], 'ACTIVE')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM patch_batches').fetchone()[0], 0)

    def test_folder_claim_cannot_be_silently_relaxed(self):
        self.claim(self.a, '.')
        with self.assertRaisesRegex(BoardError, 'folder holders'):
            self.coord(self.a, 'relax')
        self.assertFalse(self.claim(self.b)['accepted'])

    def test_explicit_hot_files_block_files_and_covering_folders_but_not_resources(self):
        (self.root / '.swarmboard.json').write_text(json.dumps({'queue_only': ['common.txt']}))
        for path in ('common.txt', '.'):
            self.assertEqual(self.claim(self.a, path)['queue_only_files'], ['common.txt'])
        self.assertTrue(self.claim(self.a, 'other.txt')['accepted'])
        self.assertTrue(self.call(self.a, 'mutex_claim', resources=['git:index'], reason='Commit')['accepted'])
        self.assertEqual(self.call(self.a, 'board_info')['queue_only_files'], ['common.txt'])
        self.assertTrue(self.call(self.b, 'patch_queue', action='join', path='common.txt', reason='Hot file')['accepted'])

    def test_policy_grandfathers_existing_holder_but_not_new_agents(self):
        claim = self.claim(self.a)
        (self.root / '.swarmboard.json').write_text('{"queue_only":["common.txt"]}')
        self.assertEqual(self.claim(self.a)['claim_ids'], claim['claim_ids'])
        self.assertFalse(self.claim(self.b)['accepted'])
        self.assertTrue(self.coord(self.a, 'relax')['accepted'])

    def test_invalid_policy_fails_closed(self):
        for text in ('{bad', '{"queue_only":["../escape"]}', '{"queue_only":["."]}', '[]'):
            (self.root / '.swarmboard.json').write_text(text)
            with self.assertRaises(BoardError):
                self.claim(self.a)

    def test_report_broadcast_excludes_reporter_and_deduplicates(self):
        result = self.coord(self.b, 'report', reason='Observed unrelated edits while A held the file')
        self.assertEqual(result['notified'], 2)
        self.assertEqual(self.messages(self.b), [])
        self.assertEqual(len(self.messages(self.a)), 1)
        self.assertEqual(len(self.messages(self.c)), 1)
        duplicate = self.coord(self.c, 'report', reason='Observed unrelated edits while A held the file')
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_automatic_incident_survives_rejected_submit_and_repeated_checks(self):
        batch = self.call(self.a, 'patch_queue', action='join', path='common.txt', reason='Queue')['batch']
        self.file.write_bytes(b'outside edit\n')
        with self.assertRaisesRegex(BoardError, 'outside the queue'):
            self.call(self.a, 'patch_queue', action='submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch='bad')
        for who in (self.a, self.b, self.c):
            messages = self.messages(who)
            self.assertEqual(len(messages), 1)
            self.assertIn('not proof against any agent', messages[0]['body'])
        self.call(self.a, 'patch_queue', action='release', batch_id=batch['id'])
        self.assertEqual(self.file.read_bytes(), b'outside edit\n')
        self.assertEqual(len(self.messages(self.a)), 2)  # Incident plus normal final result.
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT state FROM patch_batches').fetchone()[0], 'BLOCKED')

    def test_direct_holder_legitimate_edits_do_not_generate_automatic_incidents(self):
        self.claim(self.a)
        self.file.write_bytes(b'legitimate owner edit\n')
        self.assertEqual(self.messages(self.b), [])


if __name__ == '__main__':
    unittest.main()
