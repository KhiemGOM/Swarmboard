import concurrent.futures
import threading
import unittest
import tests.test_hub as hubtests
from board.core import BoardError, Hub


class ResourceTests(unittest.TestCase):
    setUp = hubtests.HubTests.setUp
    tearDown = hubtests.HubTests.tearDown
    call = hubtests.HubTests.call
    def test_resource_conflicts_are_atomic_and_separate_from_files(self):
        first = self.call(self.a, "mutex_claim", resources=["DB:core_test"], reason="tests")
        self.assertTrue(first["accepted"])
        self.assertFalse(self.call(self.b, "claim", paths=["src"], resources=["db:core_test"], reason="mixed")["accepted"])
        self.assertTrue(self.call(self.a, "claim", paths=["src"], reason="no partial B claim")["accepted"])
        self.assertTrue(self.call(self.b, "mutex_claim", resources=["port:3101", "git:index", "build:web/dist", "misc:fixture"], reason="resources")["accepted"])

    def test_concurrent_resource_claim_has_one_winner(self):
        barrier = threading.Barrier(2)
        def compete(who):
            barrier.wait()
            return self.call(who, "mutex_claim", resources=["git:index"], reason="commit")["accepted"]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(compete, [self.a, self.b])), [False, True])

    def test_release_requires_evidence_and_is_atomic(self):
        ids = self.call(self.a, "claim", paths=["a.py", "b.py"], reason="edit")["claim_ids"]
        for evidence in [{}, {"commit_hash": "not-a-hash"}, {"commit_hash": "abcdef0", "held_reason": "later"}]:
            with self.assertRaises(BoardError):
                self.call(self.a, "release", claim_ids=ids, **evidence)
            self.assertEqual(len(self.call(self.a, "board_status")["claims"]), 2)
        self.call(self.a, "release", claim_ids=ids, commit_hash="abcdef0")
        self.assertTrue(all(c["state"] == "COMMITTED" for c in self.call(self.a, "board_status")["custody"]))

    def test_held_custody_survives_restart_and_transfers_on_claim(self):
        ids = self.call(self.a, "claim", paths=["src/a.py"], reason="edit")["claim_ids"]
        self.call(self.a, "release", claim_ids=ids, held_reason="Awaiting targeted checks; I will commit next session")
        self.hub = Hub(self.root / "hub.sqlite", self.root)
        status = self.call(self.a, "board_status")
        self.assertFalse(status["claims"])
        self.assertIn("custody_reminder", status)
        self.assertEqual(status["custody"][0]["owner"], self.a["agent_id"])
        self.call(self.b, "claim", paths=["src"], reason="continue")
        status = self.call(self.b, "board_status")
        self.assertTrue(all(c["owner"] == self.b["agent_id"] for c in status["custody"]))

    def test_only_custodian_can_resolve_held_work(self):
        ids = self.call(self.a, "claim", paths=["a.py"], reason="edit")["claim_ids"]
        self.call(self.a, "release", claim_ids=ids, held_reason="waiting")
        with self.assertRaises(BoardError):
            self.call(self.b, "custody_resolve", paths=["a.py"], commit_hash="abcdef0")
        self.call(self.a, "custody_resolve", paths=["a.py"], commit_hash="abcdef0")
        self.assertNotIn("custody_reminder", self.call(self.a, "inbox_read"))

    def test_parent_release_resolves_inherited_child_custody(self):
        ids = self.call(self.a, "claim", paths=["src/a.py"], reason="edit")["claim_ids"]
        self.call(self.a, "release", claim_ids=ids, held_reason="unfinished")
        parent = self.call(self.b, "claim", paths=["src"], reason="continue")["claim_ids"]
        self.call(self.b, "release", claim_ids=parent, commit_hash="abcdef0")
        self.assertTrue(all(c["state"] == "COMMITTED" for c in self.call(self.b, "board_status")["custody"]))

    def test_resource_release_does_not_require_git_commit(self):
        ids = self.call(self.a, "mutex_claim", resources=["db:core_test"], reason="tests")["claim_ids"]
        self.call(self.a, "release", claim_ids=ids, reason="Tests finished; database idle")
        self.assertFalse(self.call(self.a, "board_status")["custody"])
        self.assertTrue(self.call(self.b, "mutex_claim", resources=["db:core_test"], reason="next tests")["accepted"])

    def test_expiry_keeps_custody_and_takeover_transfers_it(self):
        self.call(self.a, "claim", paths=["a.py"], reason="edit")
        with self.hub.connect() as db:
            db.execute("UPDATE claims SET expires=0")
        self.assertEqual(self.call(self.a, "board_status")["custody"][0]["state"], "HELD")
        self.call(self.b, "takeover", target_agent_id=self.a["agent_id"], reason="continuing unavailable A")
        self.assertEqual(self.call(self.b, "board_status")["custody"][0]["owner"], self.b["agent_id"])

    def test_resource_names_validated_and_own_reclaim_renews(self):
        with self.assertRaises(BoardError):
            self.call(self.a, "mutex_claim", resources=["whatever"], reason="bad")
        first = self.call(self.a, "mutex_claim", resources=["git:index"], reason="first")
        second = self.call(self.a, "mutex_claim", resources=["git:index"], reason="renew")
        self.assertEqual(first["claim_ids"], second["claim_ids"])
