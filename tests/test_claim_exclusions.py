import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub


class ClaimExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        for name in ('src/a.cs', 'src/b.cs', 'src/ui/hud.cs', 'src/ui/menu.cs', 'docs/readme.md'):
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text('x\n')
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a, self.b = [self.hub.call('register', {'name': name}) for name in 'AB']

    def protect(self, *paths):
        (self.root / '.swarmboard.json').write_text(json.dumps({'queue_only': list(paths)}))

    def claim(self, who, paths, **p):
        return self.hub.call('claim', {**who, 'paths': paths, 'reason': 'Work', **p})

    def held(self, who):
        status = self.hub.call('board_status', {**who, 'target_agent_id': 'self'})
        return {row['path']: sorted(row['exclusions']) for row in status['claims'] if row['kind'] == 'file'}

    # Basics: existing behaviour stays put
    def test_plain_folder_claim_still_has_no_exclusions(self):
        self.assertTrue(self.claim(self.a, ['src'])['accepted'])
        self.assertEqual(self.held(self.a), {'src': []})

    def test_protected_file_still_blocks_a_plain_folder_claim(self):
        self.protect('src/ui/hud.cs')
        result = self.claim(self.a, ['src'])
        self.assertFalse(result['accepted'])
        self.assertEqual(result['queue_only_files'], ['src/ui/hud.cs'])
        self.assertIn('except_protected', result['reason'])

    # except_protected
    def test_except_protected_locks_the_folder_minus_queue_only_files(self):
        self.protect('src/ui/hud.cs', 'src/b.cs', 'docs/readme.md')
        result = self.claim(self.a, ['src'], except_protected=True)
        self.assertTrue(result['accepted'])
        self.assertEqual(sorted(result['excluded']['src']), ['src/b.cs', 'src/ui/hud.cs'])
        self.assertEqual(self.held(self.a), {'src': ['src/b.cs', 'src/ui/hud.cs']})
        # The carved-out file stays available to the queue; the rest is locked.
        self.assertTrue(self.hub.call('patch_queue', {**self.b, 'action': 'join', 'path': 'src/b.cs', 'reason': 'Shared'})['accepted'])
        self.assertFalse(self.claim(self.b, ['src/a.cs'])['accepted'])
        self.assertFalse(self.claim(self.b, ['src/ui/menu.cs'])['accepted'])

    def test_except_protected_also_skips_files_with_a_live_queue(self):
        self.hub.call('patch_queue', {**self.b, 'action': 'join', 'path': 'src/a.cs', 'reason': 'Shared'})
        self.assertIn('patch_queue_conflicts', self.claim(self.a, ['src']))
        result = self.claim(self.a, ['src'], except_protected=True)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['excluded'], {'src': ['src/a.cs']})

    def test_except_protected_without_protected_files_is_a_plain_claim(self):
        result = self.claim(self.a, ['src'], except_protected=True)
        self.assertTrue(result['accepted'])
        self.assertEqual(self.held(self.a), {'src': []})

    def test_except_protected_on_the_protected_file_itself_is_still_refused(self):
        self.protect('src/a.cs')
        self.assertFalse(self.claim(self.a, ['src/a.cs'], except_protected=True)['accepted'])

    def test_whole_workspace_except_protected(self):
        self.protect('src/a.cs', 'docs/readme.md')
        self.assertTrue(self.claim(self.a, ['.'], except_protected=True)['accepted'])
        self.assertEqual(self.held(self.a), {'.': ['docs/readme.md', 'src/a.cs']})

    # Explicit exclude
    def test_exclude_carves_named_files_and_folders(self):
        result = self.claim(self.a, ['src'], exclude=['src/ui', 'src/b.cs'])
        self.assertTrue(result['accepted'])
        self.assertEqual(self.held(self.a), {'src': ['src/b.cs', 'src/ui']})
        self.assertTrue(self.claim(self.b, ['src/ui/menu.cs'])['accepted'])
        self.assertTrue(self.claim(self.b, ['src/b.cs'])['accepted'])
        self.assertFalse(self.claim(self.b, ['src/a.cs'])['accepted'])

    def test_exclude_lets_a_folder_claim_skip_another_agents_file(self):
        self.assertTrue(self.claim(self.b, ['src/a.cs'])['accepted'])
        self.assertIn('conflicts', self.claim(self.a, ['src']))
        self.assertTrue(self.claim(self.a, ['src'], exclude=['src/a.cs'])['accepted'])

    def test_exclude_routes_to_the_containing_claim_only(self):
        self.assertTrue(self.claim(self.a, ['src', 'docs'], exclude=['src/ui'])['accepted'])
        self.assertEqual(self.held(self.a), {'src': ['src/ui'], 'docs': []})

    def test_combined_exclude_and_except_protected(self):
        self.protect('src/a.cs')
        self.assertTrue(self.claim(self.a, ['src'], exclude=['src/ui'], except_protected=True)['accepted'])
        self.assertEqual(self.held(self.a), {'src': ['src/a.cs', 'src/ui']})

    def test_renewal_keeps_earlier_exclusions(self):
        self.claim(self.a, ['src'], exclude=['src/ui'])
        self.assertTrue(self.claim(self.a, ['src'])['accepted'])
        self.assertEqual(self.held(self.a), {'src': ['src/ui']})
        self.claim(self.a, ['src'], exclude=['src/b.cs'])
        self.assertEqual(self.held(self.a), {'src': ['src/b.cs', 'src/ui']})

    def test_partial_claims_pass_exclusions_to_their_own_path(self):
        self.protect('src/a.cs')
        result = self.claim(self.a, ['src', 'docs'], partial=True, exclude=['docs/readme.md'], except_protected=True)
        self.assertTrue(result['accepted'])
        self.assertEqual(self.held(self.a), {'src': ['src/a.cs'], 'docs': ['docs/readme.md']})

    def test_excluded_custody_is_not_taken_over(self):
        self.assertTrue(self.claim(self.b, ['src/a.cs'])['accepted'])
        self.hub.call('release', {**self.b, 'claim_ids': self.held_ids(self.b), 'held_reason': 'Unfinished'})
        self.claim(self.a, ['src'], exclude=['src/a.cs'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT owner FROM custody WHERE path='src/a.cs'").fetchone()[0], self.b['agent_id'])

    def held_ids(self, who):
        status = self.hub.call('board_status', {**who, 'target_agent_id': 'self'})
        return [row['id'] for row in status['claims']]

    # Bad input
    def test_bad_exclusions_are_refused_not_ignored(self):
        cases = [({'exclude': 'src/a.cs'}, ['src']), ({'exclude': [3]}, ['src']), ({'exclude': ['docs/readme.md']}, ['src']),
                 ({'exclude': ['src']}, ['src']), ({'exclude': ['src/a.cs']}, ['src/a.cs']), ({'exclude': ['../x']}, ['src']),
                 ({'exclude': ['src/*.cs']}, ['src']), ({'except_protected': 'yes'}, ['src']),
                 ({'exclude': [f'src/f{i}' for i in range(65)]}, ['src'])]
        for extra, paths in cases:
            with self.subTest(extra=str(extra)[:60]), self.assertRaises(BoardError):
                self.claim(self.a, paths, **extra)

    def test_resource_claims_reject_exclusions(self):
        with self.assertRaises(BoardError):
            self.hub.call('mutex_claim', {**self.a, 'resources': ['git:index'], 'reason': 'x', 'exclude': ['src/a.cs']})


if __name__ == '__main__':
    unittest.main()
