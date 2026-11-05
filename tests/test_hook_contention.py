import hashlib
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from board.core import Hub
from board.hooks import Adapter, owns_inbox_binding


class HookContentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.database = self.root / 'hub.sqlite'
        self.hub = Hub(self.database, self.root)
        self.key = hashlib.sha256(b'claude:chat').hexdigest()
        self.agent = self.hub.call('register', {'name': 'c', 'native_harness': 'claude', 'native_session_key': self.key})['agent_id']
        self.blocker = None

    def tearDown(self):
        if self.blocker is not None:
            self.blocker.rollback()
            self.blocker.close()

    def mirror(self):
        with self.hub.connect() as db:
            return db.execute('SELECT harness,agent,session_key FROM hook_inbox_bindings').fetchall()

    def hold(self, mode, seconds=None):
        self.blocker = sqlite3.connect(self.database, check_same_thread=False, isolation_level=None)
        self.blocker.execute('BEGIN ' + mode)
        if seconds is not None:
            threading.Timer(seconds, self.blocker.rollback).start()

    # Basics
    def test_bound_agent_owns_and_is_mirrored(self):
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.assertEqual([tuple(r) for r in self.mirror()], [('claude', self.agent, self.key)])

    def test_repeated_checks_keep_one_mirror_row(self):
        for _ in range(3):
            self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.assertEqual(len(self.mirror()), 1)

    def test_unbound_session_does_not_own(self):
        self.assertFalse(owns_inbox_binding(self.database, 'claude', hashlib.sha256(b'claude:other').hexdigest(), self.agent))

    def test_retired_agent_no_longer_owns_even_when_mirrored(self):
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        with self.hub.connect() as db:
            db.execute("UPDATE agents SET state='retired' WHERE id=?", (self.agent,))
        self.assertFalse(owns_inbox_binding(self.database, 'claude', self.key, self.agent))

    def test_rebound_session_moves_ownership_and_mirror(self):
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        other = self.hub.call('register', {'name': 'd'})['agent_id']
        with self.hub.connect() as db:
            db.execute('UPDATE native_sessions SET agent=? WHERE session_key=?', (other, self.key))
        self.assertFalse(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, other))
        self.assertEqual([tuple(r) for r in self.mirror()], [('claude', other, self.key)])

    # Contention
    def test_current_binding_check_needs_no_write_lock(self):
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.hold('IMMEDIATE')  # Another writer mid-transaction; readers may still proceed.
        start = time.time()
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.assertLess(time.time() - start, 1)

    def test_first_binding_waits_out_a_brief_writer(self):
        self.hold('EXCLUSIVE', seconds=4.5)
        self.assertTrue(owns_inbox_binding(self.database, 'claude', self.key, self.agent))
        self.assertEqual(len(self.mirror()), 1)

    def test_hook_write_waits_out_a_brief_writer(self):
        self.hold('EXCLUSIVE', seconds=4.5)
        self.assertTrue(Adapter(self.root, self.database, 'claude').declare(self.key, self.agent))


if __name__ == '__main__':
    unittest.main()
