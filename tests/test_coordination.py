import json
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.hooks import Adapter


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

    def test_folder_carveout_keeps_other_and_future_files_protected(self):
        folder = self.claim(self.a, '.')
        with self.hub.connect() as db:
            db.execute("UPDATE custody_age SET since=? WHERE path='.'", (time.time() - 7300,))
        self.coord(self.b, 'request_relax')
        result = self.coord(self.a, 'relax')
        self.assertEqual(result['folder_claims_retained'], folder['claim_ids'])
        self.assertEqual(len(self.messages(self.b)), 1)
        self.assertFalse(self.claim(self.b, 'future.txt')['accepted'])
        self.assertTrue(self.claim(self.a, '.')['accepted'])  # Renewal preserves exclusion.
        self.assertFalse(self.claim(self.a)['accepted'])  # Queue prevents direct claim.
        self.call(self.a, 'patch_queue', action='release', batch_id=result['batch']['id'])
        status = self.call(self.a, 'board_status', paths=['common.txt'], include_rules=False)
        self.assertEqual(status['matches'][0]['claim_ids'], [])
        self.assertTrue(next(r for r in status['custody'] if r['path'] == 'common.txt')['age_warning'])
        self.assertTrue(self.claim(self.b)['accepted'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT owner FROM custody WHERE path='.'").fetchone()[0], self.a['agent_id'])
            self.assertEqual(json.loads(db.execute('SELECT exclusions FROM claims WHERE id=?', (folder['claim_ids'][0],)).fetchone()[0]), ['common.txt'])

    def test_folder_carveout_release_does_not_clear_separate_file_custody(self):
        folder = self.claim(self.a, '.')
        self.coord(self.a, 'relax')
        self.call(self.a, 'release', claim_ids=folder['claim_ids'], commit_hash='a' * 40)
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM custody WHERE path='.'").fetchone()[0], 'COMMITTED')
            self.assertEqual(db.execute("SELECT state FROM custody WHERE path='common.txt'").fetchone()[0], 'HELD')

    def test_folder_requests_are_per_file_and_conversion_rolls_back_on_failure(self):
        folder = self.claim(self.a, '.')
        (self.root / 'other.txt').write_bytes(b'other\n')
        self.coord(self.b, 'request_relax')
        self.coord(self.b, 'request_relax', path='other.txt')
        self.assertEqual(len(self.messages(self.a)), 2)
        with patch('board.patch_queue.dispatch', side_effect=BoardError('conversion failed')):
            with self.assertRaisesRegex(BoardError, 'conversion failed'):
                self.coord(self.a, 'relax')
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT exclusions FROM claims WHERE id=?', (folder['claim_ids'][0],)).fetchone()[0], '[]')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM file_relax_requests').fetchone()[0], 2)
        self.coord(self.a, 'relax')
        self.assertEqual(len(self.messages(self.b)), 1)
        self.coord(self.a, 'relax', path='other.txt')
        self.assertEqual(len(self.messages(self.b)), 2)

    def test_nested_own_claims_are_all_carved_and_folder_target_is_refused(self):
        self.claim(self.a, '.')
        self.claim(self.a)
        with self.assertRaises(BoardError):
            self.coord(self.a, 'relax', path='.')
        result = self.coord(self.a, 'relax')
        self.assertEqual(len(result['claim_ids_affected']), 2)
        self.assertTrue(self.call(self.b, 'patch_queue', action='join', path='common.txt', reason='Shared')['accepted'])

    def test_carveout_survives_inheritance_and_waits_see_file_free_after_batch(self):
        folder = self.claim(self.a, '.')
        result = self.coord(self.a, 'relax')
        self.call(self.c, 'board_wait', action='subscribe', kind='mutex_free', path='common.txt')
        self.call(self.b, 'inherit', target_agent_id=self.a['agent_id'], inheritance_secret=self.a['inheritance_secret'], reason='Authorized successor')
        self.call(self.b, 'patch_queue', action='release', batch_id=result['batch']['id'])
        self.assertIn('is ready', self.messages(self.c)[0]['body'])
        status = self.call(self.b, 'board_status', target_agent_id='self', include_rules=False)
        self.assertEqual(status['claims'][0]['id'], folder['claim_ids'][0])
        self.assertEqual(status['claims'][0]['exclusions'], ['common.txt'])
        self.assertFalse(self.claim(self.c, 'future.txt')['accepted'])

    def test_hook_refuses_carved_file_after_finalization_but_allows_folder_remainder(self):
        self.claim(self.a, '.')
        result = self.coord(self.a, 'relax')
        self.call(self.a, 'patch_queue', action='release', batch_id=result['batch']['id'])
        adapter = Adapter(self.root, self.root / 'hub.sqlite', 'claude', caller=lambda url, method, params: self.hub.call(method, params))
        payload = {'session_id': 'native', 'cwd': str(self.root)}
        adapter.handle({**payload, 'hook_event_name': 'PostToolUse', 'tool_name': 'mcp__board__board_status',
                        'tool_response': {'your_agent_id': self.a['agent_id']}})
        for file, denied in ((self.file, True), (self.root / 'future.txt', False)):
            response = adapter.handle({**payload, 'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'tool_input': {'file_path': str(file)}})
            self.assertEqual(response.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny', denied)

    def test_legacy_relax_requests_migrate_to_per_file_keys(self):
        claim = self.claim(self.a)
        with self.hub.connect() as db:
            db.execute('CREATE TABLE relax_requests (claim_id INTEGER, requester TEXT, reason TEXT, created REAL, PRIMARY KEY(claim_id,requester))')
            db.execute('INSERT INTO relax_requests VALUES (?,?,?,?)', (claim['claim_ids'][0], self.b['agent_id'], 'Legacy request', 1))
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.assertTrue(self.coord(self.b, 'request_relax')['duplicate'])

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
