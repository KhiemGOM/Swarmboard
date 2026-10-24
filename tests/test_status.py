import json
import tempfile
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.mcp import TOOLS


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})
        self.call(self.a, 'claim', paths=['src/a'], reason='A files')
        self.call(self.b, 'claim', paths=['src/b'], reason='B files')
        self.call(self.b, 'mutex_claim', resources=['git:index', 'db:test'], reason='B resources')
        job = self.call(self.b, 'job_post', title='B job', description='Fix B', acceptance='B passes')['job']
        self.call(self.b, 'job_claim', job_id=job['id'])
        self.call(self.b, 'inbox_send', recipient_agent_id=self.a['agent_id'], body='Waiting on A')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, method, **params):
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def status(self, **params):
        return self.call(self.a, 'board_status', **params)

    def test_self_exact_id_and_workspace_views_keep_caller_reminders(self):
        whole = self.status()
        self.assertEqual(len(whole['agents']), 2)
        self.assertEqual(len(whole['claims']), 4)
        own = self.status(target_agent_id='self', include_rules=False)
        self.assertEqual(own['status_scope'], self.a['agent_id'])
        self.assertEqual([row['id'] for row in own['agents']], [self.a['agent_id']])
        self.assertTrue(all(row['agent'] == self.a['agent_id'] for row in own['claims']))
        self.assertEqual([row['owner'] for row in own['custody']], [self.a['agent_id']])
        self.assertNotIn('coordination_rules', own)
        other = self.status(target_agent_id=self.b['agent_id'])
        self.assertEqual(len(other['claims']), 3)
        self.assertEqual(other['job_counts'], {'CLAIMED': 1})
        self.assertEqual(own['job_counts'], {})
        self.assertEqual(other['unread_inbox'], 1)
        self.assertNotIn('inbox_reminder', other)
        self.assertEqual(other['inbox_preview'], [])
        self.assertNotIn('owned_jobs', other)
        self.assertNotIn('session_token', json.dumps(other))
        self.assertNotIn('lease_token', json.dumps(other))

    def test_paths_match_parent_child_and_unheld_queries_without_claiming(self):
        before = self.status()['claims']
        child = self.status(paths=[str(self.root / 'src/b/component.py')], include_rules=False)
        self.assertEqual([row['path'] for row in child['claims']], ['src/b'])
        self.assertEqual([row['id'] for row in child['agents']], [self.b['agent_id']])
        self.assertEqual(child['matches'][0]['claim_ids'], [before[1]['id']])
        parent = self.status(paths=['src'])
        self.assertEqual(len(parent['claims']), 2)
        empty = self.status(paths=['unheld.py'])
        self.assertEqual(empty['claims'], [])
        self.assertEqual(empty['agents'], [])
        self.assertEqual(empty['matches'][0]['claim_ids'], [])
        self.assertEqual(self.status()['claims'], before)  # Includes unchanged lease expiry.

    def test_resource_queries_combine_with_paths_and_agent_filter(self):
        result = self.status(resources=['GIT:INDEX'])
        self.assertEqual([row['path'] for row in result['claims']], ['git:index'])
        self.assertEqual(result['custody'], [])
        self.assertEqual([row['id'] for row in result['agents']], [self.b['agent_id']])
        mixed = self.status(paths=['src/a'], resources=['db:test'])
        self.assertEqual(len(mixed['claims']), 2)
        narrowed = self.status(target_agent_id='self', paths=['src'], resources=['git:index'])
        self.assertEqual([row['path'] for row in narrowed['claims']], ['src/a'])
        self.assertEqual(narrowed['matches'][1]['claim_ids'], [])

    def test_retired_agent_held_custody_is_still_inspectable(self):
        self.call(self.a, 'retire', target_agent_id=self.b['agent_id'], reason='Owner requested retirement')
        other = self.status(target_agent_id=self.b['agent_id'])
        self.assertEqual(other['agents'][0]['state'], 'retired')
        self.assertEqual(other['claims'], [])
        self.assertEqual(other['custody'][0]['state'], 'HELD')
        held = self.status(paths=['src/b/file.py'])
        self.assertEqual(held['claims'], [])
        self.assertEqual([row['id'] for row in held['agents']], [self.b['agent_id']])
        self.assertEqual(held['matches'][0]['custody_paths'], ['src/b'])

    def test_invalid_queries_are_rejected(self):
        for params in ({'target_agent_id': ''}, {'target_agent_id': 'missing'},
                       {'target_agent_id': 1}, {'include_rules': 'false'},
                       {'paths': []}, {'paths': 'src'}, {'paths': ['../escape']},
                       {'paths': ['src/*']}, {'resources': ['index']},
                       {'resources': ['git:index'] * 101}):
            with self.subTest(params=params), self.assertRaises(BoardError):
                self.status(**params)

    def test_mcp_advertises_query_options(self):
        schema = next(tool for tool in TOOLS if tool['name'] == 'board_status')['inputSchema']
        self.assertTrue({'target_agent_id', 'paths', 'resources', 'include_rules'} <= schema['properties'].keys())

