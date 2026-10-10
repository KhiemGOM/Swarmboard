import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from board.core import BoardError, Hub
from board.server import serve


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.hub = Hub(self.root / "hub.sqlite", self.root)
        self.agent = self.hub.call("register", {"name": "rollout"})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, method, **params):
        return self.hub.call(method, {**params, "agent_id": self.agent["agent_id"], "session_token": self.agent["session_token"]})

    def test_legacy_board_pagination_and_changes(self):
        (self.root / "NOTICEBOARD.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
        page = self.call("legacy_noticeboard_read", start_line=0, limit=2)
        self.assertEqual(page["lines"], ["first", "second"])
        self.assertEqual(page["next_start_line"], 2)
        self.assertEqual(self.call("legacy_noticeboard_read", start_line=2)["lines"], ["third"])
        (self.root / "NOTICEBOARD.md").write_text("new CLAIM\n", encoding="utf-8")
        self.assertEqual(self.call("legacy_noticeboard_read")["lines"], ["new CLAIM"])

    def test_missing_board_and_invalid_pagination(self):
        self.assertFalse(self.call("legacy_noticeboard_read")["exists"])
        for params in [{"start_line": -1}, {"limit": 0}, {"limit": 201}]:
            with self.assertRaises(BoardError):
                self.call("legacy_noticeboard_read", **params)

    def test_status_exposes_transition_rules(self):
        status = self.call("board_status")
        self.assertIn("coordination_rules", status)
        self.assertIn("legacy_noticeboard", status)
        self.assertEqual(status["legacy_noticeboard"]["path"], str(self.root / "NOTICEBOARD.md"))

    def test_health_verifies_workspace_without_registering(self):
        server = serve(self.hub, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{server.server_port}/health", timeout=3) as response:
                health = json.load(response)
            self.assertEqual(health["service"], "agent-board")
            self.assertEqual(health["workspace"], str(self.root))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
