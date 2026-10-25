"""Files outside the board's workspace are ordinary files: no claims, queues or edit gating.

Plan / potential failures covered here:
- The PreToolUse hook denies an edit outside the project (e.g. ~/.claude memory) even for a
  linked chat, or nags about a missing job for it.
- A mixed edit (apply_patch touching inside and outside files) skips the inside check.
- Claiming, queueing or relaxing an outside path gives a vague error instead of telling the
  agent it needs no lock and can edit directly.
- A mixed claim fails entirely because of one outside path, or silently "locks" the outside
  path, or reports it as protected.
- '../' escapes and absolute paths on another drive count as outside; the workspace root and
  its children stay coordinated (no regression for inside paths).
"""
import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.hooks import Adapter


class OutsideCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name).resolve()
        self.root = base / 'project'
        self.root.mkdir()
        self.outside = base / 'elsewhere' / 'memory.md'
        self.outside.parent.mkdir()
        self.outside.write_text('note\n')
        (self.root / 'a.py').write_text('x = 1\n')
        self.hub = Hub(self.root / '.board' / 'hub.sqlite', self.root)
        self.agent = self.hub.call('register', {'name': 'MCP'})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, **params):
        return self.hub.call(method, {**params, 'agent_id': self.agent['agent_id'], 'session_token': self.agent['session_token']})


class HookTests(OutsideCase):
    def setUp(self):
        super().setUp()
        self.adapter = Adapter(self.root, self.hub.database, 'claude', caller=lambda url, method, params: self.hub.call(method, params))
        self.event('UserPromptExpansion', expansion_type='slash_command', command_name='board', command_args='', prompt='/board')
        self.event('PostToolUse', tool_name='mcp__board__board_status',
                   tool_response={'content': [{'type': 'text', 'text': json.dumps({'your_agent_id': self.agent['agent_id']})}]})

    def event(self, event, **params):
        return self.adapter.handle({'session_id': 'chat', 'cwd': str(self.root), 'hook_event_name': event, **params})

    def denied(self, reply):
        return reply.get('hookSpecificOutput', {}).get('permissionDecision') == 'deny'

    def note(self, reply):
        return reply.get('hookSpecificOutput', {}).get('additionalContext', '')

    # --- basics ---

    def test_linked_chat_still_needs_a_claim_inside(self):
        self.assertTrue(self.denied(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'a.py')})))

    def test_edit_outside_is_allowed_without_claim_or_job(self):
        for tool in ('Write', 'Edit'):
            reply = self.event('PreToolUse', tool_name=tool, tool_input={'file_path': str(self.outside)})
            self.assertFalse(self.denied(reply), tool)
            self.assertNotIn('No owned job', self.note(reply))

    def test_relative_escape_is_outside(self):
        reply = self.event('PreToolUse', tool_name='Write', tool_input={'file_path': '../elsewhere/memory.md'})
        self.assertFalse(self.denied(reply))

    # --- potential failures ---

    def test_mixed_patch_still_checks_the_inside_file(self):
        patch = f'*** Begin Patch\n*** Update File: {self.outside}\n*** Update File: {self.root / "a.py"}\n*** End Patch'
        reply = self.event('PreToolUse', tool_name='apply_patch', tool_input={'command': patch})
        self.assertTrue(self.denied(reply))
        self.assertIn('a.py', reply['hookSpecificOutput']['permissionDecisionReason'])

    def test_outside_only_patch_is_allowed(self):
        patch = f'*** Begin Patch\n*** Update File: {self.outside}\n*** End Patch'
        self.assertFalse(self.denied(self.event('PreToolUse', tool_name='apply_patch', tool_input={'command': patch})))

    def test_claimed_inside_edit_still_allowed(self):
        self.call('job_post_and_claim', title='t', description='d', acceptance='a', kind='code')
        self.assertTrue(self.call('claim', paths=[str(self.root / 'a.py')], reason='edit')['accepted'])
        self.assertFalse(self.denied(self.event('PreToolUse', tool_name='Write', tool_input={'file_path': str(self.root / 'a.py')})))


class HubTests(OutsideCase):
    # --- basics ---

    def test_claim_outside_says_edit_directly(self):
        with self.assertRaisesRegex(BoardError, 'edit it directly'):
            self.call('claim', paths=[str(self.outside)], reason='memory')

    def test_mixed_claim_locks_only_inside_paths(self):
        result = self.call('claim', paths=[str(self.root / 'a.py'), str(self.outside)], reason='work')
        self.assertTrue(result['accepted'])
        self.assertEqual(len(result['claim_ids']), 1)
        self.assertEqual(result['outside_workspace'], [str(self.outside)])
        self.assertIn('directly', result['outside_note'])
        claims = self.call('board_status', include_rules=False)['claims']
        self.assertEqual([c['path'] for c in claims], ['a.py'])

    def test_inside_claim_has_no_outside_fields(self):
        result = self.call('claim', paths=[str(self.root / 'a.py')], reason='work')
        self.assertNotIn('outside_workspace', result)

    # --- potential failures ---

    def test_queue_and_relax_outside_say_edit_directly(self):
        with self.assertRaisesRegex(BoardError, 'edit it directly'):
            self.call('patch_queue', action='join', path=str(self.outside), reason='shared')
        with self.assertRaisesRegex(BoardError, 'edit it directly'):
            self.call('board_coordination', action='request_relax', path=str(self.outside), reason='shared')

    def test_dotdot_escape_is_outside(self):
        with self.assertRaisesRegex(BoardError, 'edit it directly'):
            self.call('claim', paths=['../elsewhere/memory.md'], reason='memory')

    def test_bad_paths_keep_their_own_errors(self):
        with self.assertRaisesRegex(BoardError, 'non-empty'):
            self.call('claim', paths=[''], reason='x')
        with self.assertRaisesRegex(BoardError, 'globs'):
            self.call('claim', paths=['*.py'], reason='x')

    def test_rules_tell_agents_outside_files_are_free(self):
        rules = self.call('board_status')['coordination_rules']
        self.assertIn('outside the workspace', ' '.join(rules) if isinstance(rules, list) else rules)


if __name__ == '__main__':
    unittest.main()
