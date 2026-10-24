import difflib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.hooks import Adapter


class PatchQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})
        self.file = self.root / 'common.txt'
        self.base = 'one\ntwo\nthree\nfour\n'
        self.file.write_bytes(self.base.encode())

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **params):
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def queue(self, who, action, **params):
        return self.call(who, 'patch_queue', action=action, **params)

    def join(self, who):
        return self.queue(who, 'join', path=str(self.file), reason='Common file work')['batch']

    def submit(self, who, batch, new):
        diff = ''.join(difflib.unified_diff(self.base.splitlines(True), new.splitlines(True), 'a/common.txt', 'b/common.txt'))
        return self.queue(who, 'submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch=diff)

    def messages(self, who):
        return self.call(who, 'inbox_read')['messages']

    def test_nonoverlap_waits_for_every_release_and_notifies_once(self):
        batch = self.join(self.a)
        self.assertEqual(self.join(self.b)['id'], batch['id'])
        first = self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        second = self.submit(self.b, batch, self.base.replace('four', 'FOUR'))
        self.assertTrue(first['accepted'] and second['accepted'])
        self.assertEqual(self.file.read_text(), self.base)
        self.queue(self.a, 'release', batch_id=batch['id'])
        self.assertEqual(self.messages(self.a), [])
        result = self.queue(self.b, 'release', batch_id=batch['id'])
        self.assertEqual(result['batch']['state'], 'APPLIED')
        self.assertEqual(self.file.read_text(), 'ONE\ntwo\nthree\nFOUR\n')
        for who in (self.a, self.b):
            self.assertEqual(len(self.messages(who)), 1)
            self.assertIn('targeted tests', self.messages(who)[0]['body'])
            self.assertEqual(self.messages(who)[0]['sender'], 'system')
        with self.assertRaises(BoardError):
            self.queue(self.a, 'record_commit', patch_ids=[second['patch_id']], commit_hash='a'*40)
        self.queue(self.a, 'record_commit', patch_ids=[first['patch_id']], commit_hash='a'*40)

    def test_conflict_rejection_preserves_candidate_and_own_revision(self):
        batch = self.join(self.a)
        self.join(self.b)
        own = self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        self.assertFalse(self.submit(self.b, batch, self.base.replace('one', 'OTHER'))['accepted'])
        self.submit(self.b, batch, self.base.replace('four', 'FOUR'))
        self.assertFalse(self.submit(self.a, batch, self.base.replace('four', 'OTHER'))['accepted'])
        data = self.queue(self.a, 'read', batch_id=batch['id'], content='candidate')
        self.assertEqual(data['content'], 'ONE\ntwo\nthree\nFOUR\n')
        self.assertEqual(data['batch']['patches'][0]['id'], own['patch_id'])

    def test_concurrent_conflicting_submissions_have_one_winner(self):
        batch = self.join(self.a)
        self.join(self.b)
        barrier, results = threading.Barrier(2), []
        def run(who, text):
            barrier.wait()
            results.append(self.submit(who, batch, self.base.replace('one', text))['accepted'])
        threads = [threading.Thread(target=run, args=(self.a, 'A')), threading.Thread(target=run, args=(self.b, 'B'))]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(sorted(results), [False, True])

    def test_external_write_blocks_without_overwrite_and_retains_candidate(self):
        batch = self.join(self.a)
        self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        self.file.write_text('external\n')
        result = self.queue(self.a, 'release', batch_id=batch['id'])
        self.assertEqual(result['batch']['state'], 'BLOCKED')
        self.assertEqual(self.file.read_text(), 'external\n')
        self.assertIn('ONE', self.queue(self.a, 'read', batch_id=batch['id'], content='candidate')['content'])
        messages = self.messages(self.a)
        self.assertEqual(len(messages), 2)  # Workspace incident plus final batch result.
        self.assertIn('coordination incident', messages[0]['body'])
        self.assertIn('finalized: BLOCKED', messages[1]['body'])

    def test_crash_after_file_replace_recovers_once(self):
        batch = self.join(self.a)
        self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        replace = os.replace
        def crash(src, dst):
            replace(src, dst)
            raise SystemExit('simulated power loss')
        with patch('board.patch_queue.os.replace', side_effect=crash):
            with self.assertRaises(SystemExit):
                self.queue(self.a, 'release', batch_id=batch['id'])
        self.hub = Hub(self.db, self.root)
        self.assertEqual(len(self.messages(self.a)), 1)
        self.assertEqual(self.file.read_text(), self.base.replace('one', 'ONE'))
        self.assertEqual(len(self.messages(self.a)), 1)

    def test_exclusive_claim_and_queue_are_mutually_exclusive(self):
        claim = self.call(self.a, 'claim', paths=['common.txt'], reason='exclusive')
        self.assertFalse(self.queue(self.b, 'join', path=str(self.file), reason='queue')['accepted'])
        self.call(self.a, 'release', claim_ids=claim['claim_ids'], held_reason='Waiting')
        self.join(self.b)
        self.assertFalse(self.call(self.a, 'claim', paths=['common.txt'], reason='exclusive')['accepted'])
        self.assertFalse(self.call(self.b, 'claim', paths=['common.txt'], reason='own queue')['accepted'])
        status = self.call(self.a, 'board_status', paths=['common.txt'], include_rules=False)
        self.assertEqual(len(status['patch_queues']), 1)
        self.assertIn(self.b['agent_id'], [a['id'] for a in status['agents']])
        self.assertEqual(status['matches'][0]['patch_queue_ids'], [status['patch_queues'][0]['id']])

    def test_retirement_closes_membership_without_discarding_patch(self):
        batch = self.join(self.a)
        self.join(self.b)
        self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        self.queue(self.b, 'release', batch_id=batch['id'])
        self.call(self.b, 'retire', target_agent_id=self.a['agent_id'], reason='Owner requested')
        self.assertEqual(self.file.read_text(), self.base.replace('one', 'ONE'))

    def test_inheritance_transfers_queue_and_patch_custody(self):
        batch = self.join(self.a)
        item = self.submit(self.a, batch, self.base.replace('one', 'ONE'))
        self.call(self.b, 'inherit', target_agent_id=self.a['agent_id'], inheritance_secret=self.a['inheritance_secret'], reason='Continuation')
        self.queue(self.b, 'release', batch_id=batch['id'])
        self.queue(self.b, 'record_commit', patch_ids=[item['patch_id']], commit_hash='b'*40)
        self.assertEqual(len(self.messages(self.b)), 1)

    def test_read_pages_and_malformed_diff_are_safe(self):
        batch = self.join(self.a)
        first = self.queue(self.a, 'read', batch_id=batch['id'], content='base', limit=4)
        self.assertEqual(first['content'], 'one\n')
        self.assertEqual(first['next_offset'], 4)
        for diff in ('nonsense', '--- a/other\n+++ b/other\n@@ -1 +1 @@\n-one\n+ONE\n',
                     '--- a/common.txt\n+++ b/common.txt\n@@ -1 +1 @@\n-wrong\n+ONE\n'):
            with self.assertRaises(BoardError):
                self.queue(self.a, 'submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch=diff)
        self.assertEqual(self.file.read_text(), self.base)

    def test_released_agent_cannot_submit_or_rejoin_same_batch(self):
        batch = self.join(self.a)
        self.join(self.b)
        self.queue(self.a, 'release', batch_id=batch['id'])
        with self.assertRaises(BoardError): self.submit(self.a, batch, 'changed\n')
        with self.assertRaises(BoardError): self.join(self.a)

    def test_crlf_and_no_final_newline_are_preserved(self):
        self.base = 'one\r\ntwo\r\n'
        self.file.write_bytes(self.base.encode())
        batch = self.join(self.a)
        self.submit(self.a, batch, 'ONE\r\ntwo\r\n')
        self.queue(self.a, 'release', batch_id=batch['id'])
        self.assertEqual(self.file.read_bytes(), b'ONE\r\ntwo\r\n')
        self.base = 'one'
        self.file.write_bytes(b'one')
        batch = self.join(self.a)
        diff = '--- a/common.txt\n+++ b/common.txt\n@@ -1 +1 @@\n-one\n\\ No newline at end of file\n+ONE\n\\ No newline at end of file\n'
        self.queue(self.a, 'submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch=diff)
        self.queue(self.a, 'release', batch_id=batch['id'])
        self.assertEqual(self.file.read_bytes(), b'ONE')

    def test_repeated_lines_keep_explicit_hunk_positions(self):
        self.base = 'same\nsame\nsame\n'
        self.file.write_bytes(self.base.encode())
        batch = self.join(self.a)
        self.join(self.b)
        for who, position, value in [(self.a, 1, 'A'), (self.b, 3, 'B')]:
            diff = f'--- a/common.txt\n+++ b/common.txt\n@@ -{position} +{position} @@\n-same\n+{value}\n'
            result = self.queue(who, 'submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch=diff)
            self.assertTrue(result['accepted'])
        self.queue(self.a, 'release', batch_id=batch['id'])
        self.queue(self.b, 'release', batch_id=batch['id'])
        self.assertEqual(self.file.read_text(), 'A\nsame\nB\n')

    def test_same_insertion_point_is_rejected(self):
        batch = self.join(self.a)
        self.join(self.b)
        self.assertTrue(self.submit(self.a, batch, 'new\n' + self.base)['accepted'])
        self.assertFalse(self.submit(self.b, batch, 'other\n' + self.base)['accepted'])

    def test_lf_diff_against_crlf_base_preserves_bytes_and_combines_patches(self):
        self.file.write_bytes(self.base.replace('\n', '\r\n').encode())
        batch = self.join(self.a)
        self.join(self.b)
        self.assertTrue(self.submit(self.a, batch, self.base.replace('one', 'ONE'))['accepted'])
        self.assertTrue(self.submit(self.b, batch, self.base.replace('four', 'FOUR'))['accepted'])
        self.queue(self.a, 'release', batch_id=batch['id'])
        self.queue(self.b, 'release', batch_id=batch['id'])
        self.assertEqual(self.file.read_bytes(), b'ONE\r\ntwo\r\nthree\r\nFOUR\r\n')

    def test_crlf_diff_against_lf_base_and_mixed_endings(self):
        from board.patch_queue import proposed
        diff = '--- a/common.txt\r\n+++ b/common.txt\r\n@@ -1,3 +1,3 @@\r\n one\r\n-two\r\n+TWO\r\n three\r\n'
        for original, expected in [('one\ntwo\nthree\n', 'one\nTWO\nthree\n'),
                                   ('one\r\ntwo\nthree\r\n', 'one\r\nTWO\nthree\r\n')]:
            self.assertEqual(proposed(original, diff, 'common.txt')[0], expected)

    def test_new_lines_follow_base_style_and_no_newline_marker_is_preserved(self):
        from board.patch_queue import proposed
        diff = '--- a/common.txt\n+++ b/common.txt\n@@ -1 +1,2 @@\n one\n+new\n'
        self.assertEqual(proposed('one\r\n', diff, 'common.txt')[0], 'one\r\nnew\r\n')
        diff = '--- a/common.txt\n+++ b/common.txt\n@@ -1,2 +1,2 @@\n one\n-two\n\\ No newline at end of file\n+TWO\n\\ No newline at end of file\n'
        self.assertEqual(proposed('one\r\ntwo', diff, 'common.txt')[0], 'one\r\nTWO')

    def test_windows_header_casing_and_separators_without_accepting_other_files(self):
        from board.patch_queue import proposed
        diff = '--- a/COMMON.TXT\n+++ b\\Common.Txt\n@@ -1 +1 @@\n-one\n+ONE\n'
        if os.name == 'nt':
            self.assertEqual(proposed('one\n', diff, 'common.txt')[0], 'ONE\n')
        else:
            with self.assertRaises(BoardError):
                proposed('one\n', diff, 'common.txt')
        with self.assertRaises(BoardError):
            proposed('one\n', diff.replace('COMMON.TXT', 'OTHER.TXT'), 'common.txt')

    def test_newline_tolerance_does_not_hide_missing_newline_or_content_mismatch(self):
        from board.patch_queue import proposed
        diff = '--- a/common.txt\n+++ b/common.txt\n@@ -1 +1 @@\n-one\n+ONE\n'
        for original in ('one', 'different\r\n'):
            with self.assertRaisesRegex(BoardError, 'context/count'):
                proposed(original, diff, 'common.txt')

    def test_age_survives_release_reclaim_and_new_work_resets(self):
        ids = self.call(self.a, 'claim', paths=['common.txt'], reason='edit')['claim_ids']
        with self.hub.connect() as db:
            db.execute('UPDATE custody_age SET since=?', (time.time()-7300,))
        self.call(self.a, 'release', claim_ids=ids, held_reason='unfinished')
        self.assertTrue(self.call(self.a, 'board_status', target_agent_id='self')['custody'][0]['age_warning'])
        ids = self.call(self.a, 'claim', paths=['common.txt'], reason='continue')['claim_ids']
        self.call(self.a, 'release', claim_ids=ids, held_reason='still unfinished')
        self.assertTrue(self.call(self.a, 'board_status', target_agent_id='self')['custody'][0]['age_warning'])
        self.call(self.a, 'custody_resolve', paths=['common.txt'], commit_hash='a'*40)
        ids = self.call(self.a, 'claim', paths=['common.txt'], reason='new')['claim_ids']
        self.call(self.a, 'release', claim_ids=ids, held_reason='new work')
        self.assertFalse(self.call(self.a, 'board_status', target_agent_id='self')['custody'][0]['age_warning'])
