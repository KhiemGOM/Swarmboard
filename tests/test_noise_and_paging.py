import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from board.core import BoardError, Hub
from board.responses import encode, MAX_RESPONSE_CHARS
from board.runtime import ensure_hub


class NoisePagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / 'hub.sqlite'
        self.hub = Hub(self.db, self.root)
        self.a = self.hub.call('register', {'name': 'A'})
        self.b = self.hub.call('register', {'name': 'B'})

    def call(self, method, who=None, **params):
        who = who or self.a
        return self.hub.call(method, {**params, 'agent_id': who['agent_id'], 'session_token': who['session_token']})

    def test_preview_once_across_tool_and_hook_without_ack_and_after_restart(self):
        message = self.call('inbox_send', who=self.b, recipient_agent_id=self.a['agent_id'], body='New instruction')['id']
        first = self.call('hook_status')
        self.assertEqual(first['inbox_preview'][0]['id'], message)
        self.assertEqual(self.call('board_info')['inbox_preview'], [])
        self.hub = Hub(self.db, self.root)
        self.assertEqual(self.call('hook_status')['inbox_preview'], [])
        self.assertEqual(self.call('inbox_read')['messages'][0]['id'], message)
        self.assertEqual(self.call('board_info')['unread_inbox'], 1)

    def test_custody_reminders_only_changes_and_stop(self):
        claim = self.call('claim', paths=['a.py'], reason='Edit')
        released = self.call('release', claim_ids=claim['claim_ids'], held_reason='Await commit')
        self.assertIn('custody_reminder', released)
        self.assertNotIn('custody_reminder', self.call('board_info'))
        self.assertIn('custody_reminder', self.call('hook_status', force_reminders=True))
        self.assertNotIn('custody_reminder', self.call('hook_status'))

    def test_status_cursors_cover_every_entity_under_cap(self):
        with self.hub.connect() as db:
            for i in range(150):
                db.execute('INSERT INTO agents(id,name,token,state,started,seen) VALUES (?,?,?,\'active\',0,0)', (str(i), 'Agent ' + str(i), 'private'))
        seen, cursor = set(), {}
        for _ in range(30):
            result = self.call('board_status', cursor=cursor)
            self.assertLessEqual(len(encode(result)), MAX_RESPONSE_CHARS)
            ids = {a['id'] for a in result['agents']}
            self.assertFalse(seen & ids)
            seen |= ids
            if not result['has_more']:
                break
            cursor = result['next_cursor']
        self.assertEqual(len(seen), 152)

    def test_large_single_record_explicit_summary_and_full_detail(self):
        with self.hub.connect() as db:
            ident = db.execute("INSERT INTO jobs(title,description,acceptance,kind,state,creator,created,updated) VALUES ('Big',?,?,'code','OPEN',?,0,0)", ('x'*16000, 'y'*16000, self.a['agent_id'])).lastrowid
        page = self.call('job_list')
        self.assertIn('_detail', page['jobs'][0])
        chunks, offset = [], 0
        for _ in range(20):
            part = self.call('board_read', entity='jobs', record_id=ident, offset=offset)
            self.assertLessEqual(len(encode(part)), MAX_RESPONSE_CHARS)
            chunks.append(part['content'])
            if not part['has_more']:
                break
            offset = part['next_offset']
        restored = json.loads(''.join(chunks))
        self.assertEqual(restored['description'], 'x'*16000)
        self.assertEqual(restored['acceptance'], 'y'*16000)
        with self.assertRaises(BoardError):
            self.call('board_read', entity='messages', record_id=1000)

    def test_partial_claims_are_explicit_and_atomic_default_unchanged(self):
        self.call('claim', who=self.b, paths=['blocked.py'], reason='Other work')
        denied = self.call('claim', paths=['free.py', 'blocked.py'], reason='Batch')
        self.assertFalse(denied['accepted'])
        partial = self.call('claim', paths=['free.py', 'blocked.py'], reason='Batch', partial=True)
        self.assertFalse(partial['accepted'])
        self.assertEqual([r['accepted'] for r in partial['results']], [True, False])
        self.assertEqual(len(partial['claim_ids']), 1)

    def test_job_delete_preserves_history_and_refuses_another_live_owner(self):
        job = self.call('job_post_and_claim', title='Task', description='Work', acceptance='Done')['job']
        with self.assertRaises(BoardError):
            self.call('job_delete', who=self.b, job_id=job['id'], reason='Obsolete')
        self.call('job_delete', job_id=job['id'], lease_token=job['lease_token'], reason='Owner cancelled')
        self.assertEqual(self.call('job_list')['jobs'], [])
        history = self.call('job_list', state='DELETED')['jobs'][0]
        self.assertEqual(history['deletion_reason'], 'Owner cancelled')
        self.assertNotIn('lease_token', history)

    def test_ignored_scratch_release_closes_custody_but_keeps_audit(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        (self.root / '.gitignore').write_text('scratch/\n', encoding='utf-8')
        (self.root / 'scratch').mkdir()
        (self.root / 'scratch' / 'image.txt').write_text('artifact', encoding='utf-8')
        claim = self.call('claim', paths=['scratch'], reason='Screenshot')
        result = self.call('release', claim_ids=claim['claim_ids'], reason='Artifact finished')
        self.assertEqual(result['scratch_completed'], ['scratch'])
        self.assertEqual(self.call('board_status', paths=['scratch'])['custody'], [])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT path FROM scratch_completions').fetchone()[0], 'scratch')
        history = self.call('board_file', action='history', path='scratch')
        self.assertEqual(history['scratch_history']['record_id'], 'scratch')
        audit = self.call('board_read', entity='scratch', record_id='scratch')
        self.assertEqual(json.loads(audit['content'])[0]['owner'], self.a['agent_id'])
        (self.root / 'scratch' / 'code.txt').write_text('tracked', encoding='utf-8')
        subprocess.run(['git', 'add', '-f', 'scratch/code.txt'], cwd=self.root, check=True)
        tracked = self.call('claim', paths=['scratch'], reason='Tracked code')
        with self.assertRaises(BoardError):
            self.call('release', claim_ids=tracked['claim_ids'], reason='Cannot discard tracked code')

    def test_status_closes_existing_ignored_custody_before_building_page(self):
        subprocess.run(['git', 'init', '-q'], cwd=self.root, check=True)
        (self.root / '.gitignore').write_text('old-scratch/\n', encoding='utf-8')
        with self.hub.connect() as db:
            db.execute("INSERT INTO custody VALUES ('old-scratch',?,'HELD','Old artifact',NULL,0)", (self.a['agent_id'],))
        result = self.call('board_status', paths=['old-scratch'])
        self.assertEqual(result['custody'], [])
        self.assertNotIn('custody_reminder', result)

    def test_restart_keeps_credentials_and_database_id_and_wrong_db_is_rejected(self):
        ident = self.hub.database_id
        self.hub = Hub(self.db, self.root)
        self.assertEqual(self.hub.database_id, ident)
        self.assertEqual(self.call('board_info')['database_id'], ident)
        other = Hub(self.root / 'other.sqlite', self.root)
        with patch('board.runtime.health', return_value={'service':'agent-board','workspace':str(self.root),'database_id':other.database_id}):
            with self.assertRaisesRegex(BoardError, 'different database'):
                ensure_hub('http://127.0.0.1:4317', self.root, self.db)

    def test_detail_never_exposes_another_agents_credentials(self):
        record = self.call('board_read', entity='agents', record_id=self.b['agent_id'])
        data = json.loads(record['content'])
        self.assertNotIn('token', data)
        self.assertNotIn('inheritance_hash', data)
        self.assertNotIn(self.b['session_token'], record['content'])

    def test_inbox_page_marks_only_delivered_messages_for_preview(self):
        ids = []
        for i in range(3):
            ids.append(self.call('inbox_send', who=self.b, recipient_agent_id=self.a['agent_id'], body=str(i) + 'x'*15990)['id'])
        page = self.call('inbox_read')
        self.assertEqual(len(page['messages']), 1)
        following = self.call('hook_status')['inbox_preview']
        self.assertEqual([row['id'] for row in following], ids[1:])
        self.assertEqual(self.call('hook_status')['inbox_preview'], [])
        self.assertEqual(self.call('board_info')['unread_inbox'], 3)
