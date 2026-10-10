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

    def test_status_exposes_custody_and_no_legacy_integration(self):
        status = self.call("board_status")
        self.assertIn("custody", status)
        self.assertNotIn("legacy_noticeboard", status)
        with self.assertRaises(BoardError):
            self.call("legacy_noticeboard_read")

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
