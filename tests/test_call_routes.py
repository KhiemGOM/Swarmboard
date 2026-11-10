"""Claude Code auto mode refuses calls whose input a hook rewrote, so board calls must reach the
bridge unchanged. The hook records the chat's route under the call's tool_use_id instead, and the
bridge reads it from the tools/call _meta["claudecode/toolUseId"] that Claude Code sends."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from board import call_routes
from board.core import Hub
from board.hooks import Adapter
from tests.test_route_redesign import BridgeCase, CLEAN_ENV, route


class CallRouteStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch.dict(os.environ, {'SWARMBOARD_CALLS_DIR': self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_route_is_taken_once(self):
        value = route('chat', 'C:\\w')
        self.assertTrue(call_routes.record('toolu_01ABC', value))
        self.assertEqual(call_routes.take('toolu_01ABC'), value)
        self.assertIsNone(call_routes.take('toolu_01ABC'))

    def test_unknown_or_malformed_ids_give_nothing(self):
        self.assertIsNone(call_routes.take('toolu_missing'))
        for bad in ('', '../x', 'a/b', 'x' * 300, None, 5):
            with self.subTest(bad=str(bad)[:20]):
                self.assertFalse(call_routes.record(bad, route('chat', 'C:\\w')))
                self.assertIsNone(call_routes.take(bad))
        self.assertEqual(os.listdir(self.temp.name), [])

    def test_abandoned_routes_expire(self):
        call_routes.record('toolu_old', route('chat', 'C:\\w'))
        old = time.time() - 3600
        os.utime(Path(self.temp.name) / 'toolu_old.json', (old, old))
        call_routes.record('toolu_new', route('chat', 'C:\\w'))
        self.assertIsNone(call_routes.take('toolu_old'))
        self.assertIsNotNone(call_routes.take('toolu_new'))


class HookRecordsRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / 'project'
        self.root.mkdir()
        self.calls = Path(self.temp.name) / 'calls'
        patcher = patch.dict(os.environ, {'SWARMBOARD_CALLS_DIR': str(self.calls)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hub = Hub(self.root / '.board/hub.sqlite', self.root)
        self.adapter = Adapter(self.root, self.root / '.board/hub.sqlite', 'claude', caller=lambda url, method, params: self.hub.call(method, params))

    def event(self, event, **params):
        return self.adapter.handle({'session_id': 'chat', 'cwd': str(self.root), 'hook_event_name': event, **params})

    def test_board_call_input_is_left_unchanged_and_route_recorded(self):
        self.event('UserPromptSubmit', prompt='/board')
        reply = self.event('PreToolUse', tool_name='mcp__board__claim', tool_use_id='toolu_01', tool_input={'paths': ['a.py'], 'reason': 'r'})
        self.assertNotIn('updatedInput', reply.get('hookSpecificOutput', {}))
        self.assertNotEqual(reply.get('hookSpecificOutput', {}).get('permissionDecision'), 'deny')
        self.assertEqual(call_routes.take('toolu_01'), {'harness': 'claude', 'session_id': 'chat', 'cwd': str(self.root)})

    def test_opted_out_chat_is_still_refused_and_nothing_recorded(self):
        reply = self.event('PreToolUse', tool_name='mcp__board__board_status', tool_use_id='toolu_02', tool_input={})
        self.assertEqual(reply['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIsNone(call_routes.take('toolu_02'))

    def test_payload_without_tool_use_id_keeps_the_old_stamp(self):
        self.event('UserPromptSubmit', prompt='/board')
        reply = self.event('PreToolUse', tool_name='mcp__board__board_status', tool_input={})
        self.assertEqual(reply['hookSpecificOutput']['updatedInput']['_route']['session_id'], 'chat')


class BridgeReadsRecordedRouteTests(BridgeCase):
    def setUp(self):
        super().setUp()
        self.calls = self.base / 'calls'
        self.env = {**CLEAN_ENV, 'SWARMBOARD_CALLS_DIR': str(self.calls)}
        patcher = patch.dict(os.environ, {'SWARMBOARD_CALLS_DIR': str(self.calls)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, bridge, name, tool_use_id, arguments=None):
        reply = bridge.request('tools/call', {'name': name, 'arguments': arguments or {},
                                              '_meta': {'claudecode/toolUseId': tool_use_id}})
        result = reply['result']
        return result['isError'], json.loads(result['content'][0]['text'])

    def test_shared_bridge_routes_by_recorded_tool_use_id(self):
        fire, notice = self.project('FireCracking'), self.project('AgentsNoticeBoard')
        shared = self.bridge(cwd=self.base, env=self.env)
        call_routes.record('toolu_a', route('chat-fire', fire.root))
        call_routes.record('toolu_b', route('chat-notice', notice.root))
        error, first = self.call(shared, 'board_status', 'toolu_a', {'include_rules': False})
        self.assertFalse(error, first)
        error, second = self.call(shared, 'board_status', 'toolu_b', {'include_rules': False})
        self.assertFalse(error, second)
        self.assertEqual([a['id'] for a in fire.agents()], [first['your_agent_id']])
        self.assertEqual([a['id'] for a in notice.agents()], [second['your_agent_id']])

    def test_recorded_route_beats_a_model_written_route(self):
        fire, notice = self.project('FireCracking'), self.project('AgentsNoticeBoard')
        shared = self.bridge(cwd=self.base, env=self.env)
        call_routes.record('toolu_c', route('chat-fire', fire.root))
        error, output = self.call(shared, 'board_status', 'toolu_c', {'include_rules': False, '_route': route('forged', notice.root)})
        self.assertFalse(error, output)
        self.assertEqual(notice.agents(), [])
        self.assertEqual([a['id'] for a in fire.agents()], [output['your_agent_id']])

    def test_without_a_recorded_route_the_shared_bridge_still_refuses(self):
        self.project('FireCracking')
        shared = self.bridge(cwd=self.base, env=self.env)
        error, output = self.call(shared, 'board_status', 'toolu_none')
        self.assertTrue(error)
        self.assertIn('SHARED_BRIDGE', output['error'])


if __name__ == '__main__':
    unittest.main()
