import tempfile
import unittest
from pathlib import Path
from board.core import BoardError, Hub
from board.responses import MAX_RESPONSE_CHARS, check, encode
from board.mcp import TOOLS


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.hub = Hub(root / 'hub.sqlite', root)
        self.identity = self.hub.call('register', {})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, **params):
        return self.hub.call(method, {**params, 'agent_id': self.identity['agent_id'],
                                      'session_token': self.identity['session_token']})

    def test_boundary_and_unicode(self):
        check('board_status', 'x' * (MAX_RESPONSE_CHARS - 2))
        with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE.*target_agent_id'):
            check('board_status', 'x' * (MAX_RESPONSE_CHARS - 1))
        check('inbox_read', {'body': '好' * 16000})

    def test_status_requires_filters(self):
        with self.hub.connect() as db:
            for i in range(150):
                db.execute('INSERT INTO agents(id,name,token,state,started,seen) VALUES (?,?,?, ?,0,0)',
                           (str(i), 'x' * 200, 'private', 'active'))
        with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE'):
            self.call('board_status')
        self.assertLess(len(encode(self.call('board_status', target_agent_id='self', include_rules=False))), MAX_RESPONSE_CHARS)

    def test_notice_and_inbox_pages_without_ack(self):
        for _ in range(2):
            self.call('noticeboard_post', body='x' * 16000)
            self.call('inbox_send', recipient_agent_id=self.identity['agent_id'], body='x' * 16000)
        for method, key in [('noticeboard_read', 'posts'), ('inbox_read', 'messages')]:
            with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE.*limit=1'):
                self.call(method)
            first = self.call(method, limit=1)[key][0]
            second = self.call(method, limit=1, after_id=first['id'])[key][0]
            self.assertGreater(second['id'], first['id'])
        self.assertEqual(self.call('board_info')['unread_inbox'], 2)

    def test_oversized_mutation_rolls_back(self):
        with self.assertRaisesRegex(BoardError, 'rolled back'):
            self.call('job_post', title='Huge', description='x' * 16000, acceptance='y' * 16000)
        self.assertEqual(self.call('job_list')['jobs'], [])
        self.call('job_post', title='OK', description='x' * 12000, acceptance='OK')
        self.call('job_post', title='OK', description='y' * 12000, acceptance='OK')
        with self.assertRaisesRegex(BoardError, 'RESPONSE_TOO_LARGE'):
            self.call('job_list')
        self.assertEqual(len(self.call('job_list', limit=1)['jobs']), 1)

    def test_page_validation_and_schema(self):
        for method in ('job_list', 'inbox_read', 'noticeboard_read'):
            for invalid in (0, 101, True, '1'):
                with self.assertRaisesRegex(BoardError, 'limit must'):
                    self.call(method, limit=invalid)
            with self.assertRaisesRegex(BoardError, 'after_id must'):
                self.call(method, after_id=-1)
            schema = next(t for t in TOOLS if t['name'] == method)['inputSchema']['properties']
            self.assertIn('limit', schema)
            self.assertIn('after_id', schema)

    def test_info_reports_cap(self):
        self.assertEqual(self.call('board_info')['max_response_chars'], MAX_RESPONSE_CHARS)
