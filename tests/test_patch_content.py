import difflib
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub


class WholeFileSubmitTests(unittest.TestCase):
    BASE = 'one\ntwo\nthree\nfour\nfive\nsix\nseven\neight\nnine\nten\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a, self.b = [self.hub.call('register', {'name': name}) for name in 'AB']

    def queue(self, base=BASE, name='Shared.txt'):
        self.path = self.root / name
        self.path.write_bytes(base.encode('utf-8'))
        self.batch = None
        for who in (self.a, self.b):
            self.batch = self.call(who, 'join', path=str(self.path), reason='shared')['batch']
        return self.batch

    def call(self, who, action, **p):
        return self.hub.call('patch_queue', {**p, 'action': action, **who})

    def submit(self, who, **p):
        return self.call(who, 'submit', batch_id=self.batch['id'], base_hash=self.batch['base_hash'], **p)

    def finish(self):
        for who in (self.a, self.b):
            self.call(who, 'release', batch_id=self.batch['id'])
        return self.path.read_bytes().decode('utf-8')

    def scratch_file(self, text, name='out.txt', encoding='utf-8'):
        path = Path(self.scratch.name) / name
        path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
        return str(path)

    # Basics
    def test_inline_content_publishes_that_file(self):
        self.queue()
        new = self.BASE.replace('three', 'THREE')
        self.assertTrue(self.submit(self.a, new_content=new)['accepted'])
        self.assertEqual(self.finish(), new)

    def test_content_path_from_a_script_publishes_that_file(self):
        self.queue()
        new = self.BASE.replace('two\n', 'two\ntwo and a half\n')
        self.assertTrue(self.submit(self.a, new_content_path=self.scratch_file(new))['accepted'])
        self.assertEqual(self.finish(), new)

    def test_content_and_diff_from_different_agents_merge(self):
        self.queue()
        self.assertTrue(self.submit(self.a, new_content=self.BASE.replace('two', 'TWO'))['accepted'])
        diff = ''.join(difflib.unified_diff(self.BASE.splitlines(True), self.BASE.replace('nine', 'NINE').splitlines(True), 'a/Shared.txt', 'b/Shared.txt'))
        self.assertTrue(self.submit(self.b, patch=diff)['accepted'])
        self.assertEqual(self.finish(), self.BASE.replace('two', 'TWO').replace('nine', 'NINE'))

    def test_overlapping_whole_files_conflict(self):
        self.queue()
        self.assertTrue(self.submit(self.a, new_content=self.BASE.replace('five', 'FIVE'))['accepted'])
        result = self.submit(self.b, new_content=self.BASE.replace('five', 'cinq'))
        self.assertFalse(result['accepted'])
        self.assertTrue(result['conflicts'])

    def test_generated_diff_is_readable_afterwards(self):
        self.queue()
        patch_id = self.submit(self.a, new_content=self.BASE.replace('four', 'FOUR'))['patch_id']
        diff = self.call(self.a, 'read', batch_id=self.batch['id'], content='patch', patch_id=patch_id)['content']
        self.assertIn('-four\n+FOUR\n', diff)

    def test_resubmitting_content_replaces_own_patch(self):
        self.queue()
        self.submit(self.a, new_content=self.BASE.replace('one', 'ONE'))
        self.assertTrue(self.submit(self.a, new_content=self.BASE.replace('ten', 'TEN'))['accepted'])
        self.assertEqual(self.finish(), self.BASE.replace('ten', 'TEN'))

    def test_large_rewrite_is_not_held_to_the_diff_size_cap(self):
        base = ''.join(f'line {i}\n' for i in range(3000))
        self.queue(base)
        new = base.replace('line', 'LINE')
        self.assertTrue(self.submit(self.a, new_content=new)['accepted'])
        self.assertEqual(self.finish(), new)

    # Line endings and final newline
    def test_crlf_base_keeps_its_endings_when_a_script_writes_lf(self):
        base = self.BASE.replace('\n', '\r\n')
        self.queue(base)
        result = self.submit(self.a, new_content=self.BASE.replace('six', 'SIX'))
        self.assertTrue(result['accepted'])
        self.assertEqual(self.finish(), base.replace('six', 'SIX'))

    def test_change_to_a_last_line_without_newline(self):
        self.queue('alpha\nbeta')
        self.assertTrue(self.submit(self.a, new_content='alpha\nBETA')['accepted'])
        self.assertEqual(self.finish(), 'alpha\nBETA')

    def test_adding_a_final_newline(self):
        self.queue('alpha\nbeta')
        self.assertTrue(self.submit(self.a, new_content='alpha\nbeta\n')['accepted'])
        self.assertEqual(self.finish(), 'alpha\nbeta\n')

    def test_insertion_among_repeated_lines_lands_exactly(self):
        base = 'x\n}\n}\n}\ny\n'
        self.queue(base)
        new = 'x\n}\n}\nadded\n}\ny\n'
        self.assertTrue(self.submit(self.a, new_content=new)['accepted'])
        self.assertEqual(self.finish(), new)

    def test_non_ascii_content(self):
        self.queue('Tiêu đề — một\nhai\n')
        self.assertTrue(self.submit(self.a, new_content='Tiêu đề — một\nHAI · ba\n')['accepted'])
        self.assertEqual(self.finish(), 'Tiêu đề — một\nHAI · ba\n')

    # Bad input
    def test_exactly_one_submission_form(self):
        self.queue()
        new = self.BASE.replace('one', 'ONE')
        for extra in ({}, {'new_content': new, 'new_content_path': self.scratch_file(new)},
                      {'new_content': new, 'patch': '--- a/Shared.txt\n+++ b/Shared.txt\n'}):
            with self.subTest(keys=sorted(extra)), self.assertRaises(BoardError):
                self.submit(self.a, **extra)

    def test_unchanged_content_is_refused(self):
        self.queue()
        with self.assertRaises(BoardError):
            self.submit(self.a, new_content=self.BASE)

    def test_line_ending_only_change_is_refused_as_no_change(self):
        self.queue()
        with self.assertRaises(BoardError):
            self.submit(self.a, new_content=self.BASE.replace('\n', '\r\n'))

    def test_stale_base_hash_is_refused(self):
        self.queue()
        with self.assertRaises(BoardError):
            self.call(self.a, 'submit', batch_id=self.batch['id'], base_hash='0' * 64, new_content=self.BASE + 'x\n')

    def test_bad_content_values(self):
        self.queue()
        for value in (5, None, 'has\x00nul\n', 'x' * (1024 * 1024 + 1)):
            with self.subTest(value=str(value)[:20]), self.assertRaises(BoardError):
                self.submit(self.a, new_content=value)

    def test_bad_content_paths(self):
        self.queue()
        folder = Path(self.scratch.name) / 'folder'
        folder.mkdir()
        for value in (str(self.path), str(Path(self.scratch.name) / 'missing.txt'), str(folder),
                      self.scratch_file(b'\xff\xfe bad', 'bad.txt'), self.scratch_file('a\x00b\n', 'nul.txt'),
                      self.scratch_file('x' * (1024 * 1024 + 1), 'big.txt'), 7):
            with self.subTest(value=str(value)[-20:]), self.assertRaises(BoardError):
                self.submit(self.a, new_content_path=value)
        self.assertEqual(self.path.read_text(), self.BASE)


if __name__ == '__main__':
    unittest.main()
