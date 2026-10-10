import json
import hashlib
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

    def coord(self, who, action, path='common.txt', reason='Shared work needs coordination', **p):
        return self.call(who, 'board_coordination', action=action, path=path, reason=reason, **p)

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
        hashes = {'before_hash': hashlib.sha256(self.file.read_bytes()).hexdigest()}
        self.file.write_bytes(b'changed\r\n')
        hashes['after_hash'] = hashlib.sha256(self.file.read_bytes()).hexdigest()
        result = self.coord(self.b, 'report', reason='Observed unrelated edits while A held the file', **hashes)
        self.assertEqual(result['notified'], 2)
        self.assertEqual(result['before_verification'], 'reporter_provided')
        self.assertEqual(result['after_verification'], 'current_file_bytes')
        self.assertEqual(self.messages(self.b), [])
        self.assertEqual(len(self.messages(self.a)), 1)
        self.assertEqual(len(self.messages(self.c)), 1)
        self.assertIn(hashes['before_hash'], self.messages(self.a)[0]['body'])
        self.assertIn(hashes['after_hash'], self.messages(self.a)[0]['body'])
        duplicate = self.coord(self.c, 'report', reason='Different words, same change', **hashes)
        self.assertTrue(duplicate['duplicate'])
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_vague_invalid_identical_and_stale_hash_reports_do_not_broadcast(self):
        actual = hashlib.sha256(self.file.read_bytes()).hexdigest()
        for hashes in ({}, {'before_hash': 'bad', 'after_hash': actual},
                       {'before_hash': actual, 'after_hash': actual},
                       {'before_hash': actual, 'after_hash': 'a' * 64}):
            with self.assertRaises(BoardError):
                self.coord(self.b, 'report', reason='file changed', **hashes)
        self.assertEqual(self.messages(self.a), [])
        self.assertEqual(self.messages(self.c), [])

    def test_queue_report_checks_recorded_before_hash_and_normalizes_hex_case(self):
        batch = self.call(self.a, 'patch_queue', action='join', path='common.txt', reason='Queue')['batch']
        with self.assertRaisesRegex(BoardError, 'recorded queue base'):
            self.coord(self.b, 'report', before_hash='a' * 64, after_hash=batch['base_hash'])
        self.file.write_bytes(b'outside\n')
        result = self.coord(self.b, 'report', before_hash=batch['base_hash'].upper(),
                            after_hash=hashlib.sha256(self.file.read_bytes()).hexdigest().upper())
        self.assertEqual(result['before_hash'], batch['base_hash'])
        self.assertEqual(result['before_verification'], 'recorded_queue_base')

    def test_report_requires_existing_file_not_folder(self):
        for path in ('.', 'missing.txt', '../outside'):
            with self.assertRaises(BoardError):
                self.coord(self.b, 'report', path=path, before_hash='a' * 64, after_hash='b' * 64)
        self.assertEqual(self.messages(self.a), [])

    def test_file_custody_history_survives_ack_and_restart_with_bounded_pages(self):
        self.claim(self.a)
        before = hashlib.sha256(self.file.read_bytes()).hexdigest()
        for content in (b'first\n', b'second\r\n'):
            self.file.write_bytes(content)
            after = hashlib.sha256(content).hexdigest()
            self.coord(self.b, 'report', before_hash=before, after_hash=after)
            before = after
        ids = [m['id'] for m in self.messages(self.a)]
        self.call(self.a, 'inbox_ack', message_ids=ids)
        self.assertEqual(self.messages(self.a), [])
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        first = self.call(self.c, 'board_coordination', action='history', path='common.txt', limit=1)
        second = self.call(self.c, 'board_coordination', action='history', path='common.txt', limit=1, after_id=first['next_after_id'])
        self.assertEqual(len(first['incidents']), 1)
        self.assertEqual(len(second['incidents']), 1)
        self.assertNotEqual(first['incidents'][0]['id'], second['incidents'][0]['id'])
        self.assertEqual(second['incidents'][0]['evidence']['after_hash'], after)
        custody = self.call(self.c, 'board_status', paths=['common.txt'], include_rules=False)['custody'][0]
        self.assertEqual(custody['incident_count'], 2)
        self.assertEqual(custody['custody_history']['action'], 'history')
        with self.assertRaises(BoardError):
            self.call(self.c, 'board_coordination', action='history', path='common.txt', limit=11)

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
