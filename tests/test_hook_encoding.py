import difflib
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import swarmboard_hook
from board.core import Hub
from board.hooks import Adapter


def run_hook(raw, locale='cp1252'):
    """Run main() on raw stdin bytes, as Windows Python sees them under a non-UTF-8 locale."""
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding=locale)
    echo = lambda self, payload: {'echo': payload.get('tool_input')}
    with patch('sys.argv', ['swarmboard_hook.py', '--harness', 'claude']), patch('sys.stdin', stdin), \
            patch('swarmboard_hook.hook_connection', return_value=('C:/w', 'C:/w/.board/hub.sqlite', 'http://127.0.0.1:1')), \
            patch.object(Adapter, 'handle', echo), redirect_stdout(io.StringIO()) as output:
        swarmboard_hook.main()
    return json.loads(output.getvalue())


class HookEncodingTests(unittest.TestCase):
    def payload(self, tool_input):
        return {'cwd': 'C:/w', 'session_id': 'chat', 'hook_event_name': 'PreToolUse',
                'tool_name': 'mcp__board__patch_queue', 'tool_input': tool_input}

    def test_ascii_payload_round_trips(self):
        tool_input = {'patch': '--- a/x\n+++ b/x\n', 'reason': 'plain'}
        self.assertEqual(run_hook(json.dumps(self.payload(tool_input)).encode())['echo'], tool_input)

    def test_utf8_payload_is_not_read_in_the_locale_codepage(self):
        tool_input = {'patch': 'a \u2014 b \u00b7 c', 'reason': 'Vi\u1ec7t \U0001f600'}
        raw = json.dumps(self.payload(tool_input), ensure_ascii=False).encode('utf-8')
        self.assertEqual(run_hook(raw)['echo'], tool_input)

    def test_utf8_byte_order_mark_is_accepted(self):
        tool_input = {'reason': '\u2014'}
        raw = b'\xef\xbb\xbf' + json.dumps(self.payload(tool_input), ensure_ascii=False).encode('utf-8')
        self.assertEqual(run_hook(raw)['echo'], tool_input)

    def test_invalid_utf8_never_vetoes_the_tool(self):
        self.assertEqual(run_hook(b'{"tool_input": {"reason": "\xff\xfe"}}'), {})

    def test_text_only_stdin_still_works(self):
        tool_input = {'reason': '\u2014'}
        stdin = io.StringIO(json.dumps(self.payload(tool_input), ensure_ascii=False))
        with patch('sys.argv', ['swarmboard_hook.py', '--harness', 'claude']), patch('sys.stdin', stdin), \
                patch('swarmboard_hook.hook_connection', return_value=('C:/w', 'C:/w/db', 'http://127.0.0.1:1')), \
                patch.object(Adapter, 'handle', lambda self, payload: {'echo': payload['tool_input']}), \
                redirect_stdout(io.StringIO()) as output:
            swarmboard_hook.main()
        self.assertEqual(json.loads(output.getvalue())['echo'], tool_input)


class PublishCasingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.agent = self.hub.call('register', {'name': 'A'})

    def publish(self, relative, base, new):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base.encode('utf-8'))
        call = lambda action, **p: self.hub.call('patch_queue', {**p, 'action': action, **self.agent})
        batch = call('join', path=str(path), reason='shared')['batch']
        name = relative.replace('\\', '/')
        diff = ''.join(difflib.unified_diff(base.splitlines(True), new.splitlines(True), 'a/' + name, 'b/' + name))
        self.assertTrue(call('submit', batch_id=batch['id'], base_hash=batch['base_hash'], patch=diff)['accepted'])
        return call('release', batch_id=batch['id'])['batch']

    def test_publish_keeps_the_files_exact_name(self):
        batch = self.publish('CLAUDE.md', 'one\ntwo\n', 'one\nTWO\n')
        self.assertEqual(batch['state'], 'APPLIED')
        self.assertIn('CLAUDE.md', os.listdir(self.root))
        self.assertNotIn('claude.md', os.listdir(self.root))
        self.assertEqual((self.root / 'CLAUDE.md').read_text(), 'one\nTWO\n')

    def test_publish_keeps_mixed_case_folders_and_file(self):
        self.publish('Assets/Scripts/BasePanels.cs', 'class BasePanels {}\n', 'class BasePanels { }\n')
        self.assertEqual(os.listdir(self.root / 'Assets'), ['Scripts'])
        self.assertEqual(os.listdir(self.root / 'Assets' / 'Scripts'), ['BasePanels.cs'])

    def test_non_ascii_context_lines_apply(self):
        base = 'Title \u2014 one\nmiddle \u00b7 dot\nend\n'
        batch = self.publish('Notes.md', base, base.replace('end', 'END'))
        self.assertEqual(batch['state'], 'APPLIED')
        self.assertEqual((self.root / 'Notes.md').read_text(encoding='utf-8'), base.replace('end', 'END'))


if __name__ == '__main__':
    unittest.main()
