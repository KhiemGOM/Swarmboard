import concurrent.futures
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from board.core import BoardError, Hub
from board.server import serve


class HubTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hub = Hub(self.root / "hub.sqlite", self.root)
        self.a = self.hub.call("register", {"name": "A"})
        self.b = self.hub.call("register", {"name": "B"})

    def tearDown(self):
        self.temp.cleanup()

    def call(self, who, operation, **params):
        return self.hub.call(operation, {**params, "agent_id": who["agent_id"], "session_token": who["session_token"]})

    def test_concurrent_claim_has_one_winner(self):
        gate = threading.Barrier(2)
        def compete(who):
            gate.wait()
            return self.call(who, "claim", paths=["src"], reason="work")["accepted"]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(compete, [self.a, self.b]))
        self.assertEqual(sorted(results), [False, True])

    def test_any_hub_response_previews_only_callers_unread_inbox_without_ack(self):
        first = self.call(self.b, 'inbox_send', recipient_agent_id=self.a['agent_id'], body='Waiting for your reply')
        self.call(self.a, 'inbox_send', recipient_agent_id=self.b['agent_id'], body='Private to B')
        result = self.call(self.a, 'noticeboard_read')
        self.assertEqual(result['inbox_preview'], [{'id': first['id'], 'sender': self.b['agent_id'], 'body': 'Waiting for your reply'}])
        self.assertEqual(self.call(self.a, 'inbox_read')['unread_inbox'], 1)
        self.call(self.a, 'inbox_ack', message_ids=[first['id']])
        self.assertNotIn('inbox_preview', self.call(self.a, 'board_status'))

    def test_parent_child_conflict_but_siblings_allowed(self):
        self.call(self.a, "claim", paths=["src/editor"], reason="editing")
        self.assertFalse(self.call(self.b, "claim", paths=["src"], reason="parent")["accepted"])
        self.assertFalse(self.call(self.b, "claim", paths=["src/editor/a.py"], reason="child")["accepted"])
        self.assertTrue(self.call(self.b, "claim", paths=["src/server"], reason="sibling")["accepted"])

    def test_multifile_claim_is_all_or_nothing(self):
        self.call(self.a, "claim", paths=["a.py"], reason="work")
        self.assertFalse(self.call(self.b, "claim", paths=["b.py", "a.py"], reason="work")["accepted"])
        self.assertTrue(self.call(self.a, "claim", paths=["b.py"], reason="work")["accepted"])

    def test_takeover_revokes_original_identity(self):
        ids = self.call(self.a, "claim", paths=["src"], reason="work")["claim_ids"]
        self.call(self.b, "takeover", target_agent_id=self.a["agent_id"], reason="Continuing A's work; A is dead")
        with self.assertRaisesRegex(BoardError, "retired"):
            self.call(self.a, "claim", paths=["other"], reason="returned")
        self.assertEqual(self.call(self.b, "board_status")["claims"][0]["agent"], self.b["agent_id"])
        self.call(self.b, "release", claim_ids=ids, commit_hash="abcdef0")

    def test_messages_remind_until_acknowledged_and_survive_restart(self):
        message = self.call(self.a, "inbox_send", recipient_agent_id=self.b["agent_id"], body="Need port 3101")
        self.call(self.a, "noticeboard_post", body="Database is being rebuilt")
        self.hub = Hub(self.root / "hub.sqlite", self.root)
        self.assertEqual(self.call(self.b, "board_status")["unread_inbox"], 1)
        self.assertEqual(self.call(self.b, "inbox_read")["unread_inbox"], 1)
        self.assertEqual(self.call(self.b, "noticeboard_read")["posts"][0]["body"], "Database is being rebuilt")
        self.assertEqual(self.call(self.b, "inbox_ack", message_ids=[message["id"]])["unread_inbox"], 0)

    def test_authentication_and_ack_ownership(self):
        with self.assertRaises(BoardError):
            self.hub.call("board_status", {"agent_id": self.a["agent_id"], "session_token": "wrong"})
        message = self.call(self.a, "inbox_send", recipient_agent_id=self.b["agent_id"], body="hello")
        with self.assertRaises(BoardError):
            self.call(self.a, "inbox_ack", message_ids=[message["id"]])

    def test_path_escape_and_globs_refused(self):
        for path in ["../escape", "src/**"]:
            with self.assertRaises(BoardError):
                self.call(self.a, "claim", paths=[path], reason="work")

    def test_expired_lease_can_be_claimed(self):
        self.call(self.a, "claim", paths=["src"], reason="work")
        with self.hub.connect() as db:
            db.execute("UPDATE claims SET expires=0")
        self.assertTrue(self.call(self.b, "claim", paths=["src"], reason="work")["accepted"])

    def test_wrong_database_workspace_refused(self):
        with self.assertRaises(BoardError):
            Hub(self.root / "hub.sqlite", self.root / "different")

    def test_http_and_two_mcp_processes(self):
        server = serve(self.hub, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}"
        entry = Path(__file__).resolve().parents[1] / "run_board.py"
        processes = [subprocess.Popen([sys.executable, str(entry), "mcp", "--url", url, "--name", name],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', cwd=self.root)
                     for name in ["codex-probe", "claude-probe"]]
        def rpc(process, method, params=None):
            process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}) + "\n")
            process.stdin.flush()
            response = json.loads(process.stdout.readline())
            self.assertNotIn("error", response)
            return response["result"]
        def tool(process, name, arguments=None):
            result = rpc(process, "tools/call", {"name": name, "arguments": arguments or {}})
            self.assertFalse(result["isError"])
            return json.loads(result["content"][0]["text"])
        try:
            for process in processes:
                initialized = rpc(process, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1"}})
                self.assertEqual(initialized["serverInfo"]["name"], "swarmboard")
                self.assertEqual(len(rpc(process, "tools/list")["tools"]), 24)
            first, second = processes
            a = tool(first, "board_status")["your_agent_id"]
            b = tool(second, "board_status")["your_agent_id"]
            self.assertNotEqual(a, b)
            unicode_message = tool(first, 'inbox_send', {'recipient_agent_id': b, 'body': 'Unicode patch context: 日本語 🐝'})
            self.assertIn('日本語 🐝', tool(second, 'inbox_read')['messages'][0]['body'])
            tool(second, 'inbox_ack', {'message_ids': [unicode_message['id']]})
            self.assertTrue(tool(first, "claim", {"paths": ["src"], "reason": "probe"})["accepted"])
            self.assertFalse(tool(second, "claim", {"paths": ["src/a.py"], "reason": "probe"})["accepted"])
            files = tool(first, "board_status")["claims"]
            missing_evidence = rpc(first, "tools/call", {"name": "release", "arguments": {"claim_ids": [files[0]["id"]]}})
            self.assertTrue(missing_evidence["isError"])
            resource = tool(first, "mutex_claim", {"resources": ["db:shared_test"], "reason": "tests"})
            self.assertFalse(tool(second, "mutex_claim", {"resources": ["db:shared_test"], "reason": "other tests"})["accepted"])
            tool(first, "release", {"claim_ids": resource["claim_ids"], "reason": "tests finished"})
            message = tool(first, "inbox_send", {"recipient_agent_id": b, "body": "handoff"})
            posted = tool(first, 'job_post', {'title': 'Probe job', 'description': 'Authorized simulation', 'acceptance': 'Probe passes'})['job']
            owned = tool(first, 'job_claim', {'job_id': posted['id']})['job']
            self.assertFalse(tool(second, 'job_claim', {'job_id': posted['id']})['accepted'])
            tool(first, 'job_update', {'job_id': posted['id'], 'lease_token': owned['lease_token'], 'progress': 'Probe checks passed'})
            self.assertIn("inbox_reminder", tool(second, "board_status"))
            transferred = tool(second, "takeover", {"target_agent_id": a, "reason": "First probe is dead"})
            resumed = transferred['jobs'][0]
            done = tool(second, 'job_finish', {'job_id': posted['id'], 'lease_token': resumed['lease_token'], 'summary': 'Probe finished',
                        'commit_hash': 'abcdef0', 'checks': [{'command': 'probe', 'result': 'passed', 'evidence': 'Simulated checks'}]})
            self.assertEqual(done['job']['state'], 'DONE')
            retired = rpc(first, "tools/call", {"name": "board_status"})
            self.assertTrue(retired["isError"])
            self.assertEqual(tool(second, "inbox_ack", {"message_ids": [message["id"]]})["unread_inbox"], 0)
            tool(second, "release", {"claim_ids": [files[0]["id"]], "held_reason": "Awaiting validation"})
            self.assertIn("custody_reminder", tool(second, "board_status"))
            tool(second, "custody_resolve", {"paths": ["src"], "commit_hash": "abcdef0"})
            self.assertNotIn("custody_reminder", tool(second, "board_status"))
            unfinished = tool(second, 'job_post', {'title': 'Unfinished probe', 'description': 'Recovery simulation', 'acceptance': 'Probe passes'})['job']
            tool(second, 'job_claim', {'job_id': unfinished['id']})
        finally:
            for process in processes:
                process.stdin.close()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                process.stdout.close()
                process.stderr.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        with self.hub.connect() as db:
            self.assertEqual(db.execute("SELECT state FROM jobs WHERE title='Unfinished probe'").fetchone()[0], 'OPEN')
            self.assertIsNotNone(db.execute('SELECT disconnected FROM agents WHERE id=?', (b,)).fetchone()[0])


if __name__ == "__main__":
    unittest.main()
