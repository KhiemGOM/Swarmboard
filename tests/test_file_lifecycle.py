import tempfile
import time
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board import file_lifecycle as lifecycle


class FileLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.agent = self.hub.call('register', {'name': 'owner'})

    def call(self, method, **params):
        return self.hub.call(method, {**self.agent, **params})

    def reconcile(self):
        with self.hub.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return lifecycle.reconcile(self.hub, db, self.agent['agent_id'], time.time())

    def test_missing_existing_file_releases_exact_claim_preserves_history(self):
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=[str(path)], reason='work')
        self.reconcile()
        path.unlink()
        deleted = self.reconcile()
        self.assertEqual(deleted[0]['released_claim_ids'], claim['claim_ids'])
        status = self.call('board_status', include_rules=False)
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['state'], 'HELD')
        self.assertIn('deleted', status['custody'][0]['reason'])
        self.assertEqual(self.reconcile(), [])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM file_deletions').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='file_deleted'").fetchone()[0], 1)

    def test_claim_for_not_yet_created_file_is_not_stale(self):
        claim = self.call('claim', paths=['new.py'], reason='Create file')
        self.assertEqual(self.reconcile(), [])
        self.assertEqual(self.call('board_status')['claims'][0]['id'], claim['claim_ids'][0])

    def test_folder_claim_remains_after_child_deletion(self):
        folder = self.root / 'src'
        folder.mkdir()
        path = folder / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['src'], reason='Folder work')
        with self.hub.connect() as db:
            lifecycle.initialize(db)
            db.execute('INSERT INTO file_observations VALUES (?,1,?)', ('src/a.py', time.time()))
        path.unlink()
        self.reconcile()
        self.assertEqual(self.call('board_status')['claims'][0]['id'], claim['claim_ids'][0])

    def test_explicit_report_refuses_existing_and_foreign_paths(self):
        path = self.root / 'a.py'
        path.write_text('work')
        self.call('claim', paths=['a.py'], reason='Work')
        with self.hub.connect() as db:
            with self.assertRaisesRegex(BoardError, 'verified missing'):
                lifecycle.dispatch(self.hub, db, {'action': 'deleted', 'path': 'a.py', 'reason': 'removed'}, self.agent['agent_id'], time.time())
            with self.assertRaisesRegex(BoardError, 'outside this board'):
                lifecycle.dispatch(self.hub, db, {'action': 'deleted', 'path': str(self.root.parent / 'outside'), 'reason': 'removed'}, self.agent['agent_id'], time.time())

    def test_recreated_file_can_be_claimed_and_deleted_again(self):
        path = self.root / 'a.py'
        path.write_text('first')
        self.call('claim', paths=['a.py'], reason='First')
        self.reconcile()
        path.unlink()
        self.reconcile()
        path.write_text('second')
        second = self.call('claim', paths=['a.py'], reason='Second')
        self.reconcile()
        path.unlink()
        deleted = self.reconcile()
        self.assertEqual(deleted[0]['released_claim_ids'], second['claim_ids'])
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM file_deletions').fetchone()[0], 2)

    def test_unreadable_path_is_not_treated_as_deleted(self):
        from unittest.mock import patch
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        self.reconcile()
        with patch('board.file_lifecycle.existence', return_value='unreadable'):
            self.assertEqual(self.reconcile(), [])
        self.assertEqual(self.call('board_status')['claims'][0]['id'], claim['claim_ids'][0])

    def test_reclaimed_missing_path_recreated_and_deleted_between_calls(self):
        path = self.root / 'a.py'
        path.write_text('first')
        self.call('claim', paths=['a.py'], reason='First lifecycle')
        path.unlink()
        first = self.call('board_file', action='deleted', path='a.py', reason='First deletion')
        second_claim = self.call('claim', paths=['a.py'], reason='Recreate missing file')
        path.write_text('second')
        path.unlink()
        second = self.call('board_file', action='deleted', path='a.py', reason='Second deletion')
        self.assertFalse(second['duplicate'])
        self.assertNotEqual(second['deletion_id'], first['deletion_id'])
        self.assertEqual(second['released_claim_ids'], second_claim['claim_ids'])
        self.assertEqual(self.call('board_status', include_rules=False)['claims'], [])
        again = self.call('board_file', action='deleted', path='a.py', reason='Repeat second report')
        self.assertTrue(again['duplicate'])
        self.assertEqual(again['deletion_id'], second['deletion_id'])

    def test_reconciliation_survives_hub_restart(self):
        path = self.root / 'a.py'
        path.write_text('work')
        self.call('claim', paths=['a.py'], reason='Work')
        self.reconcile()
        path.unlink()
        self.hub = Hub(self.root / 'hub.sqlite', self.root)
        self.assertEqual(len(self.reconcile()), 1)

    def test_non_custodian_cannot_explicitly_report_deletion(self):
        path = self.root / 'a.py'
        path.write_text('work')
        self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        with self.hub.connect() as db:
            with self.assertRaisesRegex(BoardError, 'custodian'):
                lifecycle.dispatch(self.hub, db, {'action': 'deleted', 'path': 'a.py', 'reason': 'removed'}, 'other', time.time())

    def test_deletion_without_hooks_is_reconciled_on_next_board_call(self):
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        status = self.call('board_status', include_rules=False)
        self.assertEqual(status['claims'], [])
        self.assertEqual(status['custody'][0]['filesystem_state'], 'deleted')
        records = self.call('board_file', action='history', path='a.py')['records']
        self.assertEqual(records[0]['released_claims'], claim['claim_ids'])
        self.assertEqual(len(records), 1)

    def test_deleted_claim_can_record_commit_without_touching_new_claim(self):
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        self.call('board_file', action='deleted', path='a.py', reason='Remove obsolete module')
        self.call('release', claim_ids=claim['claim_ids'], commit_hash='abcdef1')
        self.assertEqual(self.call('board_status')['custody'][0]['state'], 'COMMITTED')
        path.write_text('new')
        new = self.call('claim', paths=['a.py'], reason='New work')
        with self.assertRaises(BoardError):
            self.call('release', claim_ids=claim['claim_ids'], commit_hash='abcdef2')
        self.assertEqual(self.call('board_status')['claims'][0]['id'], new['claim_ids'][0])

    def test_deleted_custody_can_be_resolved_with_commit_evidence(self):
        path = self.root / 'a.py'
        path.write_text('work')
        self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        self.call('board_status')
        self.call('custody_resolve', paths=['a.py'], commit_hash='abcdef1')
        self.assertEqual(self.call('board_status')['custody'][0]['state'], 'COMMITTED')

    def test_explicit_child_deletion_preserves_covering_folder_claim(self):
        folder = self.root / 'src'
        folder.mkdir()
        path = folder / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['src'], reason='Folder work')
        self.call('board_file', action='observe', path='src/a.py')
        path.unlink()
        self.call('board_file', action='deleted', path='src/a.py', reason='Remove module')
        status = self.call('board_status')
        self.assertEqual(status['claims'][0]['id'], claim['claim_ids'][0])
        child = next(row for row in status['custody'] if row['path'] == 'src/a.py')
        self.assertEqual(child['state'], 'HELD')
        self.assertEqual(child['filesystem_state'], 'deleted')

    def test_manual_terminal_cleanup_uses_cwd_and_registers_no_agent(self):
        import json
        import subprocess
        import sys
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        config = self.root / '.codex/board-operator.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'workspace': str(self.root), 'database': str(self.root / 'hub.sqlite'), 'port': 4318}))
        path.unlink()
        entry = Path(__file__).resolve().parents[1] / 'boardctl.py'
        result = subprocess.run([sys.executable, str(entry), 'cleanup'], cwd=self.root, capture_output=True, text=True, check=True)
        output = json.loads(result.stdout)
        self.assertEqual(Path(output['workspace']), self.root)
        self.assertEqual(output['deletion_count'], 1)
        self.assertEqual(output['released_claim_count'], 1)
        self.assertEqual(output['deletions'][0]['released_claim_ids'], claim['claim_ids'])
        status = self.call('board_status')
        self.assertEqual(len(status['agents']), 1)
        self.assertEqual(status['custody'][0]['state'], 'HELD')
        again = subprocess.run([sys.executable, str(entry), 'cleanup'], cwd=self.root, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(again.stdout)['deletion_count'], 0)

    def test_manual_cleanup_refuses_wrong_workspace_and_missing_database(self):
        import subprocess
        import sys
        entry = Path(__file__).resolve().parents[1] / 'boardctl.py'
        other = self.root / 'other'
        other.mkdir()
        result = subprocess.run([sys.executable, str(entry), 'cleanup', '--workspace', str(other), '--database', str(self.root / 'hub.sqlite')], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different workspace', result.stderr)
        result = subprocess.run([sys.executable, str(entry), 'cleanup', '--workspace', str(other)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((other / '.board/hub.sqlite').exists())

    def test_manual_mcp_cleanup_reports_actual_work_and_is_idempotent(self):
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        result = self.call('board_cleanup')
        self.assertEqual(result['deletion_count'], 1)
        self.assertEqual(result['released_claim_count'], 1)
        self.assertEqual(result['deletions'][0]['released_claim_ids'], claim['claim_ids'])
        self.assertEqual(self.call('board_cleanup')['deletion_count'], 0)
        self.assertEqual(self.call('board_status')['custody'][0]['state'], 'HELD')

    def test_mcp_cleanup_accepts_known_id_and_rejects_unknown_id(self):
        path = self.root / 'a.py'
        path.write_text('work')
        claim = self.call('claim', paths=['a.py'], reason='Work')
        path.unlink()
        with self.assertRaisesRegex(BoardError, 'Unknown agent'):
            self.hub.call('board_cleanup', {'agent_id': 'unknown'})
        with self.hub.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM claims WHERE id=?', (claim['claim_ids'][0],)).fetchone()[0], 1)
        self.assertEqual(self.hub.call('board_cleanup', {'agent_id': self.agent['agent_id']})['deletion_count'], 1)
